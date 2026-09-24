"""Package and independently audit the fixed-route Q2 schedule improvements."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from model import Scenario
from run import export_q2, save_csv, save_json
from verify import verify_q2


ROOT = Path(__file__).resolve().parent
DATA = ROOT.parent / "D题" / "数据"
BASELINE = ROOT / "results_q2" / "q2.json"
MAIN = ROOT / "results_q2_exact_main_dominance" / "main_plan.json"
ENERGY = ROOT / "results_q2_exact_alns_strict" / "alns_plan.json"
OUTPUT = ROOT / "results_q2_refined"
METRICS = ("weighted_soft_delay_seconds", "weighted_delivery_seconds",
           "makespan", "energy_kwh", "sortie_count")


def core(plan: dict) -> dict:
    return {key: plan[key] for key in ("sorties", "deliveries", "objective")}


def fixed_routes(old: dict, new: dict) -> bool:
    signature = lambda plan: {row["job"]: row["visits"] for row in plan["sorties"]}
    return signature(old) == signature(new)


def audit_dominance(old: dict, new: dict, label: str) -> None:
    if not fixed_routes(old, new):
        raise AssertionError(f"{label}: box groups or zone visit orders changed")
    for metric in METRICS:
        previous, current = old["objective"][metric], new["objective"][metric]
        if current > previous + 1e-7:
            raise AssertionError(f"{label}: {metric} worsened: {previous} -> {current}")
    if new["objective"]["weighted_delivery_seconds"] >= old["objective"]["weighted_delivery_seconds"]:
        raise AssertionError(f"{label}: weighted delivery did not improve")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA)
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    parser.add_argument("--main", type=Path, default=MAIN)
    parser.add_argument("--energy", type=Path, default=ENERGY)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()

    scenario = Scenario(args.data_dir)
    original = json.loads(args.baseline.read_text(encoding="utf-8"))
    old_main = core(original)
    old_energy = core(original["alternatives"]["alns_zero_delay_efficient"])
    new_main = core(json.loads(args.main.read_text(encoding="utf-8")))
    new_energy = core(json.loads(args.energy.read_text(encoding="utf-8")))

    for name, plan in (("original_main", old_main), ("original_energy", old_energy),
                       ("refined_main", new_main), ("refined_energy", new_energy)):
        verify_q2(scenario, plan)
    audit_dominance(old_main, new_main, "main")
    audit_dominance(old_energy, new_energy, "energy")

    combined = dict(new_main, alternatives={
        "original_main": old_main,
        "original_energy": old_energy,
        "refined_energy": new_energy,
    })
    checked = verify_q2(scenario, combined)
    export_q2(args.output, scenario, combined, checked)
    save_json(args.output / "q2_main_plan.json", new_main)
    save_json(args.output / "q2_energy_plan.json", new_energy)
    for source, target in ((args.main.parent / "main_solver.json", "q2_main_solver.json"),
                           (args.energy.parent / "alns_solver.json", "q2_energy_solver.json")):
        save_json(args.output / target, json.loads(source.read_text(encoding="utf-8")))

    comparison = []
    for case, old, new in (("main", old_main, new_main),
                           ("energy", old_energy, new_energy)):
        for metric in METRICS:
            before, after = old["objective"][metric], new["objective"][metric]
            comparison.append(dict(case=case, metric=metric, original=before,
                                   refined=after, change=after - before,
                                   change_pct=(100 * (after - before) / before if before else 0)))
    save_csv(args.output / "q2_comparison.csv",
             ["case", "metric", "original", "refined", "change", "change_pct"], comparison)
    print(json.dumps(dict(verification=checked, main=new_main["objective"],
                          energy=new_energy["objective"]), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
