"""Retiming an accepted Q4 candidate while preserving both selected partitions."""

from __future__ import annotations

import argparse
from pathlib import Path

from model import Scenario
from optimize_q3_timing import optimize
from q3_upgrade import dump_json
from q4_exact import solve as solve_q4
from search_q34_compromise import read
from deliver_q34_compromise import choose


ROOT = Path(__file__).resolve().parent
BASELINE = ROOT / "results_q3_imported/selection/primary/q3_plan.json"
CANDIDATE = ROOT / "results_q4_joint_next/three_time500/q3_plan.json"
OUTPUT = ROOT / "results_q4_joint_next/three_lp_timely"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    parser.add_argument("--candidate", type=Path, default=CANDIDATE)
    parser.add_argument("--candidate-q4", type=Path,
                        help="Optional saved Q4 result; otherwise recompute it from the candidate")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--two-partition", default="P1256_34")
    parser.add_argument("--three-partition", default="P126_34_5")
    args = parser.parse_args()

    baseline, candidate = read(args.baseline), read(args.candidate)
    scenario = Scenario()
    q4 = (read(args.candidate_q4) if args.candidate_q4 else
          solve_q4(scenario, candidate, args.candidate.read_bytes()))
    two = next(p for p in q4["partitions"] if p["id"] == args.two_partition)
    three = next(p for p in q4["partitions"] if p["id"] == args.three_partition)
    if two["group_count"] != 2 or three["group_count"] != 3:
        raise ValueError("Selected partitions have incorrect group counts")
    chains = [(two["id"], group["id"], key)
              for group in two["groups"] for key in group["resource_need"]
              if key.startswith(("A_", "B_", "C_"))]
    plan, final_q4, report = optimize(
        scenario, candidate, 5.0, policy="timely",
        makespan_cap=baseline["objective"]["makespan"] + 1e-5,
        preserve_partition=three["id"], preserve_q4_resources=chains,
    )
    selected = {str(k): choose(final_q4, k) for k in (2, 3)}
    if selected["2"]["shortage_total"] > 7 or selected["3"]["shortage_total"] > 11:
        raise AssertionError("The Q4 savings were lost during timing refinement")
    if plan["objective"]["energy_kwh"] > baseline["objective"]["energy_kwh"] + 1e-5:
        raise AssertionError("Energy increased during timing refinement")
    dump_json(args.output / "q3_plan.json", plan)
    dump_json(args.output / "q4_results.json", final_q4)
    dump_json(args.output / "report.json", report)
    dump_json(args.output / "protected_partitions.json", selected)
    print("PASS: time and energy protected; Q4 shortages at most 7 and 11")


if __name__ == "__main__":
    main()
