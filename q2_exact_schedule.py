"""CP-SAT rescheduling experiment for fixed Q2 box groups and visit orders.

The official Q2 physics and independent verifier remain in model.py/verify.py.
Only aircraft model, physical aircraft, battery, and sortie start time may change.
Integer time is rounded conservatively; exported plans use the original continuous
route timings and are accepted only after independent verification.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from math import ceil, floor
from pathlib import Path
from time import perf_counter

from ortools.sat.python import cp_model

from model import Scenario
from q2 import Dispatch, Job
from verify import verify_q2


ROOT = Path(__file__).resolve().parent
DEFAULT_DATA = ROOT.parent / "D题" / "数据"
DEFAULT_INPUT = ROOT / "results_q2" / "q2.json"
DEFAULT_OUTPUT = ROOT / "results_q2_exact"
ENERGY_SCALE = 1_000_000


def up(value: float, scale: int) -> int:
    return ceil(value * scale - 1e-8)


@dataclass(frozen=True)
class Option:
    model: str
    route: dict
    duration: int
    battery_span: int
    relative: dict[str, int]
    weighted_offset: int
    energy: int


def prepare(s: Scenario, baseline: dict, scale: int) -> tuple[list[Job], list[dict[str, Option]]]:
    dispatcher = Dispatch(s)
    jobs = []
    options = []
    for row in baseline["sorties"]:
        job = Job(row["job"], tuple((zone, tuple(boxes)) for zone, boxes in row["visits"]))
        jobs.append(job)
        available = {}
        for model in s.transport:
            route = dispatcher.evaluate(job, model)
            if route is None:
                continue
            charge = s.charge_time(route["return_soc"], s.battery_charge[model])
            relative = {bid: up(value, scale) for bid, value in route["delivery"].items()}
            available[model] = Option(
                model=model,
                route=route,
                duration=up(route["duration"], scale),
                battery_span=up(route["duration"] + charge, scale),
                relative=relative,
                weighted_offset=sum(s.boxes[bid].priority * value
                                    for bid, value in relative.items()),
                energy=ceil(route["energy_kwh"] * ENERGY_SCALE - 1e-8),
            )
        if not available:
            raise ValueError(f"No physically feasible model for {job.id}")
        options.append(available)
    if sorted(b for job in jobs for b in job.box_ids) != sorted(s.boxes):
        raise ValueError("Fixed routes do not cover exactly the 80 original boxes")
    return jobs, options


def build_model(s: Scenario, baseline: dict, jobs: list[Job],
                options: list[dict[str, Option]], scale: int,
                weighted_cap: int | None = None,
                makespan_cap: float | None = None,
                energy_cap: float | None = None) -> tuple[cp_model.CpModel, dict]:
    model = cp_model.CpModel()
    horizon = up(max(15_000.0, baseline["objective"]["makespan"] + 3_000.0), scale)
    starts = []
    ends = []
    choices = []
    aircraft_choices = []
    battery_choices = []
    intervals_aircraft = {u: [] for u in s.aircraft}
    batteries = {name: model_name for model_name, count in s.battery_count.items()
                 for name in (f"{model_name}{i:02d}" for i in range(1, count + 1))}
    intervals_battery = {b: [] for b in batteries}
    weighted_terms = []
    energy_terms = []

    for j, (job, available) in enumerate(zip(jobs, options)):
        start = model.NewIntVar(0, horizon, f"start_{j}")
        end = model.NewIntVar(0, horizon, f"end_{j}")
        starts.append(start)
        ends.append(end)
        take = {g: model.NewBoolVar(f"route_{j}_{g}") for g in available}
        model.AddExactlyOne(take.values())
        choices.append(take)
        model.Add(end == start + sum(take[g] * opt.duration for g, opt in available.items()))
        weighted_terms.append(sum(s.boxes[bid].priority for bid in job.box_ids) * start)
        weighted_terms.extend(take[g] * opt.weighted_offset for g, opt in available.items())
        energy_terms.extend(take[g] * opt.energy for g, opt in available.items())

        for g, opt in available.items():
            # Both medical desired times and first-aid cutoffs are hard.  All
            # other expected times are a zero-tardiness constraint in this run.
            for bid, relative in opt.relative.items():
                box = s.boxes[bid]
                limit = box.hard_deadline if box.hard_deadline is not None else box.desired
                model.Add(start + relative <= floor(limit * scale + 1e-8)).OnlyEnforceIf(take[g])

        drone_take = {}
        for u, g in s.aircraft.items():
            if g not in available:
                continue
            selected = model.NewBoolVar(f"drone_{j}_{u}")
            drone_take[u] = selected
            opt = available[g]
            interval = model.NewOptionalIntervalVar(
                start, opt.duration, start + opt.duration, selected,
                f"flight_{j}_{u}")
            intervals_aircraft[u].append(interval)
        for g in available:
            model.Add(sum(drone_take[u] for u in drone_take if s.aircraft[u] == g) == take[g])
        aircraft_choices.append(drone_take)

        battery_take = {}
        for b, g in batteries.items():
            if g not in available:
                continue
            selected = model.NewBoolVar(f"battery_{j}_{b}")
            battery_take[b] = selected
            opt = available[g]
            interval = model.NewOptionalIntervalVar(
                start, opt.battery_span, start + opt.battery_span, selected,
                f"use_charge_{j}_{b}")
            intervals_battery[b].append(interval)
        for g in available:
            model.Add(sum(battery_take[b] for b in battery_take if batteries[b] == g) == take[g])
        battery_choices.append(battery_take)

    for intervals in intervals_aircraft.values():
        model.AddNoOverlap(intervals)
    for intervals in intervals_battery.values():
        model.AddNoOverlap(intervals)
    makespan = model.NewIntVar(0, horizon, "makespan")
    model.AddMaxEquality(makespan, ends)
    weighted = model.NewIntVar(0, 2_000_000_000, "weighted_delivery_ticks")
    energy = model.NewIntVar(0, 1_000_000_000, "energy_micro_kwh")
    model.Add(weighted == sum(weighted_terms))
    model.Add(energy == sum(energy_terms))
    if weighted_cap is not None:
        model.Add(weighted <= weighted_cap)
    if makespan_cap is not None:
        model.Add(makespan <= up(makespan_cap, scale))
    if energy_cap is not None:
        # Each selected route was individually rounded up to micro-kWh.
        model.Add(energy <= floor(energy_cap * ENERGY_SCALE + 1e-8) + len(jobs))

    # Supply a feasible starting point after conservative rounding.  The solver
    # is free to replace every hint, including routes' model and resource IDs.
    drone_ready = {u: 0 for u in s.aircraft}
    battery_ready = {b: 0 for b in batteries}
    for j, row in enumerate(baseline["sorties"]):
        g, u, b = row["model"], row["drone"], row["battery"]
        opt = options[j][g]
        start = max(drone_ready[u], battery_ready[b])
        model.AddHint(starts[j], start)
        model.AddHint(ends[j], start + opt.duration)
        for name, variable in choices[j].items():
            model.AddHint(variable, int(name == g))
        for name, variable in aircraft_choices[j].items():
            model.AddHint(variable, int(name == u))
        for name, variable in battery_choices[j].items():
            model.AddHint(variable, int(name == b))
        drone_ready[u] = start + opt.duration
        battery_ready[b] = start + opt.battery_span

    return model, dict(starts=starts, choices=choices, aircraft=aircraft_choices,
                       batteries=battery_choices, makespan=makespan,
                       weighted=weighted, energy=energy, horizon=horizon)


def assemble_plan(s: Scenario, sorties: list[dict]) -> dict:
    deliveries = {}
    for row in sorties:
        for bid, relative in row["route"]["delivery"].items():
            deliveries[bid] = dict(sortie=row["id"], zone=s.boxes[bid].zone,
                                   time=row["start"] + relative)
    objective = dict(
        weighted_delivery_seconds=sum(s.boxes[bid].priority * row["time"]
                                      for bid, row in deliveries.items()),
        weighted_soft_delay_seconds=sum(
            s.boxes[bid].priority * max(0, row["time"] - s.boxes[bid].desired)
            for bid, row in deliveries.items() if s.boxes[bid].hard_deadline is None),
        makespan=max(row["return_time"] for row in sorties),
        energy_kwh=sum(row["energy_kwh"] for row in sorties),
        sortie_count=len(sorties),
    )
    return dict(sorties=sorties, deliveries=deliveries, objective=objective)


def left_shift(s: Scenario, plan: dict) -> dict:
    """Remove idle time while preserving each selected resource's task order."""
    drone_ready = {u: 0.0 for u in s.aircraft}
    battery_ready = {f"{g}{i:02d}": 0.0 for g, count in s.battery_count.items()
                     for i in range(1, count + 1)}
    shifted = []
    for old in sorted(plan["sorties"], key=lambda row: (row["start"], row["id"])):
        start = max(drone_ready[old["drone"]], battery_ready[old["battery"]])
        if start > old["start"] + 1e-6:
            raise AssertionError("Left shift would violate CP-SAT resource order")
        end = start + old["route"]["duration"]
        ready = end + s.charge_time(old["return_soc"], s.battery_charge[old["model"]])
        shifted.append(dict(old, start=start, return_time=end, battery_ready=ready))
        drone_ready[old["drone"]] = end
        battery_ready[old["battery"]] = ready
    return assemble_plan(s, shifted)


