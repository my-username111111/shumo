"""Repack boxes of three Q2 sorties into two newly ordered routes."""

from __future__ import annotations

import argparse
import json
from itertools import permutations
from pathlib import Path

from model import BASE, Scenario
from q2 import Dispatch, Job
from q2_exact_schedule import solve_case
from q2_merge_search import dummy_plan, jobs_from_plan
from verify import verify_q2


ROOT = Path(__file__).resolve().parent


def ordered_jobs(box_ids: tuple[str, ...], s: Scenario) -> list[Job]:
    grouped = {}
    for bid in box_ids:
        grouped.setdefault(s.boxes[bid].zone, []).append(bid)
    return [Job("new", tuple((zone, tuple(sorted(grouped[zone]))) for zone in order))
            for order in permutations(sorted(grouped))]


def local_options(dispatcher: Dispatch, job: Job, model: str) -> dict | None:
    route = dispatcher.evaluate(job, model)
    if route is None:
        return None
    for bid, relative in route["delivery"].items():
        box = dispatcher.s.boxes[bid]
        due = box.hard_deadline if box.hard_deadline is not None else box.desired
        if relative > due + 1e-7:
            return None
    return route


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=BASE)
    parser.add_argument("--input", type=Path,
                        default=ROOT / "results_q2_ejection" / "weighted_refined" / "plan.json")
    parser.add_argument("--source-sorties", type=str, default="6,8,11",
                        help="three one-based row numbers in the source plan")
    parser.add_argument("--first-model", type=str, default="B")
    parser.add_argument("--second-model", type=str, default="C")
    parser.add_argument("--output", type=Path, default=ROOT / "results_q2_three_to_two")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--seconds-per-candidate", type=float, default=4)
    parser.add_argument("--makespan-cap", type=float, default=10000)
    parser.add_argument("--objective", choices=("energy", "makespan", "weighted"),
                        default="energy")
    args = parser.parse_args()
    s = Scenario(args.data_dir)
    source = json.loads(args.input.read_text(encoding="utf-8"))
    verify_q2(s, source)
    dispatcher = Dispatch(s)
    jobs = jobs_from_plan(source)
    indices = tuple(int(x) - 1 for x in args.source_sorties.split(","))
    if len(indices) != 3 or len(set(indices)) != 3 or any(i < 0 or i >= len(jobs) for i in indices):
        parser.error("source-sorties must contain three distinct valid row numbers")
    boxes = tuple(sorted(bid for i in indices for bid in jobs[i].box_ids))
    first_type = s.transport[args.first_model]
    second_type = s.transport[args.second_model]
    candidates = {}
    # Nonempty proper subsets; first model has a small payload, so the mass and
    # volume tests remove most masks before any DEM route evaluation.
    for mask in range(1, (1 << len(boxes)) - 1):
        first_ids = tuple(b for k, b in enumerate(boxes) if mask & (1 << k))
        second_ids = tuple(b for k, b in enumerate(boxes) if not mask & (1 << k))
        mass_first = sum(s.boxes[b].kg for b in first_ids)
        mass_second = sum(s.boxes[b].kg for b in second_ids)
        volume_first = sum(s.boxes[b].volume for b in first_ids)
        volume_second = sum(s.boxes[b].volume for b in second_ids)
        if (mass_first > first_type.max_kg + 1e-8 or
                mass_second > second_type.max_kg + 1e-8 or
                volume_first > first_type.max_volume + 1e-8 or
                volume_second > second_type.max_volume + 1e-8):
            continue
        left = [(job, local_options(dispatcher, job, args.first_model))
                for job in ordered_jobs(first_ids, s)]
        right = [(job, local_options(dispatcher, job, args.second_model))
                 for job in ordered_jobs(second_ids, s)]
        for first_job, first_route in left:
            if first_route is None:
                continue
            for second_job, second_route in right:
                if second_route is None:
                    continue
                signature = (first_job.visits, second_job.visits)
                candidates[signature] = dict(first=first_job, second=second_job,
                                             energy=first_route["energy_kwh"]
                                             + second_route["energy_kwh"],
                                             duration=max(first_route["duration"],
                                                          second_route["duration"]))
    ranked = sorted(candidates.values(), key=lambda x: (x["energy"], x["duration"]))
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    best = None
    for rank, candidate in enumerate(ranked[:args.limit], 1):
        updated = [job for k, job in enumerate(jobs) if k not in indices]
        updated.extend([candidate["first"], candidate["second"]])
        updated = [Job(f"J{k:03d}", job.visits) for k, job in enumerate(updated, 1)]
        trial = dummy_plan(s, dispatcher, updated, source["objective"]["makespan"])
        case = {"energy": "alns", "makespan": "makespan",
                "weighted": "main"}[args.objective]
        plan, report = solve_case(s, trial, case, 1,
                                  args.seconds_per_candidate, 8, 2026 + rank,
                                  makespan_cap=args.makespan_cap)
        result = dict(rank=rank, route_energy_kwh=candidate["energy"],
                      first_visits=candidate["first"].visits,
                      second_visits=candidate["second"].visits,
                      solver_status=report["status"],
                      objective=plan["objective"] if plan else None)
        results.append(result)
        print(json.dumps({k: v for k, v in result.items() if k not in
                          ("first_visits", "second_visits")}, ensure_ascii=False), flush=True)
        keys = {"energy": ("energy_kwh", "makespan"),
                "makespan": ("makespan", "energy_kwh"),
                "weighted": ("weighted_delivery_seconds", "energy_kwh")}
        measure = lambda p: tuple(p["objective"][key] for key in keys[args.objective])
        if plan is not None and (best is None or measure(plan) < measure(best)):
            best = plan
    report = dict(source=str(args.input), source_objective=source["objective"],
                  source_sorties=[i + 1 for i in indices],
                  model_pair=[args.first_model, args.second_model],
                  search_objective=args.objective,
                  physical_candidates=len(candidates), tested=len(results),
                  cases=results, best_objective=best["objective"] if best else None)
    (args.output / "search.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if best is not None:
        verify_q2(s, best)
        (args.output / "best_plan.json").write_text(
            json.dumps(best, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
