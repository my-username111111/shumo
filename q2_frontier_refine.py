"""Refine a verified Q2 route set under explicit energy and completion budgets."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from model import BASE, Scenario
from q2_exact_schedule import solve_case
from verify import verify_q2


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=BASE)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--objective", choices=("weighted", "makespan", "energy"),
                        default="weighted")
    parser.add_argument("--energy-cap", type=float)
    parser.add_argument("--makespan-cap", type=float)
    parser.add_argument("--weighted-cap", type=float)
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args()
    s = Scenario(args.data_dir)
    source = json.loads(args.input.read_text(encoding="utf-8"))
    verify_q2(s, source)
    case = {"weighted": "main", "makespan": "makespan", "energy": "alns"}[
        args.objective]
    plan, report = solve_case(s, source, case, 1, args.seconds, 8, args.seed,
                              weighted_cap_seconds=args.weighted_cap,
                              makespan_cap=args.makespan_cap,
                              energy_cap=args.energy_cap)
    report["source"] = str(args.input)
    report["source_objective"] = source["objective"]
    if plan is not None:
        verify_q2(s, plan)
        report["candidate_objective"] = plan["objective"]
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "solver.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if plan is not None:
        (args.output / "plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
