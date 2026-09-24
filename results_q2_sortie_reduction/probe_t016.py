"""Probe deletion of the two-box T016 route while holding Q2 time/energy caps."""

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
    source_path = ROOT / "results_q2_time/frontier_24/weighted_plan.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    scenario = Scenario(BASE)
    verify_q2(scenario, source)
    dispatch = Dispatch(scenario)
    jobs = jobs_from_plan(source)
    source_index = next(i for i, row in enumerate(source["sorties"]) if row["id"] == "T016")
    emptied = jobs[source_index]
    assert len(emptied.box_ids) == 2
    option_lists = []
    for bid in emptied.box_ids:
        candidates = []
        zone = scenario.boxes[bid].zone
        for i, job in enumerate(jobs):
            if i == source_index:
                continue
            for visits in insert_box(job.visits, bid, zone):
                changed = Job(job.id, tuple(visits))
                valid = [g for g in "AB" if (route := dispatch.evaluate(changed, g)) is not None
                         and relative_deadlines_ok(scenario, route)]
                if valid:
                    candidates.append((i, changed, valid))
        option_lists.append(candidates)
    cases = []
    best = None
    for ai, ajob, amodels in option_lists[0]:
        for bi, bjob, bmodels in option_lists[1]:
            if ai == bi:
                continue
            updated = [job for i, job in enumerate(jobs) if i != source_index]
            updated[ai - (ai > source_index)] = ajob
            updated[bi - (bi > source_index)] = bjob
            updated = [Job(f"J{i:03d}", job.visits) for i, job in enumerate(updated, 1)]
            counts = Counter(bid for job in updated for bid in job.box_ids)
            assert len(updated) == 23 and set(counts) == set(scenario.boxes)
            assert all(count == 1 for count in counts.values())
            route_options = [[dispatch.evaluate(job, g) for g in scenario.transport]
                             for job in updated]
            route_options = [[route for route in options if route is not None]
                             for options in route_options]
            energy_lower = sum(min(route["energy_kwh"] for route in options)
                               for options in route_options)
            workload_lower = {g: sum(dispatch.evaluate(job, g)["duration"]
                                     for job in updated
                                     if len([h for h in scenario.transport
                                             if dispatch.evaluate(job, h) is not None]) == 1
                                     and dispatch.evaluate(job, g) is not None)
                              for g in scenario.transport}
            trial = dummy_plan(scenario, dispatch, updated, source["objective"]["makespan"])
            plan, report = solve_case(scenario, trial, "makespan", 1, 6.0, 8, 2026 + len(cases),
                                      makespan_cap=6075, energy_cap=69.15451555444506)
            row = {"recipient_a": source["sorties"][ai]["id"],
                   "recipient_b": source["sorties"][bi]["id"],
                   "a_visits": ajob.visits, "b_visits": bjob.visits,
                   "models": [amodels, bmodels],
                   "energy_route_lower": energy_lower,
                   "forced_workload": workload_lower,
                   "status": report["status"],
                   "objective": plan["objective"] if plan else None}
            cases.append(row)
            print(json.dumps({key: row[key] for key in
                              ("recipient_a", "recipient_b", "status", "objective")},
                             ensure_ascii=False), flush=True)
            if plan is not None and (best is None or plan["objective"]["makespan"]
                                    < best["objective"]["makespan"]):
                best = plan
    out = Path(__file__).resolve().parent
    (out / "t016_probe.json").write_text(json.dumps({"source": str(source_path),
                                                     "source_objective": source["objective"],
                                                     "cases": cases},
                                                    ensure_ascii=False, indent=2), encoding="utf-8")
    if best is not None:
        verify_q2(scenario, best)
        (out / "t016_best_plan.json").write_text(json.dumps(best, ensure_ascii=False, indent=2),
                                                  encoding="utf-8")


if __name__ == "__main__":
    main()
