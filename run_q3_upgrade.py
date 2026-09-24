"""Run Q3 from the best saved Q2 plans and write the documented artefacts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
from pathlib import Path

from model import Scenario
from q3_upgrade import dump_json, solve


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def write_csv(path: Path, rows: list[dict], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    fields = fields or list(rows[0])
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def plan_candidates(root: Path) -> list[tuple[str, Path]]:
    return [
        ("balanced_24", root / "results_q2_time" / "frontier_24" / "weighted_plan.json"),
        ("delivery_23", root / "results_q2_time" / "frontier_23" / "weighted_plan.json"),
        ("fastest_25", root / "results_q2_time" / "frontier_25" / "weighted_plan.json"),
    ]


def emit(result: dict, out: Path) -> None:
    dump_json(out / "q3_plan.json", result)
    write_csv(out / "q3_deliveries.csv", [dict(box_id=k, **v) for k, v in result["deliveries"].items()])
    resource = []
    for row in result["transport_sorties"]:
        resource.append(dict(kind="transport", task=row["id"], resource=row["drone"],
                             energy_component=row["battery"], start=row["start"],
                             service_start=row["start"], service_end=row["return_time"],
                             return_time=row["return_time"], resource_ready=row["return_time"],
                             component_ready=row["battery_ready"], energy_kwh=row["energy_kwh"]))
    for row in result["relay_sorties"]:
        resource.append(dict(kind="relay", task=row["id"], resource=row["relay"],
                             energy_component=row["energy_component"], start=row["start"],
                             service_start=row["established"], service_end=row["service_end"],
                             return_time=row["return_time"], resource_ready=row["relay_ready"],
                             component_ready=row["component_ready"], energy_kwh=row["energy_kwh"]))
    write_csv(out / "q3_resource_timeline.csv", resource)
    write_csv(out / "q3_communication_intervals.csv", result["communication_intervals"])
    dump_json(out / "q3_continuity_certificate.json", {
        **result["certificate"], "communication": result["communication"],
        "relay_sorties": result["relay_sorties"],
    })
    dump_json(out / "q3_q4_compatibility.json", result["q4_compatibility"])
    dump_json(out / "q3_bound_report.json", {
        "status": "FEASIBLE",
        "upper_bound": result["objective"],
        "proved_facts": [
            "All Q2 boxes and transport resource assignments are preserved.",
            "Every transport phase interval has a certified direct or two-hop path.",
            "Relay aircraft, energy components, return reserve and recharge intervals are replayed.",
        ],
        "optimality_scope": "No global optimum is claimed for the continuous joint problem.",
        "finite_search_scope": result["search"],
    })


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--only", choices=["balanced_24", "delivery_23", "fastest_25"])
    parser.add_argument("--plan", type=Path, help="Run one explicit Q2 plan JSON")
    parser.add_argument("--label", default="custom_q2")
    parser.add_argument("--coarse", type=float, default=0.004)
    parser.add_argument("--local", type=float, default=0.0015)
    parser.add_argument("--interval", type=float, default=30.0)
    args = parser.parse_args()
    root = args.root.resolve()
    output = (args.output or root / "results_q3_upgrade").resolve()
    scenario = Scenario(args.data.resolve())
    results = []
    errors = []
    candidates = [(args.label, args.plan.resolve())] if args.plan else plan_candidates(root)
    for label, path in candidates:
        if args.only and label != args.only:
            continue
        if not path.exists():
            errors.append(dict(seed=label, error=f"missing {path}"))
            continue
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
            result = solve(scenario, plan, label, args.coarse, args.local, args.interval)
            seed_out = output / label
            emit(result, seed_out)
            results.append((label, path, result, seed_out))
            print(json.dumps({"seed": label, **result["objective"], **result["communication"]},
                             ensure_ascii=False))
        except Exception as exc:
            errors.append(dict(seed=label, error=f"{type(exc).__name__}: {exc}"))
            print(json.dumps(errors[-1], ensure_ascii=False))
    if not results:
        dump_json(output / "errors.json", errors)
        raise SystemExit("No Q3 seed produced a feasible plan")
    results.sort(key=lambda x: (x[2]["objective"]["weighted_soft_delay"],
                                x[2]["objective"]["weighted_delivery_seconds"],
                                x[2]["objective"]["makespan"],
                                x[2]["objective"]["energy_kwh"],
                                x[2]["objective"]["total_sorties"]))
    selected_label, selected_path, selected, selected_dir = results[0]
    for name in ("q3_plan.json", "q3_deliveries.csv", "q3_resource_timeline.csv",
                 "q3_communication_intervals.csv", "q3_continuity_certificate.json",
                 "q3_q4_compatibility.json", "q3_bound_report.json"):
        shutil.copy2(selected_dir / name, output / name)
    pareto = [dict(seed=label, **result["objective"],
                   direct_seconds=result["communication"]["direct_seconds"],
                   relay_seconds=result["communication"]["relay_seconds"],
                   relay_share=result["communication"]["relay_share"],
                   minimum_margin_db=result["communication"]["minimum_certified_margin_db"],
                   certificate=result["certificate"]["status"])
              for label, _, result, _ in results]
    write_csv(output / "q3_pareto.csv", pareto)
    # A failed conservative bound is UNKNOWN, not an outage proof. Recompute
    # the path selection and subdivisions under each loss perturbation.
    from q3_certificate import certify
    robust = []
    for extra in (0, 1, 2, 3):
        _, communication, certificate = certify(scenario, selected, extra, stop_on_failure=True)
        robust.append(dict(extra_loss_db=extra, fixed_plan_status=certificate["status"],
                           unknown_seconds=communication["unknown_seconds"],
                           reoptimization_run=False))
    write_csv(output / "q3_robustness.csv", robust)
    manifest_files = [p for p in output.iterdir() if p.is_file() and p.name != "manifest.json"]
    dump_json(output / "manifest.json", {
        "selected_seed": selected_label,
        "selected_q2_plan": str(selected_path),
        "policy": ["weighted_soft_delay", "weighted_delivery_seconds", "makespan",
                   "energy_kwh", "total_sorties"],
        "errors": errors,
        "files": {p.name: {"sha256": sha256(p), "bytes": p.stat().st_size}
                  for p in sorted(manifest_files)},
    })
    print(json.dumps({"selected": selected_label, "output": str(output),
                      "objective": selected["objective"],
                      "communication": selected["communication"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
