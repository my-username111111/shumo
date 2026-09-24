"""Search a 26-sortie Q2 plan by eliminating two late single-purpose routes.

Apply the verified T020 tail repack from q2_time_tail_repack, then combine the
S009-WAT-01 and S009-FOD-01 jobs into one S009 visit.  CP-SAT reschedules the
fixed 26 box groups with the original DEM, energy and resource model.
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
from q2_time_tail_repack import DEFAULT_INPUT, repack_jobs
from verify import verify_q2


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "results_q2_time" / "tail_repack_26"


def second_repack_jobs(s: Scenario, source: dict) -> list[Job]:
    previous = repack_jobs(s, source)
    water_id, food_id = "S009-WAT-01", "S009-FOD-01"
    water = [job for job in previous if water_id in job.box_ids]
    food = [job for job in previous if food_id in job.box_ids]
    if len(water) != 1 or len(food) != 1 or water[0] == food[0]:
        raise ValueError("Expected separate S009 water and food jobs")
    if water[0].box_ids != (water_id,) or food[0].box_ids != (food_id,):
        raise ValueError("S009 jobs no longer match the expected single-box routes")

    kept = [job for job in previous if job not in (water[0], food[0])]
    kept.append(Job("S009-WAT-FOD", (("S009", (food_id, water_id)),)))
    jobs = [Job(f"J{k:03d}", job.visits) for k, job in enumerate(kept, 1)]
    counts = Counter(box_id for job in jobs for box_id in job.box_ids)
    if set(counts) != set(s.boxes) or any(count != 1 for count in counts.values()):
        raise ValueError("Second repack does not cover exactly the 80 original boxes")
    if len(jobs) != len(source["sorties"]) - 2:
        raise ValueError("Second repack did not remove exactly two sorties")
    dispatch = Dispatch(s)
    for job in jobs:
        if not any(dispatch.evaluate(job, model) is not None for model in s.transport):
            raise ValueError(f"No physically feasible aircraft model for {job.id}")
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
    jobs = second_repack_jobs(s, source)
    trial = dummy_plan(s, Dispatch(s), jobs, source["objective"]["makespan"])
    args.output.mkdir(parents=True, exist_ok=True)
    summary = {
        "source": str(args.input),
        "source_objective": source["objective"],
        "source_verification": source_check,
        "repacking": {
            "removed_original_sorties": ["T020", "T027"],
            "S003-WAT-01": "moved from T020 to original T018",
            "S015-WAT-01": "moved from T020 to original T026",
            "S009-WAT-01_and_S009-FOD-01": "combined into one S009 job",
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
