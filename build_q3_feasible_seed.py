"""Build the reproducible transport-timing seed used by the strict Q3 run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import q3
from model import Scenario


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().parent / "q3_feasible_compressed_seed.json")
    args = parser.parse_args()
    source = args.root / "results_q2_time" / "frontier_24" / "makespan_plan.json"
    q2 = json.loads(source.read_text(encoding="utf-8"))
    fast = q3.solve(Scenario(args.data), q2, spacing=0.005)
    seed = {"sorties": fast["transport_sorties"], "deliveries": fast["deliveries"],
            "source_objective": fast["objective"],
            "note": "Old sampled Q3 is used only to generate a timing seed."}
    # These two S001 sorties are direct throughout.  Moving them to time zero
    # fits their aircraft and battery gaps and removes avoidable soft delay.
    for row in seed["sorties"]:
        if row["id"] not in {"T001", "T004"}:
            continue
        delta = -float(row["start"])
        row["start"] += delta
        row["return_time"] += delta
        row["battery_ready"] += delta
        for bid, relative in row["route"]["delivery"].items():
            seed["deliveries"][bid]["time"] = row["start"] + relative
    args.output.write_text(json.dumps(seed, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
