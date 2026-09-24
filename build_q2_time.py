"""Compare verified Q2 time-search plans and package useful choices.

The fastest plan minimizes the last return time among the saved candidates.
The balanced plan dominates the previously delivered 24-sortie plan on the
same five axes.  The delivery plan minimizes weighted delivery among plans
that dominate the original 28-sortie baseline.  Selection is over curated,
independently verified candidates; it is not an optimality claim.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from shutil import copyfile

from model import BASE, Scenario
from q2_multiaxis_compare import dominates, fleet_mix, summarize, write_csv
from run import delivery_rows, save_csv, transport_rows
from verify import verify_q2


ROOT = Path(__file__).resolve().parent
BASELINE = ROOT / "results_q2_extended" / "plans" / "28_earliest.json"
PREVIOUS_24 = ROOT / "results_q2_time" / "research_24" / "s006_food_to_j002_weighted_plan.json"
DEFAULT_SEARCH = ROOT / "results_q2_time"
CURATED_DIRS = (
    "fast26_research", "fixed28_seed2026", "research_24",
    "tail_repack_24", "tail_repack_24_early", "tail_repack_24_time",
    "tail_repack_25", "tail_repack_26", "tail_repack_27",
    "frontier_23", "frontier_24", "frontier_25",
)


def candidate_files(search_dir: Path) -> list[tuple[str, Path]]:
    """Find saved candidates, excluding temporary experiments and copies."""
    candidates = [("28_earliest", BASELINE)]
    for dirname in CURATED_DIRS:
        for path in sorted((search_dir / dirname).rglob("*plan.json")):
            label = "__".join(path.relative_to(search_dir).with_suffix("").parts)
            candidates.append((label, path))
    if len(candidates) == 1:
        raise ValueError(f"No time-search plans found under {search_dir}")
    return candidates


def export_plan(s: Scenario, name: str, plan: dict, output: Path) -> None:
    save_csv(output / f"sorties_{name}.csv",
             ["sortie", "drone", "model", "battery", "start_s", "visits",
              "return_s", "energy_kwh", "return_soc_pct", "boxes"],
             transport_rows(plan["sorties"]))
    save_csv(output / f"deliveries_{name}.csv",
             ["box", "sortie", "zone", "delivered_s"],
             delivery_rows(plan["deliveries"]))
    resource_rows = []
    for row in plan["sorties"]:
        resource_rows.extend((
            dict(sortie=row["id"], type="aircraft", resource=row["drone"],
                 start_s=row["start"], end_s=row["return_time"]),
            dict(sortie=row["id"], type="battery_and_charge",
                 resource=row["battery"], start_s=row["start"],
                 end_s=row["battery_ready"]),
        ))
    save_csv(output / f"resource_timeline_{name}.csv",
             ["sortie", "type", "resource", "start_s", "end_s"],
             resource_rows)
    deadline_rows = []
    for bid, item in sorted(plan["deliveries"].items()):
        box = s.boxes[bid]
        deadline_rows.append(dict(
            box=bid, zone=box.zone, kind=box.kind,
            desired_s=box.desired, hard_deadline_s=box.hard_deadline,
            delivered_s=item["time"],
            hard_slack_s=(box.hard_deadline - item["time"]
                          if box.hard_deadline is not None else ""),
            soft_lateness_s=(max(0, item["time"] - box.desired)
                             if box.hard_deadline is None else ""),
        ))
    save_csv(output / f"deadline_audit_{name}.csv",
             ["box", "zone", "kind", "desired_s", "hard_deadline_s",
              "delivered_s", "hard_slack_s", "soft_lateness_s"],
             deadline_rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=BASE)
    parser.add_argument("--search-dir", type=Path, default=DEFAULT_SEARCH)
    parser.add_argument("--output", type=Path,
                        default=DEFAULT_SEARCH / "selection")
    args = parser.parse_args()
    s = Scenario(args.data_dir)
    plans = {}
    rows = []
    certificates = {}
    sources = {}
    for name, path in candidate_files(args.search_dir):
        plan = json.loads(path.read_text(encoding="utf-8"))
        certificates[name] = verify_q2(s, plan)
        plans[name] = plan
        row = summarize(s, name, plan)
        rows.append(row)
        sources[name] = str(path.relative_to(ROOT))
    baseline = rows[0]
    previous_24_source = str(PREVIOUS_24.relative_to(ROOT))
    previous_24 = next(row for row in rows
                       if sources[row["scheme"]] == previous_24_source)
    for row in rows:
        row["dominates_28_earliest"] = dominates(row, baseline)
        row["dominates_previous_24"] = dominates(row, previous_24)
        row["pareto_efficient_5_metrics"] = not any(
            dominates(other, row) for other in rows if other is not row)
        row["source_file"] = sources[row["scheme"]]
    rows.sort(key=lambda row: (row["makespan_seconds"],
                               row["weighted_delivery_seconds"]))
    fastest = rows[0]
    balanced_options = [row for row in rows
                        if row["dominates_previous_24"]
                        and row["weighted_soft_delay_seconds"] <= 1e-7]
    if not balanced_options:
        raise ValueError("No saved plan dominates the previous 24-sortie plan")
    balanced = balanced_options[0]
    delivery_options = [row for row in rows
                        if row["dominates_28_earliest"]
                        and row["weighted_soft_delay_seconds"] <= 1e-7]
    delivery = min(delivery_options,
                   key=lambda row: (row["weighted_delivery_seconds"],
                                    row["makespan_seconds"],
                                    row["energy_kwh"], row["sorties"]))
    args.output.mkdir(parents=True, exist_ok=True)
    write_csv(args.output / "comparison.csv", rows)
    mix = [row for name, plan in plans.items()
           for row in fleet_mix(s, name, plan)]
    write_csv(args.output / "fleet_mix.csv", mix)
    (args.output / "verification.json").write_text(
        json.dumps(certificates, ensure_ascii=False, indent=2), encoding="utf-8")
    for choice, selected in (("balanced", balanced), ("fastest", fastest),
                             ("delivery", delivery)):
        name = selected["scheme"]
        copyfile(ROOT / sources[name], args.output / f"{choice}.json")
        export_plan(s, choice, plans[name], args.output)
    report = {
        "baseline": {key: baseline[key] for key in (
            "scheme", "weighted_soft_delay_seconds", "weighted_delivery_seconds",
            "makespan_seconds", "energy_kwh", "sorties")},
        "previous_24": {key: previous_24[key] for key in (
            "scheme", "weighted_soft_delay_seconds", "weighted_delivery_seconds",
            "makespan_seconds", "energy_kwh", "sorties")},
        "balanced": balanced,
        "fastest": fastest,
        "delivery": delivery,
        "candidate_count": len(rows),
        "selection_scope": "verified saved plans only; no global optimality proof",
    }
    (args.output / "selection.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"balanced": balanced["scheme"],
                      "fastest": fastest["scheme"],
                      "delivery": delivery["scheme"],
                      "candidate_count": len(rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
