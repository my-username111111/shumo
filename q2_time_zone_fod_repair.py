"""Test single-zone B routes in place of crossed S010/S014 food deliveries.

Replay the six approved Q2 time-route repacks, then put S010-FOD-01 with the
S010 MED/WAT boxes and S014-FOD-01 with the S014 MED/WAT boxes.  The number of
sorties remains 24.  This exploration uses the common route physics, CP-SAT
aircraft/battery scheduler and independent Q2 verifier.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from model import BASE, Scenario
from q2 import Dispatch, Job
from q2_exact_schedule import solve_case
from q2_merge_search import dummy_plan
from q2_time_sixth_repack import sixth_repack_jobs
from q2_time_tail_repack import DEFAULT_INPUT
from verify import verify_q2


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "results_q2_time" / "zone_fod_repair_24"
OLD_S010 = {"S010-MED-01", "S010-WAT-01", "S014-FOD-01"}
OLD_S014 = {"S014-MED-01", "S014-WAT-01", "S010-FOD-01"}
NEW_S010 = {"S010-MED-01", "S010-WAT-01", "S010-FOD-01"}
NEW_S014 = {"S014-MED-01", "S014-WAT-01", "S014-FOD-01"}


def repair_jobs(s: Scenario, source: dict) -> list[Job]:
    previous = sixth_repack_jobs(s, source)
    s010 = [job for job in previous if set(job.box_ids) == OLD_S010]
    s014 = [job for job in previous if set(job.box_ids) == OLD_S014]
    if len(s010) != 1 or len(s014) != 1:
        raise ValueError("Expected the crossed S010/S014 food route pair")
    jobs = []
    for job in previous:
        if job == s010[0]:
            visits = (("S010", tuple(sorted(NEW_S010))),)
        elif job == s014[0]:
            visits = (("S014", tuple(sorted(NEW_S014))),)
        else:
            visits = job.visits
        jobs.append(Job(f"J{len(jobs) + 1:03d}", visits))
    counts = Counter(box_id for job in jobs for box_id in job.box_ids)
    if set(counts) != set(s.boxes) or any(count != 1 for count in counts.values()):
        raise ValueError("S010/S014 repair does not cover exactly the 80 boxes")
    if len(jobs) != 24:
        raise ValueError("S010/S014 repair changed the sortie count")
    dispatcher = Dispatch(s)
    for job in jobs:
        if not any(dispatcher.evaluate(job, model) is not None for model in s.transport):
            raise ValueError(f"No physically feasible model for {job.id}")
    return jobs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=BASE)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seconds", type=float, default=45.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args()
    if args.seconds <= 0 or args.workers < 1:
        parser.error("seconds and workers must be positive")

    s = Scenario(args.data_dir)
    source = json.loads(args.input.read_text(encoding="utf-8"))
    source_check = verify_q2(s, source)
    jobs = repair_jobs(s, source)
    trial = dummy_plan(s, Dispatch(s), jobs, source["objective"]["makespan"])
    args.output.mkdir(parents=True, exist_ok=True)
    summary = {
        "source": str(args.input),
        "source_objective": source["objective"],
        "source_verification": source_check,
        "route_change": {
            "old": [sorted(OLD_S010), sorted(OLD_S014)],
            "new": [sorted(NEW_S010), sorted(NEW_S014)],
            "job_count": len(jobs),
        },
        "experiments": {},
    }
    for name, weighted_cap in (
        ("dominance", source["objective"]["weighted_delivery_seconds"]),
        ("fastest", None),
    ):
        plan, report = solve_case(
            s, trial, "makespan", scale=1,
            time_limit=args.seconds, workers=args.workers, seed=args.seed,
            weighted_cap_seconds=weighted_cap,
            makespan_cap=source["objective"]["makespan"] + 1.0,
        )
        record = {"objective": None, "solver": report}
        if plan is not None:
            check = verify_q2(s, plan)
            (args.output / f"{name}_plan.json").write_text(
                json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
            record.update(objective=plan["objective"], verification=check)
        summary["experiments"][name] = record
        (args.output / f"{name}_solver.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"experiment": name, **record}, ensure_ascii=False), flush=True)
    (args.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
