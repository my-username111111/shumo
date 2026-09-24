"""Search faster 24-sortie schedules after moving S006 food to the early B job.

Replay the fifth 24-job box grouping, move S006-FOD-01 from its S006->S007 C
route to the S006 MED/WAT job, and re-evaluate all route and schedule physics.
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
from q2_time_fifth_repack import fifth_repack_jobs
from q2_time_tail_repack import DEFAULT_INPUT
from verify import verify_q2


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "results_q2_time" / "tail_repack_24_early"
S006_EARLY = {"S006-MED-01", "S006-WAT-01"}
MOVED_BOX = "S006-FOD-01"


def sixth_repack_jobs(s: Scenario, source: dict) -> list[Job]:
    previous = fifth_repack_jobs(s, source)
    early = [job for job in previous if set(job.box_ids) == S006_EARLY]
    carrier = [job for job in previous if MOVED_BOX in job.box_ids]
    if len(early) != 1 or len(carrier) != 1 or early[0] == carrier[0]:
        raise ValueError("Expected separate S006 MED/WAT and FOD carrier jobs")
    if early[0].visits != (("S006", tuple(sorted(S006_EARLY))),):
        raise ValueError("S006 MED/WAT job changed")
    if tuple(zone for zone, _ in carrier[0].visits) != ("S006", "S007"):
        raise ValueError("S006 food is no longer on an S006-to-S007 route")

    jobs = []
    for job in previous:
        if job == early[0]:
            visits = (("S006", tuple(sorted(S006_EARLY | {MOVED_BOX}))),)
        elif job == carrier[0]:
            visits = tuple((zone, tuple(box for box in box_ids if box != MOVED_BOX))
                           for zone, box_ids in job.visits)
            if any(not box_ids for _, box_ids in visits):
                raise ValueError("Food removal unexpectedly emptied a service visit")
        else:
            visits = job.visits
        jobs.append(Job(f"J{len(jobs) + 1:03d}", visits))

    counts = Counter(box_id for job in jobs for box_id in job.box_ids)
    if set(counts) != set(s.boxes) or any(count != 1 for count in counts.values()):
        raise ValueError("Sixth repack does not cover exactly the 80 original boxes")
    if len(jobs) != len(source["sorties"]) - 4:
        raise ValueError("Sixth repack changed the 24-sortie count")
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
    jobs = sixth_repack_jobs(s, source)
    trial = dummy_plan(s, Dispatch(s), jobs, source["objective"]["makespan"])
    args.output.mkdir(parents=True, exist_ok=True)
    summary = {
        "source": str(args.input),
        "source_objective": source["objective"],
        "source_verification": source_check,
        "repacking": {
            "previous_five_steps": "see q2_time_fifth_repack.py",
            "moved_box": MOVED_BOX,
            "from": "S006-to-S007 C job",
            "to": "S006 MED/WAT B-capable job",
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
