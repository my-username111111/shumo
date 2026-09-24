"""Search 3-to-2 Q2 route changes by emptying one sortie into two others."""

from __future__ import annotations

import argparse
import json
from itertools import combinations, product
from pathlib import Path

from model import BASE, Scenario
from q2 import Dispatch, Job
from q2_exact_schedule import solve_case
from q2_merge_search import dummy_plan, jobs_from_plan
from verify import verify_q2


ROOT = Path(__file__).resolve().parent


def add_box(job: Job, bid: str, zone: str) -> Job:
    visits = []
    for current, ids in job.visits:
        visits.append((current, tuple(sorted((*ids, bid))) if current == zone else ids))
    return Job(job.id, tuple(visits))


def options(dispatcher: Dispatch, job: Job) -> list[dict]:
    result = []
    for model in dispatcher.s.transport:
        route = dispatcher.evaluate(job, model)
        if route is None:
            continue
        if all(relative <= (dispatcher.s.boxes[bid].hard_deadline
                            if dispatcher.s.boxes[bid].hard_deadline is not None
                            else dispatcher.s.boxes[bid].desired) + 1e-7
               for bid, relative in route["delivery"].items()):
            result.append(route)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=BASE)
    parser.add_argument("--input", type=Path,
                        default=ROOT / "results_q2_extended" / "plans" / "21_low_sorties.json")
    parser.add_argument("--output", type=Path, default=ROOT / "results_q2_ejection")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--seconds-per-candidate", type=float, default=5)
    parser.add_argument("--makespan-cap", type=float, default=10000)
    args = parser.parse_args()
    scenario = Scenario(args.data_dir)
    source = json.loads(args.input.read_text(encoding="utf-8"))
    verify_q2(scenario, source)
    dispatcher = Dispatch(scenario)
    jobs = jobs_from_plan(source)
    candidates = {}
    for source_index, emptied in enumerate(jobs):
        if len(emptied.box_ids) > 5:
            continue
        for left_index, right_index in combinations(
                (i for i in range(len(jobs)) if i != source_index), 2):
            left, right = jobs[left_index], jobs[right_index]
            left_zones = {zone for zone, _ in left.visits}
            right_zones = {zone for zone, _ in right.visits}
            if any(scenario.boxes[bid].zone not in left_zones | right_zones
                   for bid in emptied.box_ids):
                continue
            choices = [tuple(i for i, zones in enumerate((left_zones, right_zones))
                             if scenario.boxes[bid].zone in zones)
                       for bid in emptied.box_ids]
            for assignment in product(*choices):
                if set(assignment) != {0, 1}:
                    continue
                changed = [left, right]
                for bid, side in zip(emptied.box_ids, assignment):
                    changed[side] = add_box(changed[side], bid, scenario.boxes[bid].zone)
                left_options, right_options = (options(dispatcher, job) for job in changed)
                if not left_options or not right_options:
                    continue
                updated = [job for k, job in enumerate(jobs) if k != source_index]
                updated[left_index - (left_index > source_index)] = changed[0]
                updated[right_index - (right_index > source_index)] = changed[1]
                signature = tuple(job.visits for job in updated)
                estimated_energy = (min(x["energy_kwh"] for x in left_options)
                                    + min(x["energy_kwh"] for x in right_options)
                                    - sum(source["sorties"][i]["energy_kwh"]
                                          for i in (source_index, left_index, right_index)))
                if signature not in candidates or estimated_energy < candidates[signature]["score"]:
                    candidates[signature] = dict(source=source_index, left=left_index,
                                                 right=right_index, jobs=updated,
                                                 score=estimated_energy,
                                                 assignment=assignment)
    ranked = sorted(candidates.values(), key=lambda x: x["score"])
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    best = None
    for rank, item in enumerate(ranked[:args.limit], 1):
        updated = [Job(f"J{i:03d}", job.visits)
                   for i, job in enumerate(item["jobs"], 1)]
        trial = dummy_plan(scenario, dispatcher, updated, source["objective"]["makespan"])
        plan, report = solve_case(scenario, trial, "alns", 1,
                                  args.seconds_per_candidate, 8, 2026 + rank,
                                  makespan_cap=args.makespan_cap)
        result = dict(rank=rank, source_sortie=item["source"] + 1,
                      recipient_sorties=[item["left"] + 1, item["right"] + 1],
                      estimated_energy_delta_kwh=item["score"],
                      solver_status=report["status"],
                      objective=plan["objective"] if plan else None)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if plan is not None and (best is None or (
                plan["objective"]["energy_kwh"], plan["objective"]["makespan"]
        ) < (best["objective"]["energy_kwh"], best["objective"]["makespan"])):
            best = plan
    report = dict(source=str(args.input), source_objective=source["objective"],
                  candidates=len(candidates), tested=len(results), cases=results,
                  best_objective=best["objective"] if best else None)
    (args.output / "search.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if best is not None:
        verify_q2(scenario, best)
        (args.output / "best_plan.json").write_text(
            json.dumps(best, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
