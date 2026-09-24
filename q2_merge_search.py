"""Systematically test one-sortie merges with exact Q2 resource scheduling."""

from __future__ import annotations

import argparse
import json
from itertools import combinations
from pathlib import Path

from model import BASE, Scenario
from q2 import Dispatch, Job
from q2_exact_schedule import solve_case
from verify import verify_q2


ROOT = Path(__file__).resolve().parent


def jobs_from_plan(plan: dict) -> list[Job]:
    return [Job(row["job"], tuple((zone, tuple(boxes)) for zone, boxes in row["visits"]))
            for row in plan["sorties"]]


def dummy_plan(s: Scenario, dispatcher: Dispatch, jobs: list[Job], horizon: float) -> dict:
    """Provide CP-SAT with route descriptions and valid-type hints only."""
    rows = []
    for i, job in enumerate(jobs, 1):
        options = [(dispatcher.evaluate(job, g)["energy_kwh"], g)
                   for g in s.transport if dispatcher.evaluate(job, g) is not None]
        _, model = min(options)
        drone = next(u for u, kind in s.aircraft.items() if kind == model)
        rows.append(dict(id=f"T{i:03d}", job=job.id, visits=job.visits,
                         model=model, drone=drone, battery=f"{model}01"))
    return dict(sorties=rows, objective=dict(makespan=horizon))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=BASE)
    parser.add_argument("--input", type=Path,
                        default=ROOT / "results_q2_refined" / "q2_energy_plan.json")
    parser.add_argument("--output", type=Path, default=ROOT / "results_q2_merge_search")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--seconds-per-candidate", type=float, default=5.0)
    parser.add_argument("--makespan-cap", type=float, default=None)
    parser.add_argument("--objective", choices=("energy", "makespan"), default="energy")
    parser.add_argument("--rank-by", choices=("energy", "duration"), default="energy")
    parser.add_argument("--save-all", action="store_true",
                        help="retain every verified feasible candidate for further search")
    args = parser.parse_args()
    s = Scenario(args.data_dir)
    source = json.loads(args.input.read_text(encoding="utf-8"))
    verify_q2(s, source)
    dispatcher = Dispatch(s)
    jobs = jobs_from_plan(source)
    candidates = {}
    for i, j in combinations(range(len(jobs)), 2):
        for reverse in (False, True):
            merged = dispatcher.merge(jobs[i], jobs[j], reverse)
            options = [dispatcher.evaluate(merged, g) for g in s.transport]
            options = [route for route in options if route is not None]
            if not options:
                continue
            if not any(all(relative <= (s.boxes[bid].hard_deadline
                                       if s.boxes[bid].hard_deadline is not None
                                       else s.boxes[bid].desired) + 1e-7
                           for bid, relative in route["delivery"].items())
                       for route in options):
                continue
            signature = merged.visits
            relative_energy = (min(route["energy_kwh"] for route in options)
                               - source["sorties"][i]["energy_kwh"]
                               - source["sorties"][j]["energy_kwh"])
            candidate = dict(i=i, j=j, reverse=reverse, job=merged,
                             relative_energy=relative_energy,
                             min_duration=min(route["duration"] for route in options))
            if signature not in candidates or relative_energy < candidates[signature]["relative_energy"]:
                candidates[signature] = candidate
    if args.rank_by == "duration":
        ranked = sorted(candidates.values(), key=lambda row: row["min_duration"])
    else:
        ranked = sorted(candidates.values(), key=lambda row: row["relative_energy"])
    outcome = []
    best = None
    for rank, item in enumerate(ranked[:args.limit], 1):
        i, j = item["i"], item["j"]
        updated = [job for k, job in enumerate(jobs) if k not in (i, j)]
        updated.insert(min(i, j), item["job"])
        updated = [Job(f"J{k:03d}", job.visits) for k, job in enumerate(updated, 1)]
        trial = dummy_plan(s, dispatcher, updated, source["objective"]["makespan"])
        case = "makespan" if args.objective == "makespan" else "alns"
        plan, report = solve_case(s, trial, case, 1, args.seconds_per_candidate,
                                  8, 2026 + rank, makespan_cap=args.makespan_cap)
        result = dict(rank=rank, merge_source_jobs=[jobs[i].id, jobs[j].id],
                      relative_energy_kwh=item["relative_energy"],
                      minimum_merged_duration_seconds=item["min_duration"],
                      status=report["status"],
                      objective=plan["objective"] if plan else None)
        outcome.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if plan is not None and args.save_all:
            plan_dir = args.output / "candidate_plans"
            plan_dir.mkdir(parents=True, exist_ok=True)
            (plan_dir / f"rank_{rank:03d}.json").write_text(
                json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        def key(x: dict) -> tuple[float, float]:
            o = x["objective"]
            return ((o["makespan"], o["energy_kwh"]) if args.objective == "makespan"
                    else (o["energy_kwh"], o["makespan"]))
        if plan is not None and (best is None or key(plan) < key(best)):
            best = plan
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "merge_search.json").write_text(json.dumps(dict(
        source=str(args.input), source_objective=source["objective"],
        objective=args.objective, rank_by=args.rank_by,
        makespan_cap_seconds=args.makespan_cap,
        physical_timing_candidates=len(candidates), tested=len(outcome),
        cases=outcome, best_objective=best["objective"] if best else None,
    ), ensure_ascii=False, indent=2), encoding="utf-8")
    if best is not None:
        verify_q2(s, best)
        (args.output / "best_plan.json").write_text(
            json.dumps(best, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
