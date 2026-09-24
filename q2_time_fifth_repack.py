"""Search faster 24-sortie Q2 schedules with S004-MED moved to S005.

Replay the four route repacks, move S004-MED-01 from the six-box S004 C job
to the two-box S005 MED/WAT job, and visit S005 before S004 on that job.
The sortie count remains 24.  All route physics and schedules are recomputed.
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
from q2_time_fourth_repack import fourth_repack_jobs
from q2_time_tail_repack import DEFAULT_INPUT
from verify import verify_q2


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "results_q2_time" / "tail_repack_24_time"
S005_EARLY = {"S005-MED-01", "S005-WAT-01"}
S004_ALL = {
    "S004-FOD-01", "S004-HYG-01", "S004-MED-01",
    "S004-WAT-01", "S004-WAT-02", "S004-WAT-03",
}
MOVED_BOX = "S004-MED-01"


def fifth_repack_jobs(s: Scenario, source: dict) -> list[Job]:
    previous = fourth_repack_jobs(s, source)
    s005 = [job for job in previous if set(job.box_ids) == S005_EARLY]
    s004 = [job for job in previous if set(job.box_ids) == S004_ALL]
    if len(s005) != 1 or len(s004) != 1:
        raise ValueError("Expected original S005 MED/WAT and six-box S004 jobs")
    if s005[0].visits != (("S005", tuple(sorted(S005_EARLY))),):
        raise ValueError("S005 MED/WAT job changed")
    if s004[0].visits != (("S004", tuple(sorted(S004_ALL))),):
        raise ValueError("Six-box S004 job changed")

    jobs = []
    for job in previous:
        if job == s005[0]:
            visits = (("S005", tuple(sorted(S005_EARLY))),
                      ("S004", (MOVED_BOX,)))
        elif job == s004[0]:
            visits = (("S004", tuple(sorted(S004_ALL - {MOVED_BOX}))),)
        else:
            visits = job.visits
        jobs.append(Job(f"J{len(jobs) + 1:03d}", visits))

    counts = Counter(box_id for job in jobs for box_id in job.box_ids)
    if set(counts) != set(s.boxes) or any(count != 1 for count in counts.values()):
        raise ValueError("Fifth repack does not cover exactly the 80 original boxes")
    if len(jobs) != len(source["sorties"]) - 4:
        raise ValueError("Fifth repack changed the 24-sortie count")
    dispatch = Dispatch(s)
    for job in jobs:
        if not any(dispatch.evaluate(job, model) is not None for model in s.transport):
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
    jobs = fifth_repack_jobs(s, source)
    trial = dummy_plan(s, Dispatch(s), jobs, source["objective"]["makespan"])
    args.output.mkdir(parents=True, exist_ok=True)
    summary = {
        "source": str(args.input),
        "source_objective": source["objective"],
        "source_verification": source_check,
        "repacking": {
            "previous_four_steps": "see q2_time_fourth_repack.py",
            "moved_box": MOVED_BOX,
            "from": "six-box S004 job",
            "to": "S005 MED/WAT job, visiting S005 then S004",
            "new_job_count": len(jobs),
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
