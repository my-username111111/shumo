"""Probe every single-box move out of the critical S004 C sortie.

The source is the independently verified 24-sortie S010/S014 single-zone plan.
Each candidate preserves 24 jobs and all 80 boxes.  A move is kept only if
the receiving and remaining routes pass the physical evaluator and relative
delivery windows.  CP-SAT then checks a strictly better completion-time cap.
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
DEFAULT_OUTPUT = ROOT / "results_q2_time" / "s004_tail_probe"


def insert_box(visits: tuple, box_id: str, zone: str) -> list[tuple]:
    if zone in [z for z, _ in visits]:
        return [tuple((z, tuple(sorted((*boxes, box_id))) if z == zone else boxes)
                      for z, boxes in visits)]
    return [(*visits[:position], (zone, (box_id,)), *visits[position:])
            for position in range(len(visits) + 1)]


def relative_deadlines_ok(s: Scenario, route: dict) -> bool:
    return all(relative <= (s.boxes[box_id].hard_deadline
                            if s.boxes[box_id].hard_deadline is not None
                            else s.boxes[box_id].desired) + 1e-7
               for box_id, relative in route["delivery"].items())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=BASE)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seconds-per-candidate", type=float, default=2.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args()
    s = Scenario(args.data_dir)
    source = json.loads(args.input.read_text(encoding="utf-8"))
    source_check = verify_q2(s, source)
    dispatch = Dispatch(s)
    jobs = jobs_from_plan(source)
    tail_index = max(range(len(source["sorties"])),
                     key=lambda i: source["sorties"][i]["return_time"])
    tail = jobs[tail_index]
    if tail.visits[0][0] != "S004" or len(tail.visits) != 1:
        raise ValueError("The source critical tail is no longer the single-zone S004 route")
    cap = int(source["objective"]["makespan"]) + 1
    # The source has a 6075-tick conservative CP makespan; a 6074-tick cap
    # screens for a real change in its critical path before exact verification.
    cap = min(cap, 6074)
    candidates = []
    for box_id in tail.box_ids:
        remaining = tuple(box for box in tail.box_ids if box != box_id)
        shorter_tail = Job("shorter_tail", (("S004", remaining),))
        if not any((route := dispatch.evaluate(shorter_tail, model)) is not None
                   and relative_deadlines_ok(s, route) for model in s.transport):
            continue
        for target_index, target in enumerate(jobs):
            if target_index == tail_index:
                continue
            for visits in insert_box(target.visits, box_id, "S004"):
                moved_target = Job("receiving", visits)
                models = [model for model in s.transport
                          if (route := dispatch.evaluate(moved_target, model)) is not None
                          and relative_deadlines_ok(s, route)]
                if models:
                    candidates.append((box_id, target_index, moved_target,
                                       shorter_tail, models))

    seen = set()
    rows = []
    best = None
    for index, (box_id, target_index, target, shorter_tail, models) in enumerate(candidates, 1):
        signature = (box_id, target_index, target.visits)
        if signature in seen:
            continue
        seen.add(signature)
        updated = [shorter_tail if i == tail_index else target if i == target_index else job
                   for i, job in enumerate(jobs)]
        updated = [Job(f"J{k:03d}", job.visits) for k, job in enumerate(updated, 1)]
        counts = Counter(box for job in updated for box in job.box_ids)
        if set(counts) != set(s.boxes) or any(count != 1 for count in counts.values()):
            raise AssertionError("Candidate lost or duplicated a box")
        trial = dummy_plan(s, dispatch, updated, source["objective"]["makespan"])
        plan, report = solve_case(s, trial, "makespan", scale=1,
                                  time_limit=args.seconds_per_candidate,
                                  workers=args.workers, seed=args.seed + index,
                                  makespan_cap=cap)
        row = {
            "box": box_id, "target_source_sortie": source["sorties"][target_index]["id"],
            "target_visits": target.visits, "target_models": models,
            "status": report["status"],
            "objective": plan["objective"] if plan is not None else None,
        }
        rows.append(row)
        if plan is not None and (best is None or
                                 plan["objective"]["makespan"] < best["objective"]["makespan"]):
            best = plan
            print(json.dumps({"new_best": row}, ensure_ascii=False), flush=True)
    args.output.mkdir(parents=True, exist_ok=True)
    summary = {
        "source": str(args.input), "source_objective": source["objective"],
        "source_verification": source_check, "critical_tail": tail.visits,
        "integer_makespan_cap": cap, "physical_candidates": len(seen),
        "tested": len(rows), "feasible": sum(row["objective"] is not None for row in rows),
        "cases": rows,
        "best_objective": best["objective"] if best is not None else None,
    }
    (args.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if best is not None:
        verify_q2(s, best)
        (args.output / "best_plan.json").write_text(
            json.dumps(best, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: summary[key] for key in
                      ("physical_candidates", "tested", "feasible", "best_objective")},
                     ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
