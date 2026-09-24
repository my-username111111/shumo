"""Resource-aware route repair and execution-order refinement for Q2.

The route search uses incremental energy to rank proposals, then replays every
accepted candidate with physical aircraft, batteries, charging and deadlines.
It is a finite feasible-solution search, not an optimality proof.
"""

from __future__ import annotations

from random import Random

from model import distance_m
from q2_alns import _insert_box, _remove_box, jobs_from_plan


def _route_min_energy(dispatcher, job) -> float | None:
    energies = [route["energy_kwh"] for model in dispatcher.s.transport
                if (route := dispatcher.evaluate(job, model)) is not None]
    return min(energies) if energies else None


def _zone_distance(dispatcher, zone: str, job) -> float:
    a = dispatcher.s.nodes[zone]
    return min(distance_m((a.lon, a.lat),
                          (dispatcher.s.nodes[z].lon, dispatcher.s.nodes[z].lat))
               for z, _ in job.visits)


def _candidate_pool(dispatcher, plan: dict, job_type, max_targets: int = 9
                    ) -> tuple[list[tuple], int]:
    jobs = jobs_from_plan(plan, job_type)
    rows = plan["sorties"]
    energy = plan["objective"]["energy_kwh"]
    pool = {}
    evaluated = 0

    def offer(changes: list[tuple[int, object]], removed: list[int],
              old_indices: list[int], label: str) -> None:
        nonlocal evaluated
        evaluated += 1
        route_energy = 0.0
        for _, changed in changes:
            value = _route_min_energy(dispatcher, changed)
            if value is None:
                return
            route_energy += value
        # This is a ranking estimate: unchanged routes retain their current
        # energy, while changed routes use the cheapest compatible model.
        # A later reschedule may also change an unchanged route's model.
        bound = (energy - sum(rows[i]["energy_kwh"] for i in old_indices)
                 + route_energy)
        if bound >= energy - 0.00001:
            return
        proposal = jobs[:]
        for index, changed in changes:
            proposal[index] = changed
        for index in sorted(removed, reverse=True):
            proposal.pop(index)
        signature = tuple(job.visits for job in proposal)
        if signature not in pool or bound < pool[signature][0]:
            pool[signature] = (bound, len(proposal), proposal, label)

    for i, job in enumerate(jobs):
        for bid in job.box_ids:
            stripped = _remove_box(job, bid)
            zone = dispatcher.s.boxes[bid].zone
            targets = [j for j in range(len(jobs)) if j != i]
            targets.sort(key=lambda j: (_zone_distance(dispatcher, zone, jobs[j]), j))
            for j in targets[:max_targets]:
                for position in range(len(jobs[j].visits) + 1):
                    inserted = _insert_box(jobs[j], bid, zone, position)
                    changes = [(j, inserted)]
                    if stripped is not None:
                        changes.append((i, stripped))
                    offer(changes, [i] if stripped is None else [], [i, j], "relocate")

    for i in range(len(jobs)):
        for j in range(i + 1, len(jobs)):
            for sequence in ((jobs[i], jobs[j]), (jobs[j], jobs[i])):
                zones = []
                batches = {}
                for job in sequence:
                    for zone, boxes in job.visits:
                        if zone not in batches:
                            zones.append(zone)
                            batches[zone] = []
                        batches[zone].extend(boxes)
                merged = job_type(jobs[i].id,
                                  tuple((z, tuple(sorted(batches[z]))) for z in zones))
                offer([(i, merged)], [j], [i, j], "merge")

    for i, job in enumerate(jobs):
        if len(job.visits) < 2:
            continue
        for a in range(len(job.visits)):
            for b in range(a + 1, len(job.visits)):
                visits = list(job.visits)
                visits[a], visits[b] = visits[b], visits[a]
                offer([(i, job_type(job.id, tuple(visits)))], [], [i], "reorder")

    return sorted(pool.values(), key=lambda row: (row[0], row[1])), evaluated


def _admissible(scenario, plan: dict, delivery_cap: float,
                makespan_cap: float | None = None,
                minimum_hard_slack: float = 0.0) -> bool:
    obj = plan["objective"]
    if (obj["weighted_soft_delay_seconds"] > 1e-7 or
            obj["weighted_delivery_seconds"] > delivery_cap + 1e-7 or
            (makespan_cap is not None and obj["makespan"] > makespan_cap + 1e-7)):
        return False
    if minimum_hard_slack:
        return all(scenario.boxes[bid].hard_deadline - delivery["time"] >= minimum_hard_slack
                   for bid, delivery in plan["deliveries"].items()
                   if scenario.boxes[bid].hard_deadline is not None)
    return True


