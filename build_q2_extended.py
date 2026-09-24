"""Package independently verified Q2 route-search plans and comparison tables."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from shutil import copyfile

from model import BASE, Scenario
from q2_multiaxis_compare import dominates, fleet_mix, summarize, write_csv
from run import delivery_rows, save_csv, transport_rows, write_q2_workbook
from verify import verify_q2


ROOT = Path(__file__).resolve().parent
SOURCES = {
    "28_earliest": "results_q2_refined/q2_main_plan.json",
    "25_previous_efficient": "results_q2_refined/q2_energy_plan.json",
    "24_fast_efficient": "results_q2_frontier/24_weighted_60s/plan.json",
    "24_min_energy_same_finish": "results_q2_merge_search_cap7540/best_plan.json",
    "23_balanced": "results_q2_frontier/23_weighted_60s/plan.json",
    "23_min_energy": "results_q2_merge_search_23/best_plan.json",
    "22_low_sorties": "results_q2_merge_search_22/best_plan.json",
    "21_low_sorties": "results_q2_frontier/21_weighted/plan.json",
    "20_repacked": "results_q2_ejection/weighted_refined/plan.json",
    "19_best_delivery": "results_q2_three_to_two_weighted/best_plan.json",
    "19_fast": "results_q2_three_to_two_fast/weighted_refined/plan.json",
    "19_min_energy": "results_q2_three_to_two/energy_best_weighted/plan.json",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=BASE)
    parser.add_argument("--output", type=Path, default=ROOT / "results_q2_extended")
    args = parser.parse_args()
    s = Scenario(args.data_dir)
    args.output.mkdir(parents=True, exist_ok=True)
    plan_dir = args.output / "plans"
    plan_dir.mkdir(exist_ok=True)
    plans = {}
    certificates = {}
    for name, relative in SOURCES.items():
        source = ROOT / relative
        packaged = plan_dir / f"{name}.json"
        if not source.exists():
            source = packaged
        plan = json.loads(source.read_text(encoding="utf-8"))
        certificates[name] = dict(source=str(source.relative_to(ROOT)),
                                  **verify_q2(s, plan))
        plans[name] = plan
        if source.resolve() != packaged.resolve():
            copyfile(source, packaged)
    rows = [summarize(s, name, plan) for name, plan in plans.items()]
    mix = [row for name, plan in plans.items() for row in fleet_mix(s, name, plan)]
    for row in rows:
        row["pareto_efficient_5_metrics"] = not any(
            dominates(other, row) for other in rows if other is not row)
    write_csv(args.output / "comparison.csv", rows)
    write_csv(args.output / "fleet_mix.csv", mix)
    for name in ("24_fast_efficient", "23_balanced", "20_repacked",
                 "19_best_delivery", "19_fast", "19_min_energy"):
        plan = plans[name]
        write_q2_workbook(args.output / f"result_template_{name}.xlsx", plan)
        save_csv(args.output / f"sorties_{name}.csv",
                 ["sortie", "drone", "model", "battery", "start_s", "visits",
                  "return_s", "energy_kwh", "return_soc_pct", "boxes"],
                 transport_rows(plan["sorties"]))
        save_csv(args.output / f"deliveries_{name}.csv",
                 ["box", "sortie", "zone", "delivered_s"],
                 delivery_rows(plan["deliveries"]))
    (args.output / "verification.json").write_text(
        json.dumps(certificates, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps([{k: row[k] for k in (
        "scheme", "weighted_soft_delay_seconds", "weighted_delivery_seconds",
        "makespan_seconds", "energy_kwh", "sorties",
        "pareto_efficient_5_metrics")}
        for row in rows], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
