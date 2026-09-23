"""Independent replay checks for exported Q1–Q4 solutions."""

from __future__ import annotations

from collections import Counter, defaultdict
from math import ceil, isclose

from model import Scenario, phase_position
from q4 import coupled_components, resource_need


def _close(actual: float, expected: float, label: str, tolerance: float = 1e-5) -> None:
    if not isclose(actual, expected, rel_tol=tolerance, abs_tol=tolerance):
        raise AssertionError(f"{label}: {actual} != {expected}")


def _no_overlap(rows: list[dict], key: str, start_key: str, end_key: str, label: str) -> None:
    grouped = defaultdict(list)
    for row in rows:
        grouped[row[key]].append((row[start_key], row[end_key], row["id"]))
    for resource, periods in grouped.items():
        periods.sort()
        for a, b in zip(periods, periods[1:]):
            if a[1] > b[0] + 1e-6:
                raise AssertionError(f"{label} {resource} overlaps: {a[2]} and {b[2]}")


def verify_q1(s: Scenario, q1: dict) -> dict:
    seen = []
    for zone, plan in q1["zone_plans"].items():
        for batch in plan["batches"]:
            result = s.route(batch["model"], [(zone, batch["box_ids"])])
            _close(result["energy_kwh"], batch["energy"], "Q1 energy")
            _close(result["duration"], batch["duration"], "Q1 duration")
            _close(result["return_soc"], batch["return_soc"], "Q1 return SOC")
            seen.extend(batch["box_ids"])
    counts = Counter(seen)
    if set(counts) != set(s.boxes) or any(n != 1 for n in counts.values()):
        raise AssertionError("Q1 does not deliver every box exactly once")
    return dict(boxes=len(seen), sorties=sum(x["sorties"] for x in q1["zone_plans"].values()))


def _verify_transport(s: Scenario, rows: list[dict], deliveries: dict, label: str) -> dict:
    seen = []
    rebuilt_delivery = {}
    for row in rows:
        replay = s.route(row["model"], [(z, list(boxes)) for z, boxes in row["visits"]])
        _close(replay["energy_kwh"], row["energy_kwh"], f"{label} energy {row['id']}")
        _close(row["start"] + replay["duration"], row["return_time"], f"{label} return {row['id']}")
        _close(replay["return_soc"], row["return_soc"], f"{label} SOC {row['id']}")
        expected_ready = row["return_time"] + s.charge_time(row["return_soc"], s.battery_charge[row["model"]])
        _close(expected_ready, row["battery_ready"], f"{label} battery recharge {row['id']}")
        seen.extend(replay["box_ids"])
        for bid, relative in replay["delivery"].items():
            rebuilt_delivery[bid] = row["start"] + relative
    counts = Counter(seen)
    if set(counts) != set(s.boxes) or any(n != 1 for n in counts.values()):
        raise AssertionError(f"{label} does not deliver every box exactly once")
    if set(deliveries) != set(s.boxes):
        raise AssertionError(f"{label} delivery table does not cover every box")
    for bid, expected in rebuilt_delivery.items():
        _close(expected, deliveries[bid]["time"], f"{label} box time {bid}")
        hard = s.boxes[bid].hard_deadline
        if hard is not None and expected > hard + 1e-6:
            raise AssertionError(f"{label} hard deadline exceeded: {bid}")
    _no_overlap(rows, "drone", "start", "return_time", f"{label} transport aircraft")
    _no_overlap(rows, "battery", "start", "battery_ready", f"{label} transport battery")
    return dict(boxes=len(seen), sorties=len(rows), hard_boxes=sum(b.hard_deadline is not None for b in s.boxes.values()))


def verify_q2(s: Scenario, q2: dict) -> dict:
    return _verify_transport(s, q2["sorties"], q2["deliveries"], "Q2")


