"""Two-stage adaptive large-neighborhood search for Q2 route sets.

The first stage protects service quality (soft delay, then weighted delivery).
The second stage keeps zero soft delay and a bounded weighted-delivery loss while
reducing energy, sortie count and completion time.  Every candidate is replayed
by Q2's aircraft/battery scheduler before it can be accepted.
"""

from __future__ import annotations

import math
from random import Random


def _make(job, visits, ident=None):
    return job.__class__(ident or job.id, tuple(visits))


def _renumber(jobs):
    return [_make(job, job.visits, f"J{i:03d}") for i, job in enumerate(jobs, 1)]


def jobs_from_plan(plan, job_type):
    return [job_type(f"J{i:03d}", tuple((z, tuple(bs)) for z, bs in row["visits"]))
            for i, row in enumerate(plan["sorties"], 1)]


def _remove_box(job, bid):
    visits = tuple((z, tuple(b for b in bs if b != bid))
                   for z, bs in job.visits if any(b != bid for b in bs))
    return _make(job, visits) if visits else None


def _insert_box(job, bid, zone, position):
    visits = list(job.visits)
    same = next((i for i, (z, _) in enumerate(visits) if z == zone), None)
    if same is None:
        visits.insert(min(position, len(visits)), (zone, (bid,)))
    else:
        z, boxes = visits[same]
        visits[same] = (z, tuple(sorted((*boxes, bid))))
    return _make(job, visits)


def _locally_feasible(dispatcher, job):
    return bool(job.visits) and any(dispatcher.evaluate(job, model) is not None
                                    for model in dispatcher.s.transport)


def _relocate(jobs, rng, dispatcher):
    source = rng.randrange(len(jobs))
    bid = rng.choice(jobs[source].box_ids)
    stripped = _remove_box(jobs[source], bid)
    targets = [i for i in range(len(jobs)) if i != source]
    rng.shuffle(targets)
    zone = dispatcher.s.boxes[bid].zone
    for target in targets[:8]:
        positions = list(range(len(jobs[target].visits) + 1))
        rng.shuffle(positions)
        for position in positions:
            changed = _insert_box(jobs[target], bid, zone, position)
            if not _locally_feasible(dispatcher, changed):
                continue
            proposal = jobs[:]
            proposal[target] = changed
            if stripped is None:
                proposal.pop(source)
            else:
                proposal[source] = stripped
            return _renumber(proposal)
    return None


def _exchange(jobs, rng, dispatcher):
    if len(jobs) < 2:
        return None
    i, j = rng.sample(range(len(jobs)), 2)
    a, b = rng.choice(jobs[i].box_ids), rng.choice(jobs[j].box_ids)
    left0, right0 = _remove_box(jobs[i], a), _remove_box(jobs[j], b)
    if left0 is None or right0 is None:
        return None
    left = _insert_box(left0, b, dispatcher.s.boxes[b].zone,
                       rng.randrange(len(left0.visits) + 1))
    right = _insert_box(right0, a, dispatcher.s.boxes[a].zone,
                        rng.randrange(len(right0.visits) + 1))
    if not _locally_feasible(dispatcher, left) or not _locally_feasible(dispatcher, right):
        return None
    proposal = jobs[:]
    proposal[i], proposal[j] = left, right
    return _renumber(proposal)


def _merge(jobs, rng, dispatcher):
    if len(jobs) < 2:
        return None
    i, j = sorted(rng.sample(range(len(jobs)), 2))
    order = [jobs[i], jobs[j]] if rng.random() < 0.5 else [jobs[j], jobs[i]]
    zones, batches = [], {}
    for job in order:
        for zone, boxes in job.visits:
            if zone not in batches:
                zones.append(zone)
                batches[zone] = []
            batches[zone].extend(boxes)
    combined = _make(jobs[i], ((z, tuple(sorted(batches[z]))) for z in zones), "M")
    if not _locally_feasible(dispatcher, combined):
        return None
    return _renumber([job for k, job in enumerate(jobs) if k not in (i, j)] + [combined])


