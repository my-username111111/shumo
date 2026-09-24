"""Question 2: multi-sortie dispatch with explicit aircraft and battery events.

This is a feasible-solution search, not a claim of global optimality. The main
policy minimizes weighted soft delay, then weighted delivery time, completion
time, energy and sortie count. Complete alternatives show the timing/energy
tradeoff. Every accepted candidate is replayed with physical aircraft,
type-specific batteries, charging and hard deadlines.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations
from random import Random

from model import Scenario, distance_m
from q1 import solve_zone
from q2_alns import solve_two_stage_alns
from q2_refinement import solve_refinements


@dataclass(frozen=True)
class Job:
    id: str
    visits: tuple[tuple[str, tuple[str, ...]], ...]

    @property
    def box_ids(self) -> tuple[str, ...]:
        return tuple(b for _, boxes in self.visits for b in boxes)


class Dispatch:
    def __init__(self, scenario: Scenario, priority: str = "soft_delay"):
        self.s = scenario
        if priority not in {"soft_delay", "delivery"}:
            raise ValueError(f"Unknown Q2 priority: {priority}")
        self.priority = priority

    @lru_cache(maxsize=30000)
    def evaluate(self, job: Job, model: str) -> dict | None:
        try:
            return self.s.route(model, [(z, list(bs)) for z, bs in job.visits])
        except ValueError:
            return None

    def initial_jobs(self) -> list[Job]:
        jobs = []
        for zone in sorted(self.s.zone_boxes):
            hard = {b.id for b in self.s.zone_boxes[zone] if b.hard_deadline is not None}
            if hard:
                jobs.append(Job(f"{zone}-H", ((zone, tuple(sorted(hard))),)))
            other = {b.id for b in self.s.zone_boxes[zone] if b.id not in hard}
            if other:
                partition = solve_zone(self.s, zone, box_ids=other)
                for i, b in enumerate(partition["batches"], 1):
                    jobs.append(Job(f"{zone}-R{i}", ((zone, tuple(sorted(b["box_ids"]))),)))
        return jobs

    def _job_key(self, job: Job) -> tuple:
        boxes = [self.s.boxes[b] for b in job.box_ids]
        best_duration = min((ev["duration"] for g in self.s.transport
                             if (ev := self.evaluate(job, g)) is not None), default=float("inf"))
        # Hard and soft due times share the same clock. A hard deadline at
        # 10800 s must not automatically precede a soft target at 3600 s.
        # Feasibility of every hard box is still checked in _schedule_order.
        return (min((b.hard_deadline if b.hard_deadline is not None else b.desired)
                    for b in boxes),
                -best_duration, -sum(b.priority for b in boxes), job.id)

    def objective_key(self, result: dict) -> tuple:
        """Use expected-time lateness in the primary policy; keep a delivery-time comparator."""
        obj = result["objective"]
        if self.priority == "soft_delay":
            return (obj["weighted_soft_delay_seconds"], obj["weighted_delivery_seconds"],
                    obj["makespan"], obj["energy_kwh"], obj["sortie_count"])
        return (obj["weighted_delivery_seconds"], obj["makespan"],
                obj["energy_kwh"], obj["sortie_count"])

    def _schedule_order(self, jobs: list[Job]) -> dict | None:
        s = self.s
        aircraft_ready = {u: 0.0 for u in s.aircraft}
        battery_ids = {g: [f"{g}{i:02d}" for i in range(1, n + 1)] for g, n in s.battery_count.items()}
        battery_ready = {b: 0.0 for ids in battery_ids.values() for b in ids}
        rows = []
        deliveries = {}
        for job in jobs:
            candidates = []
            for model in s.transport:
                route = self.evaluate(job, model)
                if route is None:
                    continue
                for drone, drone_model in s.aircraft.items():
                    if drone_model != model:
                        continue
                    for battery in battery_ids[model]:
                        start = max(aircraft_ready[drone], battery_ready[battery])
                        deliver = {bid: start + rel for bid, rel in route["delivery"].items()}
                        if any(s.boxes[bid].hard_deadline is not None and
                               time > s.boxes[bid].hard_deadline + 1e-7
                               for bid, time in deliver.items()):
                            continue
                        weighted = sum(s.boxes[bid].priority * time for bid, time in deliver.items())
                        soft_delay = sum(s.boxes[bid].priority * max(0.0, time - s.boxes[bid].desired)
                                         for bid, time in deliver.items()
                                         if s.boxes[bid].hard_deadline is None)
                        if self.priority == "soft_delay":
                            score = (soft_delay, weighted, start + route["duration"],
                                     route["energy_kwh"], drone, battery)
                        else:
                            score = (weighted, start + route["duration"],
                                     route["energy_kwh"], drone, battery)
                        candidates.append((score, model, drone, battery, start, route, deliver))
            if not candidates:
                return None
            _, model, drone, battery, start, route, deliver = min(candidates, key=lambda x: x[0])
            end = start + route["duration"]
            charge = s.charge_time(route["return_soc"], s.battery_charge[model])
            aircraft_ready[drone] = end
            battery_ready[battery] = end + charge
            sid = f"T{len(rows) + 1:03d}"
            rows.append(dict(id=sid, job=job.id, drone=drone, model=model, battery=battery,
                             start=start, return_time=end, battery_ready=end + charge,
                             energy_kwh=route["energy_kwh"], return_soc=route["return_soc"],
                             visits=route["visits"], load_kg=route["load_kg"], load_m3=route["load_m3"],
                             route=route))
            for bid, when in deliver.items():
                deliveries[bid] = dict(sortie=sid, zone=s.boxes[bid].zone, time=when)
        if set(deliveries) != set(s.boxes):
            return None
        weighted_delivery = sum(s.boxes[bid].priority * x["time"] for bid, x in deliveries.items())
        weighted_delay = sum(s.boxes[bid].priority * max(0, x["time"] - s.boxes[bid].desired)
                             for bid, x in deliveries.items() if s.boxes[bid].hard_deadline is None)
        return dict(sorties=rows, deliveries=deliveries,
                    objective=dict(weighted_delivery_seconds=weighted_delivery,
                                   weighted_soft_delay_seconds=weighted_delay,
                                   makespan=max(r["return_time"] for r in rows),
                                   energy_kwh=sum(r["energy_kwh"] for r in rows),
                                   sortie_count=len(rows)))

    def schedule_beam(self, jobs: list[Job], beam_width: int = 64,
                      job_branches: int = 6, assignment_branches: int = 5) -> tuple[dict | None, dict]:
        """Jointly choose job order, aircraft, battery and model with a bounded beam.

        Unlike ``_schedule_order``, this keeps several partial schedules alive so
        an attractive immediate assignment cannot monopolize a scarce aircraft or
        battery and damage later deliveries.  All completed plans are evaluated by
        the same objective used for final Q2 selection.
        """
        s = self.s
        battery_ids = {g: [f"{g}{i:02d}" for i in range(1, n + 1)]
                       for g, n in s.battery_count.items()}
        initial = dict(
            remaining=tuple(range(len(jobs))),
            aircraft={u: 0.0 for u in s.aircraft},
            batteries={b: 0.0 for ids in battery_ids.values() for b in ids},
            rows=[], deliveries={}, soft=0.0, weighted=0.0,
            makespan=0.0, energy=0.0,
        )
        beam = [initial]
        expanded = feasible = 0

        def partial_key(state: dict) -> tuple:
            # Every state at one layer has delivered the same number of jobs.
            # Use the final policy fields in exactly the same order.
            if self.priority == "soft_delay":
                return (state["soft"], state["weighted"], state["makespan"],
                        state["energy"], sum(state["aircraft"].values()))
            return (state["weighted"], state["makespan"], state["energy"],
                    sum(state["aircraft"].values()))

        while beam and beam[0]["remaining"]:
            children = []
            for state in beam:
                choices = sorted(state["remaining"], key=lambda i: self._job_key(jobs[i]))[:job_branches]
                for idx in choices:
                    job = jobs[idx]
                    assignments = []
                    for model in s.transport:
                        route = self.evaluate(job, model)
                        if route is None:
                            continue
                        for drone, drone_model in s.aircraft.items():
                            if drone_model != model:
                                continue
                            for battery in battery_ids[model]:
                                start = max(state["aircraft"][drone], state["batteries"][battery])
                                deliver = {bid: start + rel for bid, rel in route["delivery"].items()}
                                expanded += 1
                                if any(s.boxes[bid].hard_deadline is not None and
                                       when > s.boxes[bid].hard_deadline + 1e-7
                                       for bid, when in deliver.items()):
                                    continue
                                weighted = sum(s.boxes[bid].priority * when
                                               for bid, when in deliver.items())
                                soft = sum(s.boxes[bid].priority * max(0.0, when - s.boxes[bid].desired)
                                           for bid, when in deliver.items()
                                           if s.boxes[bid].hard_deadline is None)
                                end = start + route["duration"]
                                if self.priority == "soft_delay":
                                    score = (soft, weighted, end, route["energy_kwh"], drone, battery)
                                else:
                                    score = (weighted, end, route["energy_kwh"], drone, battery)
                                assignments.append((score, model, drone, battery, start,
                                                    end, route, deliver))
                    for _, model, drone, battery, start, end, route, deliver in sorted(
                            assignments, key=lambda x: x[0])[:assignment_branches]:
                        feasible += 1
                        sid = f"T{len(state['rows']) + 1:03d}"
                        charge = s.charge_time(route["return_soc"], s.battery_charge[model])
                        aircraft = state["aircraft"].copy()
                        batteries = state["batteries"].copy()
                        aircraft[drone] = end
                        batteries[battery] = end + charge
                        row = dict(id=sid, job=job.id, drone=drone, model=model, battery=battery,
                                   start=start, return_time=end, battery_ready=end + charge,
                                   energy_kwh=route["energy_kwh"], return_soc=route["return_soc"],
                                   visits=route["visits"], load_kg=route["load_kg"],
                                   load_m3=route["load_m3"], route=route)
                        new_deliveries = state["deliveries"].copy()
                        for bid, when in deliver.items():
                            new_deliveries[bid] = dict(sortie=sid, zone=s.boxes[bid].zone, time=when)
                        children.append(dict(
                            remaining=tuple(k for k in state["remaining"] if k != idx),
                            aircraft=aircraft, batteries=batteries,
                            rows=state["rows"] + [row], deliveries=new_deliveries,
                            soft=state["soft"] + sum(
                                s.boxes[bid].priority * max(0.0, when - s.boxes[bid].desired)
                                for bid, when in deliver.items()
                                if s.boxes[bid].hard_deadline is None),
                            weighted=state["weighted"] + sum(
                                s.boxes[bid].priority * when for bid, when in deliver.items()),
                            makespan=max(state["makespan"], end),
                            energy=state["energy"] + route["energy_kwh"],
                        ))
            if not children:
                break
            # Identical remaining jobs and resource ready times represent the
            # same future state; retain only the best prefix.
            dedup = {}
            for child in sorted(children, key=partial_key):
                signature = (child["remaining"],
                             tuple(round(child["aircraft"][u], 6) for u in sorted(child["aircraft"])),
                             tuple(round(child["batteries"][b], 6) for b in sorted(child["batteries"])))
                dedup.setdefault(signature, child)
            beam = sorted(dedup.values(), key=partial_key)[:beam_width]

        complete = []
        for state in beam:
            if state["remaining"] or set(state["deliveries"]) != set(s.boxes):
                continue
            plan = dict(sorties=state["rows"], deliveries=state["deliveries"],
                        objective=dict(weighted_delivery_seconds=state["weighted"],
                                       weighted_soft_delay_seconds=state["soft"],
                                       makespan=state["makespan"], energy_kwh=state["energy"],
                                       sortie_count=len(state["rows"])))
            complete.append(plan)
        best = min(complete, key=self.objective_key) if complete else None
        return best, dict(beam_width=beam_width, job_branches=job_branches,
                          assignment_branches=assignment_branches,
                          expanded=expanded, feasible=feasible,
                          complete=len(complete))

    def schedule(self, jobs: list[Job], attempts: int = 15, seed: int = 2026) -> dict:
        ordered = sorted(jobs, key=self._job_key)
        best = None
        rng = Random(seed)
        for attempt in range(attempts):
            if attempt == 0:
                order = ordered
            else:
                # Preserve deadline classes; randomize within classes to avoid
                # a single arbitrary tie order controlling resource allocation.
                groups: dict[int, list[Job]] = {}
                for job in ordered:
                    groups.setdefault(int(self._job_key(job)[0]), []).append(job)
                order = []
                for key in sorted(groups):
                    group = groups[key][:]
                    rng.shuffle(group)
                    order.extend(group)
            result = self._schedule_order(order)
            if result is None:
                continue
            score = self.objective_key(result)
            if best is None or score < best[0]:
                best = (score, result)
        if best is None:
            raise ValueError("Unable to schedule all hard-deadline cargo with current route set")
        return best[1]

    def schedule_in_order(self, jobs: list[Job]) -> dict | None:
        """Replay a proposed job order with the same aircraft/battery feasibility check."""
        return self._schedule_order(jobs)

    def merge(self, a: Job, b: Job, reverse: bool = False) -> Job:
        visits = list(b.visits if reverse else a.visits) + list(a.visits if reverse else b.visits)
        merged: list[tuple[str, tuple[str, ...]]] = []
        for zone, boxes in visits:
            if merged and merged[-1][0] == zone:
                merged[-1] = zone, tuple(sorted((*merged[-1][1], *boxes)))
            elif zone in {z for z, _ in merged}:
                # Repeated visits to a zone add no benefit in this merge.
                return Job(a.id + "+" + b.id, tuple(visits))
            else:
                merged.append((zone, boxes))
        return Job(a.id + "+" + b.id, tuple(merged))

    def improve_merges(self, jobs: list[Job], baseline: dict, max_rounds: int = 8,
                       policy: str = "priority") -> tuple[list[Job], dict]:
        current_jobs, current = jobs[:], baseline
        base_time = baseline["objective"]["weighted_delivery_seconds"]
        base_makespan = baseline["objective"]["makespan"]
        for _ in range(max_rounds):
            best = None
            for i in range(len(current_jobs)):
                for j in range(i + 1, len(current_jobs)):
                    for reverse in (False, True):
                        candidate_job = self.merge(current_jobs[i], current_jobs[j], reverse)
                        if not any(self.evaluate(candidate_job, model) is not None for model in self.s.transport):
                            continue
                        proposal = [job for k, job in enumerate(current_jobs) if k not in (i, j)] + [candidate_job]
                        try:
                            trial = self.schedule(proposal, attempts=3)
                        except ValueError:
                            continue
                        if policy == "priority":
                            score = self.objective_key(trial)
                            if score >= self.objective_key(current):
                                continue
                        elif policy == "energy_guarded":
                            obj = trial["objective"]
                            if (obj["weighted_delivery_seconds"] > 1.05 * base_time or
                                    obj["makespan"] > 1.10 * base_makespan or
                                    obj["energy_kwh"] >= current["objective"]["energy_kwh"] - 1e-8):
                                continue
                            score = (obj["energy_kwh"], obj["weighted_delivery_seconds"],
                                     obj["makespan"], obj["sortie_count"])
                        else:
                            raise ValueError(f"Unknown Q2 policy: {policy}")
                        if best is None or score < best[0]:
                            best = (score, proposal, trial)
            if best is None:
                break
            _, current_jobs, current = best
        return current_jobs, current

    def improve_routes(self, jobs: list[Job], baseline: dict, rounds: int = 4,
                       max_trials: int = 240) -> tuple[list[Job], dict, dict]:
        """Deterministic relocate/exchange/split search with full rescheduling."""
        current_jobs, current = jobs[:], baseline
        evaluated = feasible = accepted = 0
        for _ in range(rounds):
            jobs_by_id = {job.id: job for job in current_jobs}
            ordered = [jobs_by_id[row["job"]] for row in current["sorties"]]
            pressure = lambda job: sum(self.s.boxes[bid].priority * current["deliveries"][bid]["time"]
                                       for bid in job.box_ids)
            focus = sorted(range(len(ordered)), key=lambda i: (-pressure(ordered[i]), i))[:8]
            proposals: list[tuple[str, list[Job]]] = []

            def add(operator: str, candidate: list[Job]) -> None:
                proposals.append((operator, [Job(f"J{k:03d}", job.visits)
                                             for k, job in enumerate(candidate, 1)]))

            def remove_box(visits: tuple[tuple[str, tuple[str, ...]], ...], bid: str
                           ) -> tuple[tuple[str, tuple[str, ...]], ...]:
                return tuple((zone, tuple(b for b in batch if b != bid))
                             for zone, batch in visits if any(b != bid for b in batch))

            def insert_box(visits: tuple[tuple[str, tuple[str, ...]], ...], bid: str,
                           position: int) -> tuple[tuple[str, tuple[str, ...]], ...]:
                result = list(visits)
                zone = self.s.boxes[bid].zone
                matching = next((k for k, (z, _) in enumerate(result) if z == zone), None)
                if matching is not None:
                    z, batch = result[matching]
                    result[matching] = (z, tuple(sorted((*batch, bid))))
                else:
                    result.insert(position, (zone, (bid,)))
                return tuple(result)

            for i in focus:
                job = ordered[i]
                if len(job.visits) > 1:
                    add("visit_order", ordered[:i] + [Job(job.id, tuple(reversed(job.visits)))] + ordered[i + 1:])
                    first = Job(job.id, job.visits[:1])
                    rest = Job(job.id, job.visits[1:])
                    add("split", ordered[:i] + [first, rest] + ordered[i + 1:])
                center = self.s.nodes[job.visits[0][0]]
                partners = sorted((j for j in range(len(ordered)) if j != i), key=lambda j: (
                    distance_m((center.lon, center.lat),
                               (self.s.nodes[ordered[j].visits[0][0]].lon,
                                self.s.nodes[ordered[j].visits[0][0]].lat)), j))[:3]
                boxes = sorted(job.box_ids, key=lambda bid: (
                    -self.s.boxes[bid].priority * current["deliveries"][bid]["time"], bid))[:2]
                for bid in boxes:
                    source_visits = remove_box(job.visits, bid)
                    for j in partners:
                        original_target = ordered[j].visits
                        zone = self.s.boxes[bid].zone
                        matching = any(z == zone for z, _ in original_target)
                        positions = (0,) if matching else tuple(range(len(original_target) + 1))
                        for position in positions:
                            altered = ordered[:]
                            target_visits = insert_box(original_target, bid, position)
                            altered[j] = Job(ordered[j].id, target_visits)
                            altered[i] = Job(job.id, source_visits) if source_visits else None
                            add("relocate", [x for x in altered if x is not None])

                        # Exchange can cross a capacity barrier that a one-way
                        # relocation cannot.  Keep the neighborhood bounded by
                        # considering the two highest-pressure boxes per route.
                        target_boxes = sorted(ordered[j].box_ids, key=lambda other: (
                            -self.s.boxes[other].priority * current["deliveries"][other]["time"],
                            other))[:2]
                        for other in target_boxes:
                            if other == bid:
                                continue
                            left = insert_box(remove_box(job.visits, bid), other,
                                              min(len(job.visits), 1))
                            right = insert_box(remove_box(ordered[j].visits, other), bid,
                                               min(len(ordered[j].visits), 1))
                            altered = ordered[:]
                            altered[i] = Job(job.id, left)
                            altered[j] = Job(ordered[j].id, right)
                            add("exchange", altered)
                for j in (i - 1, i + 1):
                    if 0 <= j < len(ordered):
                        altered = ordered[:]
                        altered[i], altered[j] = altered[j], altered[i]
                        add("job_order", altered)

            seen = set()
            best = None
            trials_this_round = 0
            for operator, proposal in proposals:
                signature = tuple(job.visits for job in proposal)
                if signature in seen:
                    continue
                seen.add(signature)
                if trials_this_round >= max_trials:
                    break
                # Route feasibility is independent of the eventual start time.
                if any(not any(self.evaluate(job, g) is not None for g in self.s.transport)
                       for job in proposal):
                    continue
                evaluated += 1
                trials_this_round += 1
                try:
                    trial = (self._schedule_order(proposal) if operator == "job_order"
                             else self.schedule(proposal, attempts=3))
                except ValueError:
                    continue
                if trial is None:
                    continue
                feasible += 1
                score = self.objective_key(trial)
                if score < self.objective_key(current) and (best is None or score < best[0]):
                    best = (score, proposal, trial)
            if best is None:
                break
            _, current_jobs, current = best
            accepted += 1
        return current_jobs, current, dict(evaluated=evaluated, feasible=feasible, accepted=accepted)

    def improve_fast_splits(self, baseline: dict, energy_ratio: float = 1.03) -> tuple[dict, dict]:
        """Find a faster plan by splitting one C sortie while preserving soft deadlines.

        The previous neighborhood only split routes with multiple stops. It
        could not release either C aircraft from a large single-zone batch.
        A split is accepted only after full aircraft/battery rescheduling and
        only if its energy stays within the stated baseline budget.
        """
        ordered = [Job(row["job"], tuple((zone, tuple(boxes)) for zone, boxes in row["visits"]))
                   for row in baseline["sorties"]]
        base = baseline["objective"]
        best = baseline
        best_key = self.objective_key(baseline)
        evaluated = feasible = 0
        for i, row in enumerate(baseline["sorties"]):
            if row["model"] != "C" or len(ordered[i].visits) != 1:
                continue
            zone, box_ids = ordered[i].visits[0]
            if len(box_ids) < 2:
                continue
            for size in range(1, len(box_ids)):
                for subset in combinations(box_ids, size):
                    # Each unordered partition is visited once.
                    if box_ids[0] not in subset:
                        continue
                    remainder = tuple(b for b in box_ids if b not in subset)
                    left = Job(f"{row['job']}a", ((zone, subset),))
                    right = Job(f"{row['job']}b", ((zone, remainder),))
                    if any(not any(self.evaluate(job, model) is not None
                                   for model in self.s.transport) for job in (left, right)):
                        continue
                    for pair in ((left, right), (right, left)):
                        trial = self._schedule_order(ordered[:i] + list(pair) + ordered[i + 1:])
                        evaluated += 1
                        if trial is None:
                            continue
                        feasible += 1
                        obj = trial["objective"]
                        if (obj["weighted_soft_delay_seconds"] >
                                base["weighted_soft_delay_seconds"] + 1e-7 or
                                obj["energy_kwh"] > energy_ratio * base["energy_kwh"]):
                            continue
                        key = self.objective_key(trial)
                        if key < best_key:
                            best_key, best = key, trial
        return best, dict(evaluated=evaluated, feasible=feasible,
                          energy_limit_kwh=energy_ratio * base["energy_kwh"])

    def improve_order(self, baseline: dict, mode: str, attempts: int,
                      seed: int = 20260923) -> tuple[dict, dict]:
        """Reassign resources after small execution-order moves within an energy cap."""
        if mode not in {"fast", "energy_guarded"}:
            raise ValueError(f"Unknown order refinement mode: {mode}")
        order = [Job(row["job"], tuple((zone, tuple(boxes)) for zone, boxes in row["visits"]))
                 for row in baseline["sorties"]]
        limit = baseline["objective"]["energy_kwh"] + 1e-7

        def score(plan: dict) -> tuple:
            obj = plan["objective"]
            if mode == "fast":
                return self.objective_key(plan)
            return (obj["weighted_soft_delay_seconds"], obj["energy_kwh"], obj["makespan"])

        best = baseline
        rng = Random(seed)
        evaluated = feasible = accepted = 0
        for _ in range(attempts):
            proposal = order[:]
            i = rng.randrange(len(proposal))
            j = rng.randrange(len(proposal))
            if i == j:
                continue
            if rng.random() < 0.6:
                proposal.insert(j, proposal.pop(i))
            else:
                proposal[i], proposal[j] = proposal[j], proposal[i]
            trial = self._schedule_order(proposal)
            evaluated += 1
            if trial is None:
                continue
            feasible += 1
            if trial["objective"]["energy_kwh"] > limit:
                continue
            if score(trial) < score(best):
                order, best = proposal, trial
                accepted += 1
        return best, dict(evaluated=evaluated, feasible=feasible,
                          accepted=accepted, energy_limit_kwh=limit)


def solve(s: Scenario, improve: bool = True) -> dict:
    dispatcher = Dispatch(s, priority="soft_delay")
    jobs = dispatcher.initial_jobs()
    base = dispatcher.schedule(jobs)
    if improve:
        jobs, merged = dispatcher.improve_merges(jobs, base)
        merge_rounds = base["objective"]["sortie_count"] - len(jobs)
        jobs, result, neighborhood = dispatcher.improve_routes(jobs, merged)
        beam_plan, beam_search = dispatcher.schedule_beam(jobs)
        if beam_plan is not None and dispatcher.objective_key(beam_plan) < dispatcher.objective_key(result):
            result = beam_plan
        zero_delay_efficient = result
        fast_zero_delay, fast_split_search = dispatcher.improve_fast_splits(result)
        fast_split_plan = fast_zero_delay
        fast_zero_delay, fast_order_search = dispatcher.improve_order(
            fast_zero_delay, mode="fast", attempts=1000)
        dispatcher.priority = "delivery"
        delivery_base = dispatcher.schedule(dispatcher.initial_jobs())
        delivery_jobs, delivery_merged = dispatcher.improve_merges(dispatcher.initial_jobs(), delivery_base)
        _, delivery_priority, delivery_neighborhood = dispatcher.improve_routes(delivery_jobs, delivery_merged)
        _, energy_guarded = dispatcher.improve_merges(dispatcher.initial_jobs(), delivery_base,
                                                      policy="energy_guarded")
        dispatcher.priority = "soft_delay"
        energy_guarded_timely, energy_order_search = dispatcher.improve_order(
            energy_guarded, mode="energy_guarded", attempts=2000)
        preferred = min((result, fast_split_plan, fast_zero_delay), key=dispatcher.objective_key)
        if preferred is not result:
            result = preferred
        alns_main, alns_zero_delay, alns_search = solve_two_stage_alns(
            dispatcher, result, zero_delay_efficient, Job)
        if dispatcher.objective_key(alns_main) < dispatcher.objective_key(result):
            result = alns_main
        previous_main = result
        quick_refined, timely_refined, energy_refined, refinement_search = solve_refinements(
            dispatcher, previous_main, alns_zero_delay, Job)
        result = min((previous_main, quick_refined, timely_refined, energy_refined),
                     key=dispatcher.objective_key)
    else:
        result = base
        merged = base
        merge_rounds = 0
        neighborhood = dict(evaluated=0, feasible=0, accepted=0)
        delivery_priority = base
        delivery_neighborhood = dict(evaluated=0, feasible=0, accepted=0)
        energy_guarded = base
        energy_guarded_timely = base
        fast_zero_delay = base
        fast_split_plan = base
        zero_delay_efficient = base
        fast_split_search = dict(evaluated=0, feasible=0)
        fast_order_search = dict(evaluated=0, feasible=0, accepted=0)
        energy_order_search = dict(evaluated=0, feasible=0, accepted=0)
        beam_search = dict(beam_width=0, job_branches=0, assignment_branches=0,
                           expanded=0, feasible=0, complete=0)
        alns_zero_delay = base
        alns_search = dict(policy="disabled", delivery_ratio=1.03,
                           delivery_cap=base["objective"]["weighted_delivery_seconds"],
                           stage1={}, stage2_selected={}, stage2_runs=[])
        previous_main = base
        quick_refined = timely_refined = energy_refined = base
        refinement_search = dict(policy="disabled")
    result["search"] = dict(policy="weighted_soft_delay_then_weighted_delivery_then_makespan_then_energy_then_sorties",
                            initial_objective=base["objective"],
                            merged_objective=merged["objective"],
                            delivery_priority_objective=delivery_priority["objective"],
                            energy_guarded_objective=energy_guarded["objective"],
                            energy_guarded_timely_objective=energy_guarded_timely["objective"],
                            neighborhood=neighborhood,
                            joint_resource_beam=beam_search,
                            alns=alns_search,
                            refinement=refinement_search,
                            fast_split_search=fast_split_search,
                            fast_order_search=fast_order_search,
                            energy_order_search=energy_order_search,
                            delivery_neighborhood=delivery_neighborhood,
                            initial_sorties=base["objective"]["sortie_count"],
                            initial_energy_kwh=base["objective"]["energy_kwh"],
                            merge_rounds=merge_rounds)
    alternatives = {"delivery_priority": delivery_priority, "energy_guarded": energy_guarded}
    if improve:
        timing_alternative = ("zero_delay_efficient", zero_delay_efficient)
        if result is zero_delay_efficient:
            timing_alternative = ("fast_zero_delay", fast_zero_delay)
        alternatives = {timing_alternative[0]: timing_alternative[1], **alternatives,
                        "energy_guarded_timely": energy_guarded_timely,
                        "alns_zero_delay_efficient": alns_zero_delay,
                        "quick_24": quick_refined,
                        "energy_22": energy_refined}
        if result is not previous_main:
            alternatives["prior_soft_delay_priority"] = previous_main
        if result is not timely_refined:
            alternatives["timely_22"] = timely_refined
    # A search can keep the baseline as both the primary and an alternative.
    # Store only independent plan containers so JSON export cannot self-reference.
    result["alternatives"] = {
        name: {field: plan[field] for field in ("sorties", "deliveries", "objective")}
        for name, plan in alternatives.items()
    }
    return result
