"""Probe sortie-count potential in the pool of existing verified Q2 routes.

The exact-cover master ignores time/resource scheduling. Its output is a route
combination, never a Q2 solution until the independent transport check passes.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ortools.sat.python import cp_model

from model import BASE, Scenario
from q2 import Dispatch, Job
from q2_exact_schedule import solve_case
from q2_merge_search import dummy_plan
from verify import verify_q2


ROOT = Path(__file__).resolve().parent


def route_pool(s: Scenario, original: dict, refined: dict,
               extra_plans: list[dict] | None = None,
               extra_routes: list[tuple] | None = None) -> list[Job]:
    plans = [original, *original["alternatives"].values(), refined,
             refined["alternatives"]["refined_energy"], *(extra_plans or [])]
    seen = set()
    jobs = []
    for plan in plans:
        verify_q2(s, {key: plan[key] for key in ("sorties", "deliveries", "objective")})
        for row in plan["sorties"]:
            visits = tuple((zone, tuple(boxes)) for zone, boxes in row["visits"])
            if visits not in seen:
                seen.add(visits)
                jobs.append(Job(f"P{len(jobs) + 1:03d}", visits))
    for raw in extra_routes or []:
        visits = tuple((zone, tuple(boxes)) for zone, boxes in raw)
        if visits not in seen:
            seen.add(visits)
            jobs.append(Job(f"P{len(jobs) + 1:03d}", visits))
    return jobs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=BASE)
    parser.add_argument("--original", type=Path, default=ROOT / "results_q2" / "q2.json")
    parser.add_argument("--refined", type=Path,
                        default=ROOT / "results_q2_refined" / "q2.json")
    parser.add_argument("--time-limit", type=float, default=20)
    parser.add_argument("--candidates", type=int, default=10)
    parser.add_argument("--schedule-attempts", type=int, default=20)
    parser.add_argument("--exact-seconds", type=float, default=0,
                        help="CP-SAT zero-soft-delay scheduling time per greedy-feasible cover")
    parser.add_argument("--zero-soft", action="store_true",
                        help="exclude routes unable to meet every desired time even at t=0")
    parser.add_argument("--extra-plan", type=Path, action="append", default=[],
                        help="additional complete verified plan to contribute routes")
    parser.add_argument("--extra-route-search", type=Path, action="append", default=[],
                        help="search JSON whose candidate routes contribute to the pool")
    parser.add_argument("--max-count", type=int, default=None,
                        help="restrict exact covers to at most this many sorties")
    parser.add_argument("--min-count", type=int, default=None,
                        help="restrict exact covers to at least this many sorties")
    parser.add_argument("--direct-exact", action="store_true",
                        help="run CP-SAT even when greedy scheduling fails")
    parser.add_argument("--rank-objective", choices=("count", "energy"), default="count",
                        help="order exact-cover candidates by count or route energy lower bound")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "results_q2_refined" / "q2_route_pool_probe.json")
    args = parser.parse_args()
    s = Scenario(args.data_dir)
    original = json.loads(args.original.read_text(encoding="utf-8"))
    refined = json.loads(args.refined.read_text(encoding="utf-8"))
    extras = [json.loads(path.read_text(encoding="utf-8")) for path in args.extra_plan]
    extra_routes = []
    for path in args.extra_route_search:
        search = json.loads(path.read_text(encoding="utf-8"))
        for case in search["cases"]:
            extra_routes.extend(case.get("added_routes", []))
            for key in ("first_visits", "second_visits"):
                if key in case:
                    extra_routes.append(case[key])
    pool = route_pool(s, original, refined, extras, extra_routes)
    total_pool = len(pool)
    dispatcher = Dispatch(s)
    if args.zero_soft:
        def locally_timely(job: Job) -> bool:
            for aircraft_model in s.transport:
                route = dispatcher.evaluate(job, aircraft_model)
                if route is not None and all(
                    relative <= (s.boxes[bid].hard_deadline
                                 if s.boxes[bid].hard_deadline is not None
                                 else s.boxes[bid].desired) + 1e-7
                    for bid, relative in route["delivery"].items()
                ):
                    return True
            return False
        pool = [job for job in pool if locally_timely(job)]
    model = cp_model.CpModel()
    use = [model.NewBoolVar(f"route_{i}") for i in range(len(pool))]
    for bid in s.boxes:
        model.Add(sum(use[i] for i, job in enumerate(pool) if bid in job.box_ids) == 1)
    count = sum(use)
    if args.max_count is not None:
        model.Add(count <= args.max_count)
    if args.min_count is not None:
        model.Add(count >= args.min_count)
    if args.rank_objective == "energy":
        energy_bound = []
        for job in pool:
            choices = [dispatcher.evaluate(job, kind) for kind in s.transport]
            energy_bound.append(min(route["energy_kwh"] for route in choices
                                    if route is not None))
        model.Minimize(sum(round(value * 1_000_000) * use[i]
                           for i, value in enumerate(energy_bound)))
    else:
        model.Minimize(count)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = args.time_limit
    solver.parameters.num_search_workers = 8
    results = []
    best_valid = None
    lower_bound = None
    for n in range(args.candidates):
        status = solver.Solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            results.append(dict(rank=n + 1, set_partition_status=solver.StatusName(status)))
            break
        if lower_bound is None:
            lower_bound = solver.BestObjectiveBound()
        selected = [i for i in range(len(pool)) if solver.Value(use[i])]
        jobs = [Job(f"J{i:03d}", pool[index].visits)
                for i, index in enumerate(selected, 1)]
        trial = None
        try:
            trial = dispatcher.schedule(jobs, attempts=args.schedule_attempts, seed=2026 + n)
        except ValueError:
            pass
        if args.direct_exact and args.exact_seconds > 0:
            hint = trial if trial is not None else dummy_plan(s, dispatcher, jobs, 10000)
            exact, report = solve_case(s, hint, "alns", 1,
                                       args.exact_seconds, 8, 2026 + n)
            exact_status = report["status"]
            trial = exact
        else:
            exact_status = None
        if trial is not None:
            verify_q2(s, trial)
            if args.exact_seconds > 0 and not args.direct_exact:
                exact, report = solve_case(s, trial, "main", 1,
                                           args.exact_seconds, 8, 2026 + n)
                exact_status = report["status"]
                if exact is not None:
                    trial = exact
            if best_valid is None or (trial["objective"]["weighted_soft_delay_seconds"],
                                      trial["objective"]["energy_kwh"]) < (
                best_valid["objective"]["weighted_soft_delay_seconds"],
                best_valid["objective"]["energy_kwh"]):
                best_valid = trial
        results.append(dict(rank=n + 1, route_count=len(selected),
                            set_partition_status=solver.StatusName(status),
                            scheduled=trial is not None,
                            exact_zero_delay_status=exact_status,
                            objective=trial["objective"] if trial else None))
        model.Add(sum(use[i] for i in selected) <= len(selected) - 1)
    record = dict(pool_routes_total=total_pool, pool_routes_used=len(pool),
                  model="finite_existing_route_pool_set_partition_then_Q2_schedule",
                  zero_soft_route_filter=args.zero_soft,
                  extra_plan_paths=[str(path) for path in args.extra_plan],
                  extra_route_search_paths=[str(path) for path in args.extra_route_search],
                  min_count=args.min_count, max_count=args.max_count,
                  direct_exact=args.direct_exact,
                  rank_objective=args.rank_objective,
                  set_partition_lower_bound=(lower_bound if args.rank_objective == "count"
                                             else None),
                  set_partition_objective_bound=lower_bound,
                  candidates=results,
                  best_verified=best_valid["objective"] if best_valid else None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
