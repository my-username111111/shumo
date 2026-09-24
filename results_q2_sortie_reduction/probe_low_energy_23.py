"""Move one C-route box from the 68.61-kWh 23-sortie plan to an A/B route."""

from __future__ import annotations

import json
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


def main() -> None:
    scenario = Scenario(BASE)
    dispatch = Dispatch(scenario)
    path = ROOT / "results_q2_time/research_23/hyg_j004_to_j001_plan.json"
    source = json.loads(path.read_text(encoding="utf-8"))
    verify_q2(scenario, source)
    jobs = jobs_from_plan(source)
    candidates = {}
    for i, job in enumerate(jobs):
        if source["sorties"][i]["model"] != "C":
            continue
        for bid in job.box_ids:
            remaining = Job("source", tuple((zone, tuple(x for x in boxes if x != bid))
                                            for zone, boxes in job.visits
                                            if any(x != bid for x in boxes)))
            if dispatch.evaluate(remaining, "C") is None:
                continue
            for j, receiver in enumerate(jobs):
                if source["sorties"][j]["model"] == "C" or i == j:
                    continue
                for visits in insert_box(receiver.visits, bid, scenario.boxes[bid].zone):
                    changed = Job("receiver", tuple(visits))
                    if not any((route := dispatch.evaluate(changed, g)) is not None
                               and relative_deadlines_ok(scenario, route) for g in "AB"):
                        continue
                    candidates[(i, j, bid, changed.visits)] = (remaining, changed)
    print(json.dumps({"physical_candidates": len(candidates)}, ensure_ascii=False), flush=True)
    rows = []
    best = None
    for rank, ((i, j, bid, visits), (remaining, changed)) in enumerate(candidates.items(), 1):
        updated = list(jobs)
        updated[i], updated[j] = remaining, changed
        updated = [Job(f"J{k:03d}", item.visits) for k, item in enumerate(updated, 1)]
        counts = Counter(box for item in updated for box in item.box_ids)
        assert len(updated) == 23 and set(counts) == set(scenario.boxes)
        assert all(n == 1 for n in counts.values())
        trial = dummy_plan(scenario, dispatch, updated, source["objective"]["makespan"])
        plan, report = solve_case(scenario, trial, "makespan", 1, 5, 8, 2026 + rank,
                                  makespan_cap=6114, energy_cap=69.15451555444506)
        row = {"rank": rank, "source": source["sorties"][i]["id"],
               "receiver": source["sorties"][j]["id"], "box": bid,
               "status": report["status"], "objective": plan["objective"] if plan else None}
        rows.append(row)
        if plan is not None:
            print(json.dumps(row, ensure_ascii=False), flush=True)
            if best is None or plan["objective"]["makespan"] < best["objective"]["makespan"]:
                best = plan
    out = Path(__file__).resolve().parent
    (out / "low_energy_23_probe.json").write_text(
        json.dumps({"source": str(path), "source_objective": source["objective"],
                    "physical_candidates": len(candidates), "cases": rows},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    if best is not None:
        verify_q2(scenario, best)
        (out / "low_energy_23_best_plan.json").write_text(
            json.dumps(best, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