def _split(jobs, rng, dispatcher):
    choices = [i for i, job in enumerate(jobs) if len(job.box_ids) >= 2]
    if not choices:
        return None
    i = rng.choice(choices)
    ids = list(jobs[i].box_ids)
    rng.shuffle(ids)
    cut = rng.randrange(1, len(ids))
    made = []
    for part in (set(ids[:cut]), set(ids[cut:])):
        visits = tuple((z, tuple(b for b in boxes if b in part))
                       for z, boxes in jobs[i].visits if any(b in part for b in boxes))
        job = _make(jobs[i], visits, "S")
        if not _locally_feasible(dispatcher, job):
            return None
        made.append(job)
    return _renumber(jobs[:i] + made + jobs[i + 1:])


def _reorder(jobs, rng, dispatcher):
    choices = [i for i, job in enumerate(jobs) if len(job.visits) >= 2]
    if not choices:
        return None
    i = rng.choice(choices)
    visits = list(jobs[i].visits)
    rng.shuffle(visits)
    changed = _make(jobs[i], visits)
    if changed.visits == jobs[i].visits or not _locally_feasible(dispatcher, changed):
        return None
    proposal = jobs[:]
    proposal[i] = changed
    return _renumber(proposal)


def _ruin_recreate(jobs, rng, dispatcher):
    proposal = jobs[:]
    all_ids = [bid for job in proposal for bid in job.box_ids]
    removed = rng.sample(all_ids, min(rng.randint(2, 4), len(all_ids)))
    for bid in removed:
        i = next(k for k, job in enumerate(proposal) if bid in job.box_ids)
        stripped = _remove_box(proposal[i], bid)
        if stripped is None:
            proposal.pop(i)
        else:
            proposal[i] = stripped
    rng.shuffle(removed)
    for bid in removed:
        zone = dispatcher.s.boxes[bid].zone
        candidates = []
        for i, job in enumerate(proposal):
            for position in range(len(job.visits) + 1):
                changed = _insert_box(job, bid, zone, position)
                routes = [dispatcher.evaluate(changed, m) for m in dispatcher.s.transport]
                routes = [route for route in routes if route is not None]
                if routes:
                    candidates.append((min(route["energy_kwh"] for route in routes),
                                       rng.random(), i, changed))
        single = _make(jobs[0], ((zone, (bid,)),), "N")
        routes = [dispatcher.evaluate(single, m) for m in dispatcher.s.transport]
        routes = [route for route in routes if route is not None]
        if routes:
            candidates.append((min(route["energy_kwh"] for route in routes) + 0.05,
                               rng.random(), len(proposal), single))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]))
        chosen = candidates[0] if rng.random() < 0.75 else rng.choice(
            candidates[:min(4, len(candidates))])
        if chosen[2] == len(proposal):
            proposal.append(chosen[3])
        else:
            proposal[chosen[2]] = chosen[3]
    return _renumber(proposal)


OPERATORS = {
    "relocate": _relocate,
    "exchange": _exchange,
    "merge": _merge,
    "split": _split,
    "reorder": _reorder,
    "ruin_recreate": _ruin_recreate,
}


def _valid_partition(jobs, scenario):
    ids = [bid for job in jobs for bid in job.box_ids]
    return len(ids) == len(set(ids)) and set(ids) == set(scenario.boxes)


def _stage1_key(plan):
    obj = plan["objective"]
    return (obj["weighted_soft_delay_seconds"], obj["weighted_delivery_seconds"],
            obj["makespan"], obj["energy_kwh"], obj["sortie_count"])


def _stage2_key(plan):
    obj = plan["objective"]
    return (obj["energy_kwh"], obj["sortie_count"], obj["makespan"],
            obj["weighted_delivery_seconds"])