def extract(s: Scenario, baseline: dict, options: list[dict[str, Option]],
            variables: dict, solver: cp_model.CpSolver, scale: int) -> dict:
    sorties = []
    for j, old in enumerate(baseline["sorties"]):
        g = next(g for g, var in variables["choices"][j].items() if solver.Value(var))
        u = next(u for u, var in variables["aircraft"][j].items() if solver.Value(var))
        b = next(b for b, var in variables["batteries"][j].items() if solver.Value(var))
        opt = options[j][g]
        route = opt.route
        start = solver.Value(variables["starts"][j]) / scale
        end = start + route["duration"]
        ready = end + s.charge_time(route["return_soc"], s.battery_charge[g])
        row = dict(id=old["id"], job=old["job"], drone=u, model=g, battery=b,
                   start=start, return_time=end, battery_ready=ready,
                   energy_kwh=route["energy_kwh"], return_soc=route["return_soc"],
                   visits=route["visits"], load_kg=route["load_kg"],
                   load_m3=route["load_m3"], route=route)
        sorties.append(row)
    return left_shift(s, assemble_plan(s, sorties))


def solve_case(s: Scenario, baseline: dict, case: str, scale: int,
               time_limit: float, workers: int, seed: int,
               weighted_cap_seconds: float | None = None,
               makespan_cap: float | None = None,
               energy_cap: float | None = None) -> tuple[dict | None, dict]:
    jobs, options = prepare(s, baseline, scale)
    cap = (floor(weighted_cap_seconds * scale + 1e-8)
           if weighted_cap_seconds is not None else None)
    model, variables = build_model(s, baseline, jobs, options, scale, cap,
                                   makespan_cap, energy_cap)
    target = variables["weighted"] if case == "main" else variables["energy"]
    model.Minimize(target)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_search_workers = workers
    solver.parameters.random_seed = seed
    solver.parameters.relative_gap_limit = 0.0
    start_clock = perf_counter()
    status = solver.Solve(model)
    elapsed = perf_counter() - start_clock
    report = dict(case=case, status=solver.StatusName(status),
                  time_limit_seconds=time_limit, elapsed_seconds=elapsed,
                  workers=workers, seed=seed, scale=scale,
                  model_options=sum(len(x) for x in options),
                  fixed_sorties=len(jobs),
                  weighted_cap_seconds=weighted_cap_seconds,
                  makespan_cap_seconds=makespan_cap,
                  energy_cap_kwh=energy_cap,
                  objective_bound=solver.BestObjectiveBound(),
                  objective_value=solver.ObjectiveValue() if status in
                  (cp_model.OPTIMAL, cp_model.FEASIBLE) else None)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, report
    plan = extract(s, baseline, options, variables, solver, scale)
    report["verification"] = verify_q2(s, plan)
    return plan, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--case", choices=("main", "alns", "both"), default="both")
    parser.add_argument("--scale", type=int, default=1, help="integer ticks per second")
    parser.add_argument("--time-limit", type=float, default=60.0,
                        help="CP-SAT seconds for each case")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--weighted-cap", type=float, default=None,
                        help="maximum weighted delivery time in s*priority")
    parser.add_argument("--makespan-cap", type=float, default=None,
                        help="maximum latest return time in seconds")
    parser.add_argument("--energy-cap", type=float, default=None,
                        help="maximum total energy in kWh")
    args = parser.parse_args()
    if args.scale < 1 or args.time_limit <= 0 or args.workers < 1:
        parser.error("scale, time-limit and workers must be positive")
    s = Scenario(args.data_dir)
    source = json.loads(args.input.read_text(encoding="utf-8"))
    baselines = dict(main=source, alns=source["alternatives"]["alns_zero_delay_efficient"])
    args.output.mkdir(parents=True, exist_ok=True)
    cases = ("main", "alns") if args.case == "both" else (args.case,)
    comparison = []
    for name in cases:
        baseline = baselines[name]
        verify_q2(s, {key: baseline[key] for key in ("sorties", "deliveries", "objective")})
        print(f"Optimizing {name}: {baseline['objective']}", flush=True)
        weighted_cap = args.weighted_cap
        if name == "alns" and weighted_cap is None:
            # The repository's existing ALNS comparison uses a 3% budget
            # relative to the primary plan's weighted delivery time.
            weighted_cap = source["objective"]["weighted_delivery_seconds"] * 1.03
        plan, report = solve_case(s, baseline, name, args.scale, args.time_limit,
                                  args.workers, args.seed, weighted_cap,
                                  args.makespan_cap, args.energy_cap)
        if plan is not None:
            (args.output / f"{name}_plan.json").write_text(
                json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
            report["baseline"] = baseline["objective"]
            report["candidate"] = plan["objective"]
        (args.output / f"{name}_solver.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)
        comparison.append(dict(case=name, source="baseline", **baseline["objective"]))
        if plan is not None:
            comparison.append(dict(case=name, source="cp_sat", **plan["objective"]))
    with (args.output / "comparison.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(comparison[0]))
        writer.writeheader()
        writer.writerows(comparison)


if __name__ == "__main__":
    main()
