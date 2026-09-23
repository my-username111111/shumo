"""Communication-guided large-neighborhood search for Question 3.

Question 2 remains an independent transport-only answer.  Here its sorties
seed a separate search: remove/reinsert indivisible boxes, split/merge jobs,
or change the service/order choices, then replay the Q2 resource scheduler and
the complete Q3 relay scheduler.  Only fully feasible Q3 plans can replace the
incumbent.  Operator weights are updated from completed Q3 evaluations.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from random import Random

from model import Scenario, distance_m
from q2 import Dispatch, Job
from q3 import deadline_group, route_gaps, solve as solve_relays


OPERATORS = ("relocate", "split", "merge", "visit_order", "job_order")


@dataclass
class Candidate:
    operator: str
    jobs: tuple[Job, ...]
    transport: dict
    gap_pressure: float


def _jobs_from_q2(q2: dict) -> tuple[Job, ...]:
    rows = sorted(q2["sorties"], key=lambda row: (row["start"], row["id"]))
    return tuple(Job(f"J{i:03d}", tuple((zone, tuple(boxes)) for zone, boxes in row["visits"]))
                 for i, row in enumerate(rows, 1))


def _renumber(jobs: list[Job]) -> tuple[Job, ...]:
    return tuple(Job(f"J{i:03d}", job.visits) for i, job in enumerate(jobs, 1))


def _signature(jobs: tuple[Job, ...]) -> tuple:
    return tuple(job.visits for job in jobs)


def _valid_coverage(s: Scenario, jobs: tuple[Job, ...]) -> bool:
    assignments = [(zone, bid) for job in jobs for zone, batch in job.visits for bid in batch]
    if any(s.boxes[bid].zone != zone for zone, bid in assignments):
        return False
    boxes = [bid for _, bid in assignments]
    return len(boxes) == len(s.boxes) and set(boxes) == set(s.boxes)


def _replace(jobs: tuple[Job, ...], changes: dict[int, list[Job]]) -> tuple[Job, ...]:
    result = []
    for i, job in enumerate(jobs):
        result.extend(changes.get(i, [job]))
    return _renumber(result)


def _job_center(s: Scenario, job: Job) -> tuple[float, float]:
    nodes = [s.nodes[zone] for zone, _ in job.visits]
    return (sum(node.lon for node in nodes) / len(nodes),
            sum(node.lat for node in nodes) / len(nodes))


def _nearby(s: Scenario, jobs: tuple[Job, ...], i: int, limit: int = 5) -> list[int]:
    center = _job_center(s, jobs[i])
    return sorted((j for j in range(len(jobs)) if j != i),
                  key=lambda j: distance_m(center, _job_center(s, jobs[j])))[:limit]


def _without_box(job: Job, bid: str) -> Job | None:
    visits = tuple((zone, tuple(b for b in batch if b != bid))
                   for zone, batch in job.visits if any(b != bid for b in batch))
    return Job(job.id, visits) if visits else None


def _with_box(job: Job, zone: str, bid: str, before: bool) -> Job:
    visits = list(job.visits)
    for i, (z, batch) in enumerate(visits):
        if z == zone:
            visits[i] = (z, tuple(sorted((*batch, bid))))
            break
    else:
        if before:
            visits.insert(0, (zone, (bid,)))
        else:
            visits.append((zone, (bid,)))
    return Job(job.id, tuple(visits))


def _neighbors(s: Scenario, dispatcher: Dispatch, jobs: tuple[Job, ...],
               focus: list[int], rng: Random) -> list[tuple[str, tuple[Job, ...]]]:
    proposals = []
    for i in focus:
        job = jobs[i]
        partners = _nearby(s, jobs, i)
        # Destroy one box assignment and repair it in another sortie.  Keep
        # the original box intact, including its zone and deadline.
        movable = sorted(job.box_ids, key=lambda bid: (
            s.boxes[bid].hard_deadline is not None, -s.boxes[bid].priority, bid))[:3]
        for bid in movable:
            source = _without_box(job, bid)
            for j in partners[:3]:
                for before in (False, True) if s.boxes[bid].zone not in dict(jobs[j].visits) else (False,):
                    target = _with_box(jobs[j], s.boxes[bid].zone, bid, before)
                    changes = {i: [source] if source else [], j: [target]}
                    proposals.append(("relocate", _replace(jobs, changes)))
        # Split an existing sortie to free a shared battery/aircraft time slot.
        if len(job.visits) > 1:
            first, rest = job.visits[:1], job.visits[1:]
            proposals.append(("split", _replace(jobs, {i: [Job(job.id, first), Job(job.id, rest)]})))
        elif len(job.box_ids) > 1:
            bid = movable[0]
            source = _without_box(job, bid)
            if source:
                singleton = Job(job.id, ((s.boxes[bid].zone, (bid,)),))
                proposals.append(("split", _replace(jobs, {i: [source, singleton]})))
        for j in partners[:4]:
            for reverse in (False, True):
                merged = dispatcher.merge(job, jobs[j], reverse)
                proposals.append(("merge", _replace(jobs, {i: [merged], j: []})))
        if len(job.visits) > 1:
            proposals.append(("visit_order", _replace(jobs, {
                i: [Job(job.id, tuple(reversed(job.visits)))]})))
        # The sequence also changes aircraft/battery allocation in Q2 and
        # relay-wave timing in Q3; deadline checks remain hard constraints.
        for j in (i - 2, i - 1, i + 1, i + 2):
            if 0 <= j < len(jobs) and j != i:
                changed = list(jobs)
                changed[i], changed[j] = changed[j], changed[i]
                proposals.append(("job_order", _renumber(changed)))
    rng.shuffle(proposals)
    return proposals


def _delay_focus(s: Scenario, q2: dict, q3: dict, jobs: tuple[Job, ...]) -> list[int]:
    delays = []
    for i, job in enumerate(jobs):
        penalty = sum(s.boxes[bid].priority * max(
            0.0, q3["deliveries"][bid]["time"] - q2["deliveries"][bid]["time"])
                      for bid in job.box_ids)
        delays.append((penalty, i))
    delays.sort(reverse=True)
    return [i for _, i in delays[:4]]


def _gap_pressure(s: Scenario, q2: dict, cache: dict[tuple, float]) -> float:
    pressure = 0.0
    for row in q2["sorties"]:
        key = (row["model"], tuple((zone, tuple(boxes)) for zone, boxes in row["visits"]))
        if key not in cache:
            cache[key] = len(route_gaps(s, row["route"]))
        pressure += cache[key] * sum(s.boxes[bid].priority for bid in row["route"]["box_ids"])
    return pressure


def _objective(q3: dict) -> tuple[float, float, float, int]:
    obj = q3["objective"]
    return (obj["weighted_delivery_seconds"], obj["makespan"],
            obj["energy_kwh"], obj["relay_sorties"])


def _promotion_options(s: Scenario, q2: dict, limit: int) -> list[tuple[str, int]]:
    hard_classes = sorted({deadline_group(s, row) for row in q2["sorties"]
                           if deadline_group(s, row) < 1_000_000})
    soft = sorted((row for row in q2["sorties"] if deadline_group(s, row) >= 1_000_000),
                  key=lambda row: (row["start"], row["id"]))
    # Early soft sorties are most likely to gain from sharing a hard-deadline
    # wave.  Late soft sorties remain eligible when the budget is increased.
    options = [(row["id"], due) for row in soft for due in hard_classes]
    return options[:limit]


def solve(s: Scenario, q2: dict, rounds: int = 2, trials_per_round: int = 3,
          promotion_trials: int = 12, seed: int = 2026) -> dict:
    """Return the best verified joint plan; Q2's standalone answer is unchanged."""
    incumbent = solve_relays(s, q2)
    baseline_objective = incumbent["objective"].copy()
    incumbent_transport = q2
    incumbent_jobs = _jobs_from_q2(q2)
    current = incumbent
    current_transport = q2
    current_jobs = incumbent_jobs
    dispatcher = Dispatch(s)
    rng = Random(seed)
    weights = {name: 1.0 for name in OPERATORS}
    gap_cache: dict[tuple, float] = {}
    seen_evaluated = {_signature(incumbent_jobs)}
    attempts = Counter()
    feasible = Counter()
    accepted = Counter()
    failures = Counter()
    trial_log = []
    evaluated = 0
    selected_promotion = None
    for promotion in _promotion_options(s, q2, promotion_trials):
        attempts["wave_promotion"] += 1
        evaluated += 1
        try:
            joint = solve_relays(s, q2, promotion=promotion)
        except ValueError as exc:
            failures[str(exc)] += 1
            trial_log.append(dict(round=0, operator="wave_promotion", promotion=promotion,
                                  status="infeasible", reason=str(exc)))
            continue
        feasible["wave_promotion"] += 1
        trial_log.append(dict(round=0, operator="wave_promotion", promotion=promotion,
                              status="feasible",
                              weighted_delivery_seconds=joint["objective"]["weighted_delivery_seconds"]))
        if _objective(joint) < _objective(incumbent):
            incumbent = joint
            accepted["wave_promotion"] += 1
            selected_promotion = promotion
    current = incumbent
    for round_number in range(1, rounds + 1):
        focus = _delay_focus(s, current_transport, current, current_jobs)
        pool: dict[str, list[Candidate]] = {name: [] for name in OPERATORS}
        round_seen = set()
        for operator, jobs in _neighbors(s, dispatcher, current_jobs, focus, rng):
            signature = _signature(jobs)
            if signature in seen_evaluated or signature in round_seen or not _valid_coverage(s, jobs):
                continue
            round_seen.add(signature)
            if not all(any(dispatcher.evaluate(job, model) is not None for model in s.transport)
                       for job in jobs):
                continue
            transport = dispatcher.schedule_in_order(list(jobs))
            if transport is None:
                continue
            pressure = _gap_pressure(s, transport, gap_cache)
            pool[operator].append(Candidate(operator, jobs, transport, pressure))
        # Keep both fast transport and low-gap variants for each operator.
        per_operator = {}
        for operator, candidates in pool.items():
            by_delivery = min(candidates, key=lambda c: (
                c.transport["objective"]["weighted_delivery_seconds"], c.gap_pressure), default=None)
            by_gap = min(candidates, key=lambda c: (
                c.gap_pressure, c.transport["objective"]["weighted_delivery_seconds"]), default=None)
            options = list({_signature(c.jobs): c for c in (by_delivery, by_gap) if c is not None}.values())
            if options:
                per_operator[operator] = options
        # Each chosen operator gets a trial before any operator receives a
        # second one.  Updated weights control the allocation across rounds.
        selected = []
        for operator in sorted(per_operator, key=lambda name: (-weights[name], name)):
            if len(selected) >= trials_per_round:
                break
            options = per_operator[operator]
            selected.append(options[(round_number - 1) % len(options)])
        extras = [candidate for options in per_operator.values() for candidate in options
                  if _signature(candidate.jobs) not in {_signature(c.jobs) for c in selected}]
        extras.sort(key=lambda c: (
            c.gap_pressure, c.transport["objective"]["weighted_delivery_seconds"]))
        selected.extend(extras[:max(0, trials_per_round - len(selected))])
        round_feasible = []
        for candidate in selected:
            seen_evaluated.add(_signature(candidate.jobs))
            attempts[candidate.operator] += 1
            evaluated += 1
            try:
                joint = solve_relays(s, candidate.transport)
            except ValueError as exc:
                failures[str(exc)] += 1
                trial_log.append(dict(round=round_number, operator=candidate.operator,
                                      status="infeasible", reason=str(exc)))
                weights[candidate.operator] = max(0.5, 0.8 * weights[candidate.operator])
                continue
            feasible[candidate.operator] += 1
            round_feasible.append((joint, candidate))
            trial_log.append(dict(round=round_number, operator=candidate.operator,
                                  status="feasible",
                                  weighted_delivery_seconds=joint["objective"]["weighted_delivery_seconds"],
                                  q2_weighted_delivery_seconds=(
                                      candidate.transport["objective"]["weighted_delivery_seconds"])))
            if _objective(joint) < _objective(incumbent):
                incumbent, incumbent_transport, incumbent_jobs = (
                    joint, candidate.transport, candidate.jobs)
                selected_promotion = None
                accepted[candidate.operator] += 1
                weights[candidate.operator] = min(3.0, 0.8 * weights[candidate.operator] + 0.8)
            else:
                weights[candidate.operator] = 0.8 * weights[candidate.operator] + 0.2
        if round_feasible:
            # A small bounded uphill move escapes a local minimum while the
            # best fully verified plan is always retained separately.
            next_joint, next_candidate = min(round_feasible, key=lambda item: _objective(item[0]))
            if (_objective(next_joint) < _objective(current) or
                    (next_joint["objective"]["weighted_delivery_seconds"] <=
                     1.03 * current["objective"]["weighted_delivery_seconds"]
                     and rng.random() < 0.35)):
                current, current_transport, current_jobs = (
                    next_joint, next_candidate.transport, next_candidate.jobs)
        if not selected:
            break
    incumbent["search"]["coordination"] = dict(
        method="communication-guided adaptive large-neighborhood search",
        seed=seed, rounds=rounds, trials_per_round=trials_per_round,
        promotion_trials=promotion_trials, selected_promotion=selected_promotion,
        candidates_evaluated=evaluated, feasible_candidates=sum(feasible.values()),
        attempts=dict(attempts), accepted=dict(accepted), failures=dict(failures),
        trials=trial_log,
        final_operator_weights=weights,
        baseline_objective=baseline_objective,
        selected_transport_objective=incumbent_transport["objective"],
        improvement_weighted_delivery_seconds=(
            baseline_objective["weighted_delivery_seconds"] -
            incumbent["objective"]["weighted_delivery_seconds"]),
    )
    return incumbent
