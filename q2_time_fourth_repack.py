"""Search 24-sortie Q2 schedules after consolidating all S011 cargo.

Replay the first three independently verified repacks, then merge the original
S011 MED/WAT sortie and S011 FOD sortie into one three-box S011 job.  The fixed
job groups are scheduled with the common DEM, aircraft and battery model.
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
from q2_time_tail_repack import DEFAULT_INPUT
from q2_time_third_repack import third_repack_jobs
from verify import verify_q2


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "results_q2_time" / "tail_repack_24"
S011_EARLY = {"S011-MED-01", "S011-WAT-01"}
S011_LATE = {"S011-FOD-01"}


def fourth_repack_jobs(s: Scenario, source: dict) -> list[Job]:
    previous = third_repack_jobs(s, source)
    early = [job for job in previous if set(job.box_ids) == S011_EARLY]
    late = [job for job in previous if set(job.box_ids) == S011_LATE]
    if len(early) != 1 or len(late) != 1:
        raise ValueError("Expected separate original S011 MED/WAT and FOD jobs")
    if early[0].visits != (("S011", tuple(sorted(S011_EARLY))),):
        raise ValueError("S011 MED/WAT source job changed")
    if late[0].visits != (("S011", ("S011-FOD-01",)),):
        raise ValueError("S011 FOD source job changed")

    kept = [job for job in previous if job not in (early[0], late[0])]
    kept.append(Job("S011-ALL", (("S011", tuple(sorted(S011_EARLY | S011_LATE))),)))
    jobs = [Job(f"J{k:03d}", job.visits) for k, job in enumerate(kept, 1)]
    counts = Counter(box_id for job in jobs for box_id in job.box_ids)
    if set(counts) != set(s.boxes) or any(count != 1 for count in counts.values()):
        raise ValueError("Fourth repack does not cover exactly the 80 original boxes")
    if len(jobs) != len(source["sorties"]) - 4:
        raise ValueError("Fourth repack did not remove exactly four sorties")
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
    jobs = fourth_repack_jobs(s, source)
    trial = dummy_plan(s, Dispatch(s), jobs, source["objective"]["makespan"])
    args.output.mkdir(parents=True, exist_ok=True)
    summary = {
        "source": str(args.input),
        "source_objective": source["objective"],
        "source_verification": source_check,
        "repacking": {
            "removed_original_sorties": ["T020", "T027", "T017", "T028"],
            "S003-WAT-01": "moved from original T020 to T018",
            "S015-WAT-01": "moved from original T020 to T026",
            "S009-WAT-01_and_S009-FOD-01": "combined into one S009 job",
            "S004-MED-01_and_S004-WAT-01": "absorbed into original four-box S004 C job",
            "S011-MED-01_WAT-01_FOD-01": "combined into one S011 job",
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
