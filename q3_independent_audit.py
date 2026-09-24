"""Independent dense diagnostic replay for a produced Q3 plan.

This checker intentionally uses ``Scenario.link_margin`` and one-second event
sampling instead of the optimizer's swept-triangle certificate.  It is a
cross-check, not a replacement for the continuous certificate.
"""

from __future__ import annotations

import argparse
import json
from math import ceil
from pathlib import Path

from model import Scenario, phase_position


def overlap_errors(rows: list[dict], resource: str, end: str) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row[resource], []).append(row)
    errors = []
    for rid, group in groups.items():
        group.sort(key=lambda x: (x["start"], x["id"]))
        for a, b in zip(group, group[1:]):
            if b["start"] < a[end] - 1e-7:
                errors.append(dict(resource=rid, first=a["id"], second=b["id"],
                                   first_release=a[end], second_start=b["start"]))
    return errors


def audit(s: Scenario, plan: dict, step: float = 1.0) -> dict:
    transports = plan["transport_sorties"]
    relays = plan["relay_sorties"]
    hub = s.nodes["O01"]
    gateway = (hub.lon, hub.lat, hub.ground + s.gateway_agl)
    outages = []
    samples = direct = relayed = 0
    minimum_margin = float("inf")
    for row in transports:
        points: dict[float, tuple[float, float, float]] = {}
        for phase in row["route"]["phases"]:
            duration = phase["t1"] - phase["t0"]
            n = max(1, ceil(duration / step))
            for k in range(n + 1):
                local = phase["t0"] + duration * k / n
                points[round(row["start"] + local, 8)] = phase_position(phase, local)
        for time, position in sorted(points.items()):
            samples += 1
            dm = s.link_margin(position, gateway, "transport", "gateway")
            best = dm
            mode = "direct"
            relay_id = None
            if dm < 0:
                for relay in relays:
                    if relay["established"] - 1e-7 <= time <= relay["service_end"] + 1e-7:
                        point = (relay["lon"], relay["lat"], relay["altitude"])
                        access = s.link_margin(position, point, "transport", "relay_access")
                        backhaul = s.link_margin(point, gateway, "relay_backhaul", "gateway")
                        margin = min(access, backhaul)
                        if margin > best:
                            best, mode, relay_id = margin, "relay", relay["id"]
            minimum_margin = min(minimum_margin, best)
            if best < 0:
                outages.append(dict(sortie=row["id"], time=time, margin_db=best,
                                    lon=position[0], lat=position[1], altitude=position[2]))
            elif mode == "direct":
                direct += 1
            else:
                relayed += 1

    deadline_errors = []
    box_ids = []
    for row in transports:
        for bid, relative in row["route"]["delivery"].items():
            box_ids.append(bid)
            when = row["start"] + relative
            deadline = s.boxes[bid].hard_deadline
            if deadline is not None and when > deadline + 1e-7:
                deadline_errors.append(dict(box=bid, delivery=when, deadline=deadline))
    coverage_errors = sorted(set(s.boxes) ^ set(box_ids))
    if len(box_ids) != len(set(box_ids)):
        coverage_errors.append("duplicate_box_assignment")

    resource_errors = []
    resource_errors += overlap_errors(transports, "drone", "return_time")
    resource_errors += overlap_errors(transports, "battery", "battery_ready")
    resource_errors += overlap_errors(relays, "relay", "relay_ready")
    resource_errors += overlap_errors(relays, "energy_component", "component_ready")
    energy_errors = []
    for row in relays:
        check = s.relay_sortie(row["lon"], row["lat"], row["agl"],
                               row["service_end"] - row["established"])
        if abs(check["energy_kwh"] - row["energy_kwh"]) > 1e-7:
            energy_errors.append(dict(relay=row["id"], recorded=row["energy_kwh"],
                                      recomputed=check["energy_kwh"]))

    passed = not (outages or deadline_errors or coverage_errors or resource_errors or energy_errors)
    return {
        "status": "PASS" if passed else "FAIL",
        "scope": "independent one-second diagnostic replay; continuous proof is separate",
        "sample_step_seconds": step,
        "sample_count": samples,
        "direct_sample_count": direct,
        "relay_sample_count": relayed,
        "outage_sample_count": len(outages),
        "minimum_sampled_margin_db": minimum_margin,
        "deadline_errors": deadline_errors,
        "coverage_errors": coverage_errors,
        "resource_errors": resource_errors,
        "relay_energy_errors": energy_errors,
        "outages_first_20": outages[:20],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--step", type=float, default=1.0)
    args = parser.parse_args()
    result = audit(Scenario(args.data), json.loads(args.plan.read_text(encoding="utf-8")), args.step)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