def improve_energy(dispatcher, start: dict, job_type, delivery_cap: float,
                   makespan_cap: float | None = None, max_rounds: int = 40,
                   max_schedules: int = 1000) -> tuple[dict, list[dict], dict]:
    current = start
    snapshots = []
    statistics = dict(route_candidates=0, complete_schedules=0, admissible=0,
                      accepted=0)
    for _ in range(max_rounds):
        pool, count = _candidate_pool(dispatcher, current, job_type)
        statistics["route_candidates"] += count
        improved = None
        for bound, _, jobs, operator in pool[:max_schedules]:
            if bound >= current["objective"]["energy_kwh"] - 1e-7:
                continue
            statistics["complete_schedules"] += 1
            trial = dispatcher._schedule_order(jobs)
            if trial is None or not _admissible(dispatcher.s, trial, delivery_cap,
                                                makespan_cap):
                continue
            statistics["admissible"] += 1
            old = current["objective"]
            new = trial["objective"]
            if (new["energy_kwh"], new["sortie_count"], new["makespan"],
                    new["weighted_delivery_seconds"]) < (
                    old["energy_kwh"], old["sortie_count"], old["makespan"],
                    old["weighted_delivery_seconds"]):
                improved = trial
                snapshots.append(dict(operator=operator, plan=trial))
                statistics["accepted"] += 1
                break
        if improved is None:
            break
        current = improved
    return current, snapshots, statistics


def refine_order(dispatcher, start: dict, job_type, delivery_cap: float,
                 iterations: int, makespan_cap: float | None = None,
                 minimum_hard_slack: float = 0.0, seed: int = 20260924
                 ) -> tuple[dict, dict]:
    order = jobs_from_plan(start, job_type)
    current = start
    energy_cap = start["objective"]["energy_kwh"]
    rng = Random(seed)
    statistics = dict(evaluated=0, admissible=0, accepted=0)

    def score(plan: dict) -> tuple[float, float, float]:
        obj = plan["objective"]
        return (obj["makespan"], obj["weighted_delivery_seconds"], obj["energy_kwh"])

    for _ in range(iterations):
        proposal = order[:]
        i, j = rng.sample(range(len(proposal)), 2)
        if rng.random() < 0.6:
            proposal.insert(j, proposal.pop(i))
        else:
            proposal[i], proposal[j] = proposal[j], proposal[i]
        trial = dispatcher._schedule_order(proposal)
        statistics["evaluated"] += 1
        if (trial is None or not _admissible(dispatcher.s, trial, delivery_cap,
                                            makespan_cap, minimum_hard_slack) or
                trial["objective"]["energy_kwh"] > energy_cap + 1e-7):
            continue
        statistics["admissible"] += 1
        if score(trial) < score(current):
            current, order = trial, proposal
            statistics["accepted"] += 1
    return current, statistics


def solve_refinements(dispatcher, main_plan: dict, alns_plan: dict, job_type
                      ) -> tuple[dict, dict, dict, dict]:
    """Reproduce the fast, service-first and lowest-energy Q2 alternatives."""
    delivery_cap = main_plan["objective"]["weighted_delivery_seconds"] * 1.03
    time_cap = alns_plan["objective"]["makespan"]

    initial_energy, snapshots, initial_stats = improve_energy(
        dispatcher, alns_plan, job_type, delivery_cap)
    if not snapshots:
        raise ValueError("Energy refinement found no candidate from ALNS seed")
    first_improvement = snapshots[0]["plan"]

    fast_routes, _, fast_stats = improve_energy(
        dispatcher, first_improvement, job_type, delivery_cap, makespan_cap=time_cap)
    quick, quick_order_stats = refine_order(
        dispatcher, fast_routes, job_type, delivery_cap, iterations=10000,
        makespan_cap=time_cap, minimum_hard_slack=60.0)

    timely, timely_order_stats = refine_order(
        dispatcher, initial_energy, job_type, delivery_cap, iterations=15000)
    energy, _, energy_stats = improve_energy(
        dispatcher, timely, job_type, delivery_cap)

    report = dict(policy="incremental_energy_repair_and_resource_order",
                  delivery_cap=delivery_cap, fast_makespan_cap=time_cap,
                  fast_minimum_hard_slack_s=60.0,
                  initial_energy_search=initial_stats,
                  quick_route_search=fast_stats,
                  quick_order_search=quick_order_stats,
                  timely_order_search=timely_order_stats,
                  energy_route_search=energy_stats,
                  quick_objective=quick["objective"],
                  timely_objective=timely["objective"],
                  energy_objective=energy["objective"])
    return quick, timely, energy, report
