"""Audit all original and refined Q2 plans on the same comparison axes.

These are descriptive measures, not additional optimization constraints.
Every plan is verified against the original Q2 model before export.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from math import ceil
from pathlib import Path

from model import BASE, Scenario
from verify import verify_q2


ROOT = Path(__file__).resolve().parent
METRICS = ("weighted_soft_delay_seconds", "weighted_delivery_seconds",
           "makespan_seconds", "energy_kwh", "sorties")


def percentile_nearest(values: list[float], fraction: float) -> float:
    return sorted(values)[ceil(fraction * len(values)) - 1]


def peak_flights(sorties: list[dict]) -> int:
    events = sorted([(r["start"], 1) for r in sorties]
                    + [(r["return_time"], -1) for r in sorties])
    active = peak = 0
    for _, change in events:
        active += change
        peak = max(peak, active)
    return peak


def summarize(s: Scenario, name: str, plan: dict) -> dict:
    verify_q2(s, plan)
    boxes = s.boxes
    times = {bid: item["time"] for bid, item in plan["deliveries"].items()}
    hard_slack = [box.hard_deadline - times[bid] for bid, box in boxes.items()
                  if box.hard_deadline is not None]
    first_slack = [box.first_deadline - times[bid] for bid, box in boxes.items()
                   if box.first and box.first_deadline is not None]
    medical_slack = [box.desired - times[bid] for bid, box in boxes.items()
                     if box.kind == "医疗物资"]
    soft_lateness = [max(0, times[bid] - box.desired)
                     for bid, box in boxes.items() if box.hard_deadline is None]
    drone_load = Counter(row["drone"] for row in plan["sorties"])
    model_load = Counter(row["model"] for row in plan["sorties"])
    used_by_model = Counter(s.aircraft[drone] for drone in drone_load)
    zone_last = [max(times[box.id] for box in zone_boxes)
                 for zone_boxes in s.zone_boxes.values()]
    total_mass = sum(box.kg for box in boxes.values())
    objective = plan["objective"]
    return dict(
        scheme=name,
        boxes=len(times),
        hard_on_time=sum(box.hard_deadline is not None
                         and times[bid] <= box.hard_deadline + 1e-6
                         for bid, box in boxes.items()),
        hard_total=len(hard_slack),
        first_on_time=sum(box.first and box.first_deadline is not None
                          and times[bid] <= box.first_deadline + 1e-6
                          for bid, box in boxes.items()),
        first_total=len(first_slack),
        medical_on_time=sum(box.kind == "医疗物资"
                            and times[bid] <= box.desired + 1e-6
                            for bid, box in boxes.items()),
        medical_total=len(medical_slack),
        min_hard_slack_seconds=min(hard_slack),
        min_first_slack_seconds=min(first_slack),
        min_medical_slack_seconds=min(medical_slack),
        weighted_soft_delay_seconds=objective["weighted_soft_delay_seconds"],
        max_soft_lateness_seconds=max(soft_lateness),
        weighted_delivery_seconds=objective["weighted_delivery_seconds"],
        p90_box_delivery_seconds=percentile_nearest(list(times.values()), 0.9),
        latest_box_delivery_seconds=max(times.values()),
        latest_zone_completion_seconds=max(zone_last),
        makespan_seconds=objective["makespan"],
        energy_kwh=objective["energy_kwh"],
        energy_kwh_per_delivered_kg=objective["energy_kwh"] / total_mass,
        sorties=objective["sortie_count"],
        multi_zone_sorties=sum(len(row["visits"]) > 1 for row in plan["sorties"]),
        sorties_A=model_load["A"], sorties_B=model_load["B"], sorties_C=model_load["C"],
        used_aircraft_A=used_by_model["A"],
        used_aircraft_B=used_by_model["B"],
        used_aircraft_C=used_by_model["C"],
        peak_simultaneous_aircraft=peak_flights(plan["sorties"]),
        max_sorties_per_drone=max(drone_load.values()),
        min_return_soc_pct=100 * min(row["return_soc"] for row in plan["sorties"]),
    )


def fleet_mix(s: Scenario, name: str, plan: dict) -> list[dict]:
    """Separate physical aircraft count from the number of sorties they fly."""
    rows = []
    makespan = plan["objective"]["makespan"]
    for model in s.transport:
        trips = [trip for trip in plan["sorties"] if trip["model"] == model]
        available = sum(kind == model for kind in s.aircraft.values())
        occupied_seconds = sum(trip["return_time"] - trip["start"] for trip in trips)
        kg = sum(s.boxes[bid].kg for trip in trips for _, box_ids in trip["visits"]
                 for bid in box_ids)
        rows.append(dict(
            scheme=name, model=model, available_aircraft=available,
            used_aircraft=len({trip["drone"] for trip in trips}),
            peak_simultaneous_aircraft=peak_flights(trips),
            sorties=len(trips), delivered_kg=kg,
            average_kg_per_sortie=kg / len(trips) if trips else 0,
            energy_kwh=sum(trip["energy_kwh"] for trip in trips),
            occupied_hours=occupied_seconds / 3600,
            aircraft_occupancy_pct=100 * occupied_seconds / (available * makespan),
        ))
    return rows


def dominates(a: dict, b: dict) -> bool:
    return all(a[k] <= b[k] + 1e-7 for k in METRICS) and any(
        a[k] < b[k] - 1e-7 for k in METRICS)


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=BASE)
    parser.add_argument("--original", type=Path, default=ROOT / "results_q2" / "q2.json")
    parser.add_argument("--refined", type=Path, default=ROOT / "results_q2_refined" / "q2.json")
    parser.add_argument("--output", type=Path, default=ROOT / "results_q2_refined")
    args = parser.parse_args()
    scenario = Scenario(args.data_dir)
    original = json.loads(args.original.read_text(encoding="utf-8"))
    refined = json.loads(args.refined.read_text(encoding="utf-8"))
    plans = {
        "original_main": original,
        **{name: plan for name, plan in original["alternatives"].items()},
        "refined_main": refined,
        "refined_energy": refined["alternatives"]["refined_energy"],
    }
    rows = [summarize(scenario, name, plan) for name, plan in plans.items()]
    mix_rows = [row for name, plan in plans.items()
                for row in fleet_mix(scenario, name, plan)]
    for row in rows:
        row["pareto_efficient_5_metrics"] = not any(
            dominates(other, row) for other in rows if other is not row)
    args.output.mkdir(parents=True, exist_ok=True)
    write_csv(args.output / "q2_multiaxis_comparison.csv", rows)
    write_csv(args.output / "q2_fleet_mix.csv", mix_rows)
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