def verify_q3(s: Scenario, q3: dict, sample_seconds: float = 1.0) -> dict:
    transport = _verify_transport(s, q3["transport_sorties"], q3["deliveries"], "Q3")
    relays = q3["relay_sorties"]
    _no_overlap(relays, "relay", "start", "relay_ready", "Q3 relay aircraft")
    _no_overlap(relays, "energy_component", "start", "component_ready", "Q3 relay energy component")
    gateway_node = s.nodes["O01"]
    gateway = (gateway_node.lon, gateway_node.lat, gateway_node.ground + s.gateway_agl)
    for row in relays:
        if not s.dem.inside(row["lon"], row["lat"]):
            raise AssertionError(f"Relay outside DEM: {row['id']}")
        if row["agl"] > s.relay.max_agl + 1e-6:
            raise AssertionError(f"Relay too high: {row['id']}")
        if s.link_margin((row["lon"], row["lat"], row["altitude"]), gateway,
                         "relay_backhaul", "gateway") < 0:
            raise AssertionError(f"Relay backhaul unavailable: {row['id']}")
        duration = row["service_end"] - row["established"]
        trip = s.relay_sortie(row["lon"], row["lat"], row["agl"], duration)
        _close(trip["energy_kwh"], row["energy_kwh"], f"Q3 relay energy {row['id']}")
        _close(row["start"] + trip["duration"], row["return_time"], f"Q3 relay return {row['id']}")
        _close(row["return_time"] + s.relay.turnaround, row["relay_ready"], f"Q3 relay turnaround {row['id']}")
        _close(row["return_time"] + s.charge_time(row["return_soc"], s.relay_charge),
               row["component_ready"], f"Q3 component recharge {row['id']}")
    sample_count = 0
    worst_margin = float("inf")
    for row in q3["transport_sorties"]:
        for phase in row["route"]["phases"]:
            start = row["start"] + phase["t0"]
            end = row["start"] + phase["t1"]
            n = max(1, ceil((end - start) / sample_seconds))
            for i in range(n + 1):
                time = start + (end - start) * i / n
                position = phase_position(phase, phase["t0"] + (phase["t1"] - phase["t0"]) * i / n)
                direct = s.link_margin(position, gateway, "transport", "gateway")
                options = [s.link_margin(position, (r["lon"], r["lat"], r["altitude"]),
                                         "transport", "relay_access")
                           for r in relays if r["established"] - 1e-7 <= time <= r["service_end"] + 1e-7]
                best_margin = max([direct, *options])
                worst_margin = min(worst_margin, best_margin)
                if best_margin < -1e-8:
                    raise AssertionError(f"Q3 communication outage: {row['id']} at {time:.2f} s")
                sample_count += 1
    if set(c["mode"] for c in q3["communication"]) - {"direct", "relay"}:
        raise AssertionError("Q3 communication table includes outage")
    return dict(**transport, relay_sorties=len(relays), communication_samples=sample_count,
                minimum_sampled_best_link_margin_db=worst_margin,
                numerical_sample_seconds=sample_seconds)


def verify_q4(s: Scenario, q3: dict, q4: dict) -> dict:
    components, relay_zones = coupled_components(s, q3)
    if components != q4["atomic_components"]:
        raise AssertionError("Q4 components differ from Q3 communication relation")
    for k, plan in q4["partitions"].items():
        if not plan["feasible"]:
            continue
        groups = [set(g["zones"]) for g in plan["groups"]]
        if len(groups) != int(k) or set.union(*groups) != set(s.zone_boxes):
            raise AssertionError(f"Q4 K={k} does not partition service zones")
        if sum(len(x) for x in groups) != len(s.zone_boxes):
            raise AssertionError(f"Q4 K={k} has duplicate service zones")
        if any(not any(set(component) <= group for group in groups) for component in components):
            raise AssertionError(f"Q4 K={k} splits a coupled component")
        for group, detail in zip(groups, plan["groups"]):
            replay = resource_need(s, q3, group, relay_zones)
            if replay["strict_ids"] != detail["strict_ids"] or replay["minimum_relabelled"] != detail["minimum_relabelled"]:
                raise AssertionError(f"Q4 K={k} resource tally mismatch")
    return dict(atomic_components=len(components), partitions_checked={k: p.get("partitions_checked", 0)
                                                                         for k, p in q4["partitions"].items()})


def verify_all(s: Scenario, q1: dict, q2: dict, q3: dict, q4: dict) -> dict:
    return dict(q1=verify_q1(s, q1), q2=verify_q2(s, q2),
                q3=verify_q3(s, q3), q4=verify_q4(s, q3, q4))
