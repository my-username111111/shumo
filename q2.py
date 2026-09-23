"""Question 2: multi-sortie dispatch with explicit aircraft and battery events.

This is a feasible-solution search, not a claim of global optimality.  It uses
exact Q1 partitions for residual cargo, then tries route merges and reschedules
every candidate.  Every accepted sortie is evaluated by the common model.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from random import Random

from model import Scenario
from q1 import solve_zone


@dataclass(frozen=True)
class Job:
    id: str
    visits: tuple[tuple[str, tuple[str, ...]], ...]

    @property
    def box_ids(self) -> tuple[str, ...]:
        return tuple(b for _, boxes in self.visits for b in boxes)


class Dispatch:
    def __init__(self, scenario: Scenario):
        self.s = scenario

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
        hard = [b.hard_deadline for b in boxes if b.hard_deadline is not None]
        best_duration = min((ev["duration"] for g in self.s.transport
                             if (ev := self.evaluate(job, g)) is not None), default=float("inf"))
        return (min(hard) if hard else 1_000_000 + min(b.desired for b in boxes),
                -best_duration, -sum(b.priority for b in boxes), job.id)

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
                        # Completion is primary within this job.  A small energy
                        # tie-break prevents needlessly dispatching the heavy C.
                        score = (weighted + 100 * route["energy_kwh"],
                                 start + route["duration"], drone, battery)
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
            obj = result["objective"]
            score = (obj["weighted_delivery_seconds"], obj["makespan"],
                     obj["energy_kwh"], obj["sortie_count"])
            if best is None or score < best[0]:
                best = (score, result)
        if best is None:
            raise ValueError("Unable to schedule all hard-deadline cargo with current route set")
        return best[1]

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

    def improve_merges(self, jobs: list[Job], baseline: dict, max_rounds: int = 8) -> tuple[list[Job], dict]:
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
                        obj = trial["objective"]
                        if (obj["weighted_delivery_seconds"] > 1.05 * base_time or
                                obj["makespan"] > 1.10 * base_makespan):
                            continue
                        if obj["energy_kwh"] >= current["objective"]["energy_kwh"] - 1e-8:
                            continue
                        score = (obj["energy_kwh"], obj["weighted_delivery_seconds"], obj["makespan"])
                        if best is None or score < best[0]:
                            best = (score, proposal, trial)
            if best is None:
                break
            _, current_jobs, current = best
        return current_jobs, current


def solve(s: Scenario, improve: bool = True) -> dict:
    dispatcher = Dispatch(s)
    jobs = dispatcher.initial_jobs()
    base = dispatcher.schedule(jobs)
    if improve:
        jobs, result = dispatcher.improve_merges(jobs, base)
    else:
        result = base
    result["search"] = dict(initial_sorties=base["objective"]["sortie_count"],
                            initial_energy_kwh=base["objective"]["energy_kwh"],
                            merge_rounds=min(8, base["objective"]["sortie_count"] - len(jobs)))
    return result
