"""Reproduce two Q2 time/frontier route neighborhoods from the repaired 24-job plan.

The 23-job variant absorbs the early S007 job into existing S006/S007 and
S013 routes, and balances the two initial S001 C loads.  The 25-job variant
spins one soft-deadline S001 hygiene box into a separate A/B-capable route.
Both variants keep every original box indivisible and delegate model, aircraft,
battery and launch-time choices to the exact fixed-route scheduler.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from model import BASE, Scenario
from q2 import Dispatch, Job
from q2_exact_schedule import solve_case
from q2_merge_search import dummy_plan, jobs_from_plan
from verify import verify_q2


ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "results_q2_time" / "frontier_24" / "makespan_plan.json"


def _matching(jobs: list[Job], box: str) -> int:
    indices = [i for i, job in enumerate(jobs) if box in job.box_ids]
    if len(indices) != 1:
        raise ValueError(f"Expected exactly one source job for {box}")
    return indices[0]


def _change_visit(job: Job, zone: str, add: set[str] = frozenset(),
                  remove: set[str] = frozenset()) -> Job:
    visits = []
    matched = False
    for current, box_ids in job.visits:
        if current == zone:
            matched = True
            boxes = set(box_ids)
            if not remove <= boxes:
                raise ValueError(f"Cannot remove {sorted(remove - boxes)} from {job.id}")
            boxes = (boxes - remove) | add
            if not boxes:
                raise ValueError(f"Empty service visit in {job.id}")
            visits.append((current, tuple(sorted(boxes))))
        else:
            visits.append((current, box_ids))
    if not matched:
        raise ValueError(f"{job.id} has no visit to {zone}")
    return Job(job.id, tuple(visits))


def frontier_jobs(s: Scenario, source: dict, variant: str) -> list[Job]:
    jobs = jobs_from_plan(source)
    hygiene = "S001-HYG-01"
    hygiene_origin = _matching(jobs, hygiene)
    jobs[hygiene_origin] = _change_visit(jobs[hygiene_origin], "S001",
                                         remove={hygiene})
    if variant == "23":
        receiver = _matching(jobs, "S001-FOD-01")
        jobs[receiver] = _change_visit(jobs[receiver], "S001", add={hygiene})

        early_s007 = _matching(jobs, "S007-MED-01")
        if set(jobs[early_s007].box_ids) != {"S007-MED-01", "S007-WAT-01"}:
            raise ValueError("S007 early job changed; cannot absorb it safely")
        c_receiver = _matching(jobs, "S006-HYG-01")
        if "S007-FOD-01" not in jobs[c_receiver].box_ids:
            raise ValueError("Expected S006-to-S007 C route")
        jobs[c_receiver] = _change_visit(jobs[c_receiver], "S007",
                                         add={"S007-WAT-01"})
        a_receiver = _matching(jobs, "S013-MED-01")
        if tuple(zone for zone, _ in jobs[a_receiver].visits) != ("S013",):
            raise ValueError("Expected a single-stop S013 early route")
        jobs[a_receiver] = Job(jobs[a_receiver].id, jobs[a_receiver].visits +
                               (("S007", ("S007-MED-01",)),))
        del jobs[early_s007]
    elif variant == "25":
        jobs.append(Job("J_EXTRA", (("S001", (hygiene,)),)))
    else:
        raise ValueError(f"Unknown variant {variant}")

    jobs = [Job(f"J{i:03d}", job.visits) for i, job in enumerate(jobs, 1)]
    counts = Counter(box for job in jobs for box in job.box_ids)
    if set(counts) != set(s.boxes) or any(count != 1 for count in counts.values()):
        raise ValueError("Repacked routes do not cover the 80 boxes exactly once")
    expected = 23 if variant == "23" else 25
    if len(jobs) != expected:
        raise ValueError(f"Expected {expected} sorties, got {len(jobs)}")
    dispatch = Dispatch(s)
    for job in jobs:
        if not any(dispatch.evaluate(job, model) is not None for model in s.transport):
            raise ValueError(f"No physically feasible model for {job.id}")
    return jobs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=BASE)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--variant", choices=("23", "25"), required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--makespan-cap", type=float)
    parser.add_argument("--weighted-cap", type=float)
    args = parser.parse_args()
    if args.seconds <= 0 or args.workers < 1:
        parser.error("seconds and workers must be positive")
    output = args.output or (ROOT / "results_q2_time" / f"frontier_{args.variant}")

    s = Scenario(args.data_dir)
    source = json.loads(args.input.read_text(encoding="utf-8"))
    source_check = verify_q2(s, source)
    jobs = frontier_jobs(s, source, args.variant)
    trial = dummy_plan(s, Dispatch(s), jobs, source["objective"]["makespan"])
    plan, solver = solve_case(s, trial, "makespan", scale=1,
                              time_limit=args.seconds, workers=args.workers,
                              seed=args.seed,
                              weighted_cap_seconds=args.weighted_cap,
                              makespan_cap=args.makespan_cap)
    report = dict(source=str(args.input), source_verification=source_check,
                  variant=args.variant, solver=solver,
                  objective=plan["objective"] if plan else None)
    output.mkdir(parents=True, exist_ok=True)
    if plan:
        report["verification"] = verify_q2(s, plan)
        (output / "plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
