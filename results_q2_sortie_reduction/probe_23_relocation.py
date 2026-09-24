"""Single-box relocation search around the fast 23-sortie plan."""

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
from q2_time_s004_tail_probe import insert_box, relative_deadlines_ok
from verify import verify_q2


def options(s: Scenario, dispatch: Dispatch, job: Job) -> list[dict]:
    return [route for g in "AB"
            if (route := dispatch.evaluate(job, g)) is not None
            and relative_deadlines_ok(s, route)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--makespan-cap", type=float, default=6114)
    parser.add_argument("--energy-cap", type=float, default=69.15451555444506)
    parser.add_argument("--name", default="relocation_23")
    args = parser.parse_args()
    scenario = Scenario(BASE)
    dispatch = Dispatch(scenario)
    source_path = ROOT / "results_q2_time/frontier_23/weighted_plan.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    verify_q2(scenario, source)
    jobs = jobs_from_plan(source)
    candidates = {}
    for i, job in enumerate(jobs):
        if source["sorties"][i]["model"] == "C" or len(job.box_ids) <= 1:
            continue
        for bid in job.box_ids:
            remaining = Job("source", tuple((zone, tuple(x for x in box_ids if x != bid))
                                            for zone, box_ids in job.visits
                                            if any(x != bid for x in box_ids)))
            left_options = options(scenario, dispatch, remaining)
            if not left_options:
                continue
            for j, receiver in enumerate(jobs):
                if j == i or source["sorties"][j]["model"] == "C":
                    continue
                for visits in insert_box(receiver.visits, bid, scenario.boxes[bid].zone):
                    changed = Job("receiver", tuple(visits))
                    right_options = options(scenario, dispatch, changed)
                    if not right_options:
                        continue
                    signature = (i, j, bid, changed.visits)
                    energy_delta = min(x["energy_kwh"] for x in left_options) + min(
                        x["energy_kwh"] for x in right_options) - source["sorties"][i][
                        "energy_kwh"] - source["sorties"][j]["energy_kwh"]
                    if energy_delta >= -0.10:
                        continue
                    candidates[signature] = (remaining, changed, energy_delta)
    ranked = sorted(candidates.items(), key=lambda entry: entry[1][2])
    print(json.dumps({"physical_energy_improving": len(ranked)}, ensure_ascii=False), flush=True)
    rows = []
    best = None
    for rank, ((i, j, bid, visits), (remaining, changed, delta)) in enumerate(ranked, 1):
        updated = list(jobs)
        updated[i] = remaining
        updated[j] = changed
        updated = [Job(f"J{k:03d}", job.visits) for k, job in enumerate(updated, 1)]
        counts = Counter(b for job in updated for b in job.box_ids)
        assert len(updated) == 23 and set(counts) == set(scenario.boxes)
        assert all(count == 1 for count in counts.values())
        trial = dummy_plan(scenario, dispatch, updated, source["objective"]["makespan"])
        plan, report = solve_case(scenario, trial, "makespan", 1, 3.0, 8, 2026 + rank,
                                  makespan_cap=args.makespan_cap,
                                  energy_cap=args.energy_cap)
        row = {"rank": rank, "source": source["sorties"][i]["id"],
               "receiver": source["sorties"][j]["id"], "box": bid,
               "energy_route_delta": delta, "status": report["status"],
               "objective": plan["objective"] if plan is not None else None}
        rows.append(row)
        if plan is not None:
            print(json.dumps(row, ensure_ascii=False), flush=True)
            if best is None or (plan["objective"]["makespan"],
                                plan["objective"]["energy_kwh"]) < (
                                best["objective"]["makespan"],
                                best["objective"]["energy_kwh"]):
                best = plan
    out = Path(__file__).resolve().parent
    (out / f"{args.name}.json").write_text(
        json.dumps({"source": str(source_path), "source_objective": source["objective"],
                    "physical_energy_improving": len(ranked), "cases": rows},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    if best is not None:
        verify_q2(scenario, best)
        (out / f"{args.name}_best_plan.json").write_text(
            json.dumps(best, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
