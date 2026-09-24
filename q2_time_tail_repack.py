"""Repack the latest-return Q2 sortie and reschedule the resulting 27 jobs.

Starting from the verified 28-sortie timely plan, move S003-WAT-01 from T020
to T018 and S015-WAT-01 from T020 to T026.  Then delete the now-empty T020.
All aircraft, battery, model and launch-time decisions are recomputed by the
fixed-route CP-SAT scheduler and independently checked by verify_q2.
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
from verify import verify_q2


ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "results_q2_extended" / "plans" / "28_earliest.json"
DEFAULT_OUTPUT = ROOT / "results_q2_time" / "tail_repack_27"


def repack_jobs(s: Scenario, source: dict) -> list[Job]:
    """Change only the named three source jobs; retain every other box group."""
    source_by_id = {row["id"]: row for row in source["sorties"]}
    required = {"T018", "T020", "T026"}
    if not required <= set(source_by_id):
        raise ValueError(f"Source plan is missing {sorted(required - source_by_id.keys())}")
    expected_tail = {"S003-WAT-01", "S015-WAT-01"}
    if set(source_by_id["T020"]["route"]["box_ids"]) != expected_tail:
        raise ValueError("T020 is no longer the expected two-box tail route")

    jobs = []
    for row in source["sorties"]:
        if row["id"] == "T020":
            continue
        visits = []
        for zone, box_ids in row["visits"]:
            boxes = list(box_ids)
            if row["id"] == "T018" and zone == "S003":
                boxes.append("S003-WAT-01")
            if row["id"] == "T026" and zone == "S015":
                boxes.append("S015-WAT-01")
            visits.append((zone, tuple(sorted(boxes))))
        jobs.append(Job(f"J{len(jobs) + 1:03d}", tuple(visits)))

    counts = Counter(box_id for job in jobs for box_id in job.box_ids)
    if set(counts) != set(s.boxes) or any(count != 1 for count in counts.values()):
        raise ValueError("Repacked jobs do not cover exactly the 80 original boxes")
    if len(jobs) != len(source["sorties"]) - 1:
        raise ValueError("Repacking failed to remove exactly one sortie")
    dispatch = Dispatch(s)
    for job in jobs:
        if not any(dispatch.evaluate(job, model) is not None for model in s.transport):
            raise ValueError(f"Repacked job {job.id} has no physically feasible model")
    return jobs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=BASE)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args()
    if args.seconds <= 0 or args.workers < 1:
        parser.error("seconds and workers must be positive")

    s = Scenario(args.data_dir)
    source = json.loads(args.input.read_text(encoding="utf-8"))
    source_check = verify_q2(s, source)
    jobs = repack_jobs(s, source)
    dispatcher = Dispatch(s)
    trial = dummy_plan(s, dispatcher, jobs, source["objective"]["makespan"])
    args.output.mkdir(parents=True, exist_ok=True)

    experiments = (
        ("dominance", source["objective"]["weighted_delivery_seconds"]),
        ("fastest", None),
    )
    summary = {
        "source": str(args.input),
        "source_objective": source["objective"],
        "source_verification": source_check,
        "repacking": {
            "removed_source_sortie": "T020",
            "moved_boxes": {
                "S003-WAT-01": "T018 source job",
                "S015-WAT-01": "T026 source job",
            },
            "new_job_count": len(jobs),
        },
        "experiments": {},
    }
    for name, weighted_cap in experiments:
        plan, report = solve_case(
            s, trial, "makespan", scale=1, time_limit=args.seconds,
            workers=args.workers, seed=args.seed,
            weighted_cap_seconds=weighted_cap,
            makespan_cap=source["objective"]["makespan"] + 1.0,
        )
        if plan is not None:
            check = verify_q2(s, plan)
            (args.output / f"{name}_plan.json").write_text(
                json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
            summary["experiments"][name] = {
                "objective": plan["objective"], "verification": check,
                "solver": report,
            }
        else:
            summary["experiments"][name] = {"objective": None, "solver": report}
        (args.output / f"{name}_solver.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"experiment": name, **summary["experiments"][name]},
                         ensure_ascii=False), flush=True)
    (args.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