def _search(dispatcher, start_jobs, start_plan, iterations, stage, seed, delivery_cap=None):
    rng = Random(seed)
    current_jobs, current = start_jobs, start_plan
    best_jobs, best = start_jobs, start_plan
    weights = {name: 1.0 for name in OPERATORS}
    rewards = {name: 0.0 for name in OPERATORS}
    uses = {name: 0 for name in OPERATORS}
    feasible = accepted = invalid = improved = 0
    temperature = 70000.0 if stage == 1 else 120000.0

    def value(plan):
        obj = plan["objective"]
        if stage == 1:
            return (obj["weighted_soft_delay_seconds"] * 1e7 +
                    obj["weighted_delivery_seconds"] + obj["makespan"] * 0.01)
        return obj["energy_kwh"] * 1e6 + obj["sortie_count"] * 1e4 + obj["makespan"]

    key = _stage1_key if stage == 1 else _stage2_key
    for iteration in range(1, iterations + 1):
        if iteration > 1 and (iteration - 1) % 50 == 0:
            for name in OPERATORS:
                observed = rewards[name] / max(1, uses[name])
                weights[name] = max(0.15, 0.75 * weights[name] + 0.25 * (0.5 + observed))
                rewards[name] = 0.0
                uses[name] = 0
        names = list(OPERATORS)
        name = rng.choices(names, weights=[weights[n] for n in names], k=1)[0]
        uses[name] += 1
        candidate_jobs = OPERATORS[name](current_jobs, rng, dispatcher)
        if candidate_jobs is None or not _valid_partition(candidate_jobs, dispatcher.s):
            invalid += 1
            temperature *= 0.996
            continue
        try:
            candidate = dispatcher.schedule(candidate_jobs, attempts=2, seed=seed + iteration)
        except ValueError:
            invalid += 1
            temperature *= 0.996
            continue
        feasible += 1
        obj = candidate["objective"]
        if (stage == 2 and (obj["weighted_soft_delay_seconds"] > 1e-7 or
                            obj["weighted_delivery_seconds"] > delivery_cap)):
            temperature *= 0.996
            continue
        delta = value(candidate) - value(current)
        if delta <= 0 or rng.random() < math.exp(-delta / max(temperature, 1e-9)):
            current_jobs, current = candidate_jobs, candidate
            accepted += 1
            rewards[name] += 2.0
        if key(candidate) < key(best):
            best_jobs, best = candidate_jobs, candidate
            improved += 1
            rewards[name] += 8.0
        temperature *= 0.996
    return best_jobs, best, dict(iterations=iterations, feasible=feasible,
                                 accepted=accepted, invalid=invalid, improved=improved,
                                 final_weights=weights)


def solve_two_stage_alns(dispatcher, main_plan, zero_delay_plan, job_type,
                         stage1_iterations=500, stage2_iterations=1200,
                         seeds=(260924, 260925, 260926), delivery_ratio=1.03):
    """Return a protected main plan and a zero-delay resource-efficient plan."""
    stage1_jobs, stage1, stage1_stats = _search(
        dispatcher, jobs_from_plan(main_plan, job_type), main_plan,
        stage1_iterations, stage=1, seed=260923)
    delivery_cap = stage1["objective"]["weighted_delivery_seconds"] * delivery_ratio
    stage2_runs = []
    for seed in seeds:
        jobs, plan, stats = _search(
            dispatcher, jobs_from_plan(zero_delay_plan, job_type), zero_delay_plan,
            stage2_iterations, stage=2, seed=seed, delivery_cap=delivery_cap)
        stage2_runs.append((jobs, plan, dict(seed=seed, **stats)))
    _, stage2, selected_stats = min(stage2_runs, key=lambda item: _stage2_key(item[1]))
    return stage1, stage2, dict(
        policy="stage1_service_then_stage2_zero_delay_energy",
        delivery_ratio=delivery_ratio,
        delivery_cap=delivery_cap,
        stage1=stage1_stats,
        stage2_selected=selected_stats,
        stage2_runs=[dict(objective=plan["objective"], stats=stats)
                     for _, plan, stats in stage2_runs],
    )
