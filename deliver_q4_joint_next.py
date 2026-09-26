"""Deliver the verified Q4 resource-saving retiming of the imported Q3 plan."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil

from model import Scenario
from q3_upgrade import dump_json
from q4_delivery import export
from deliver_q34_compromise import choose
from search_q34_compromise import read


ROOT = Path(__file__).resolve().parent
BASELINE = ROOT / "results_q3_imported/selection/primary/q3_plan.json"
CANDIDATE = ROOT / "results_q4_joint_next/three_lp_timely/q3_plan.json"
OUTPUT = ROOT / "results_q4_joint_next/selection/primary"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    parser.add_argument("--candidate", type=Path, default=CANDIDATE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()

    baseline = read(args.baseline)
    candidate = read(args.candidate)
    args.output.mkdir(parents=True, exist_ok=True)
    target = args.output / "q3_plan.json"
    if args.candidate.resolve() != target.resolve():
        shutil.copyfile(args.candidate, target)
    result, validation = export(Scenario(), target, args.output / "q4", figures=False)
    if validation["status"] != "PASS":
        raise AssertionError("Independent Q4 replay did not pass")
    chosen = {str(k): choose(result, k) for k in (2, 3)}
    old = read(args.baseline.parent / "protected_partitions.json")
    for k in (2, 3):
        if chosen[str(k)]["shortage_total"] >= old[str(k)]["shortage_total"]:
            raise AssertionError(f"The {k}-group shortage did not improve")
    if candidate["objective"]["weighted_soft_delay"] != 0:
        raise AssertionError("The candidate introduces a soft deadline violation")
    for key in ("makespan", "energy_kwh"):
        if candidate["objective"][key] > baseline["objective"][key] + 1e-5:
            raise AssertionError(f"The candidate increases {key}")

    dump_json(args.output / "balanced_partitions.json", dict(
        policy="For each K, filter CV <= 0.30, then minimize typed shortage, CV and total resources.",
        interpretation="CV 0.30 is a declared selection preference, not a contest hard constraint.",
        same_frozen_q3=True, partitions=chosen,
    ))
    changed = []
    baseline_sorties = {r["id"]: r for r in baseline["transport_sorties"]}
    for row in candidate["transport_sorties"]:
        previous = baseline_sorties[row["id"]]
        if abs(row["start"] - previous["start"]) > 1e-5:
            changed.append(dict(id=row["id"], old_start=previous["start"],
                                new_start=row["start"], shift=row["start"] - previous["start"],
                                box_ids=row["route"]["box_ids"]))
    dump_json(args.output / "comparison.json", dict(
        baseline_objective=baseline["objective"], candidate_objective=candidate["objective"],
        objective_delta={key: candidate["objective"][key] - baseline["objective"][key]
                         for key in ("makespan", "energy_kwh", "weighted_delivery_seconds",
                                     "weighted_soft_delay")},
        baseline_shortage={str(k): old[str(k)]["shortage_total"] for k in (2, 3)},
        candidate_shortage={str(k): chosen[str(k)]["shortage_total"] for k in (2, 3)},
        changed_transport_sorties=changed,
        q4_partition_count=len(result["partitions"]),
        q4_assignment_count=len(result["assignments"]),
        independent_q4_validation=validation["status"],
    ))
    print("PASS: Q3 fixed; Q4 shortages 8 to 7 and 12 to 11; independent replay PASS")


if __name__ == "__main__":
    main()
