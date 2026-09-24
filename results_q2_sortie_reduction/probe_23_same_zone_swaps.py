"""Exchange two same-zone boxes between existing 23 Q2 sorties."""

from __future__ import annotations

import json
import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from model import BASE, Scenario
from q2 import Dispatch, Job
from q2_exact_schedule import solve_case
from q2_merge_search import dummy_plan, jobs_from_plan
from q2_time_s004_tail_probe import relative_deadlines_ok
from verify import verify_q2


def swapped(job: Job, out_id: str, in_id: str) -> Job:
    return Job(job.id, tuple((zone, tuple(sorted(in_id if x == out_id else x
                                           for x in box_ids))) for zone, box_ids in job.visits))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--energy-cap", type=float, default=69.15451555444506)
    parser.add_argument("--makespan-cap", type=float, default=6114)
    parser.add_argument("--case", choices=("main", "alns", "makespan"), default="alns")
    parser.add_argument("--name", default="same_zone_swaps_23")
    args = parser.parse_args()
    scenario = Scenario(BASE)
    source_path = ROOT / "results_q2_time/frontier_23/weighted_plan.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    verify_q2(scenario, source)
    jobs = jobs_from_plan(source)
    dispatch = Dispatch(scenario)
    candidates = []
    for i, left in enumerate(jobs):
        for j in range(i + 1, len(jobs)):
            right = jobs[j]
            for a in left.box_ids:
                for b in right.box_ids:
                    if scenario.boxes[a].zone != scenario.boxes[b].zone:
                        continue
                    changed_left = swapped(left, a, b)
                    changed_right = swapped(right, b, a)
                    left_options = [route for g in scenario.transport
                                    if (route := dispatch.evaluate(changed_left, g)) is not None
                                    and relative_deadlines_ok(scenario, route)]
                    right_options = [route for g in scenario.transport
                                     if (route := dispatch.evaluate(changed_right, g)) is not None
                                     and relative_deadlines_ok(scenario, route)]
                    if not left_options or not right_options:
                        continue
                    current_left = dispatch.evaluate(changed_left, source["sorties"][i]["model"])
                    current_right = dispatch.evaluate(changed_right, source["sorties"][j]["model"])
                    if current_left is None or current_right is None:
                        continue
                    energy_delta = (current_left["energy_kwh"] + current_right["energy_kwh"]
                                    - source["sorties"][i]["energy_kwh"]
                                    - source["sorties"][j]["energy_kwh"])
                    if energy_delta < -0.001:
                        candidates.append((energy_delta, i, j, a, b,
                                           changed_left, changed_right))
    candidates.sort(key=lambda item: item[0])
    print(json.dumps({"energy_improving_swaps": len(candidates)}, ensure_ascii=False), flush=True)
    cases = []
    best = None
    for rank, (delta, i, j, a, b, left, right) in enumerate(candidates, 1):
        updated = list(jobs)
        updated[i], updated[j] = left, right
        updated = [Job(f"J{k:03d}", job.visits) for k, job in enumerate(updated, 1)]
        counts = Counter(box for job in updated for box in job.box_ids)
        assert set(counts) == set(scenario.boxes) and all(n == 1 for n in counts.values())
        trial = dummy_plan(scenario, dispatch, updated, source["objective"]["makespan"])
        plan, report = solve_case(scenario, trial, args.case, 1, 5, 8, 2026 + rank,
                                  makespan_cap=args.makespan_cap,
                                  energy_cap=args.energy_cap)
        row = {"rank": rank, "left": source["sorties"][i]["id"],
               "right": source["sorties"][j]["id"], "swap": [a, b],
               "fixed_model_energy_delta": delta, "status": report["status"],
               "objective": plan["objective"] if plan else None}
        cases.append(row)
        if plan is not None:
            print(json.dumps(row, ensure_ascii=False), flush=True)
            if best is None or (plan["objective"]["energy_kwh"],
                                plan["objective"]["weighted_delivery_seconds"]) < (
                                best["objective"]["energy_kwh"],
                                best["objective"]["weighted_delivery_seconds"]):
                best = plan
    out = Path(__file__).resolve().parent
    (out / f"{args.name}.json").write_text(
        json.dumps({"source": str(source_path), "source_objective": source["objective"],
                    "energy_improving_swaps": len(candidates), "cases": cases},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    if best is not None:
        verify_q2(scenario, best)
        (out / f"{args.name}_best_plan.json").write_text(
            json.dumps(best, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
