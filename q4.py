"""Question 4: exact partition search over task-coupled service components."""

from __future__ import annotations

from math import sqrt

from model import Scenario, distance_m


RESOURCE_KEYS = ("A_aircraft", "B_aircraft", "C_aircraft",
                 "A_batteries", "B_batteries", "C_batteries",
                 "relay_aircraft", "relay_components")


def _peak(intervals: list[tuple[float, float]]) -> int:
    events = []
    for start, end in intervals:
        if end > start + 1e-8:
            events.extend(((start, 1), (end, -1)))
    count = maximum = 0
    for _, delta in sorted(events, key=lambda e: (e[0], e[1])):
        count += delta
        maximum = max(maximum, count)
    return maximum


def coupled_components(s: Scenario, q3: dict) -> tuple[list[list[str]], dict[str, list[str]]]:
    parent = {z: z for z in s.zone_boxes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        parent[find(b)] = find(a)

    transport_zones = {}
    for row in q3["transport_sorties"]:
        zones = [z for z, _ in row["visits"]]
        transport_zones[row["id"]] = zones
        for z in zones[1:]:
            union(zones[0], z)
    relay_zones: dict[str, set[str]] = {r["id"]: set() for r in q3["relay_sorties"]}
    for c in q3["communication"]:
        if c["mode"] == "relay" and c["relay_sortie"] and c["end"] > c["start"] + 1e-8:
            relay_zones[c["relay_sortie"]].update(transport_zones[c["sortie"]])
    for zones in relay_zones.values():
        group = sorted(zones)
        for z in group[1:]:
            union(group[0], z)
    components: dict[str, list[str]] = {}
    for z in sorted(s.zone_boxes):
        components.setdefault(find(z), []).append(z)
    return sorted(components.values(), key=lambda zs: zs[0]), {k: sorted(v) for k, v in relay_zones.items()}


def resource_need(s: Scenario, q3: dict, zones: set[str], relay_zones: dict[str, list[str]]) -> dict:
    trips = [r for r in q3["transport_sorties"] if set(z for z, _ in r["visits"]) <= zones]
    relays = [r for r in q3["relay_sorties"] if relay_zones[r["id"]] and set(relay_zones[r["id"]]) <= zones]
    strict = {}
    minimum = {}
    for model in s.transport:
        matching = [r for r in trips if r["model"] == model]
        strict[f"{model}_aircraft"] = len({r["drone"] for r in matching})
        strict[f"{model}_batteries"] = len({r["battery"] for r in matching})
        minimum[f"{model}_aircraft"] = _peak([(r["start"], r["return_time"]) for r in matching])
        minimum[f"{model}_batteries"] = _peak([(r["start"], r["battery_ready"]) for r in matching])
    strict["relay_aircraft"] = len({r["relay"] for r in relays})
    strict["relay_components"] = len({r["energy_component"] for r in relays})
    minimum["relay_aircraft"] = _peak([(r["start"], r["relay_ready"]) for r in relays])
    minimum["relay_components"] = _peak([(r["start"], r["component_ready"]) for r in relays])
    work = (sum(r["route"]["duration"] for r in trips) +
            sum(r["return_time"] - r["start"] for r in relays))
    delivered = {b for r in trips for b in r["route"]["box_ids"]}
    return dict(strict_ids=strict, minimum_relabelled=minimum,
                transport_sorties=[r["id"] for r in trips], relay_sorties=[r["id"] for r in relays],
                boxes=len(delivered), cargo_kg=sum(s.boxes[b].kg for b in delivered),
                work_seconds=work, energy_kwh=sum(r["energy_kwh"] for r in trips + relays))


def _canonical_partitions(count: int, k: int):
    labels = [0] * count
    def visit(i: int, used: int):
        if i == count:
            if used == k:
                yield tuple(labels)
            return
        for label in range(min(used + 1, k)):
            labels[i] = label
            yield from visit(i + 1, max(used, label + 1))
    yield from visit(1, 1)


def _dispersion(s: Scenario, zones: set[str]) -> float:
    if not zones:
        return 0.0
    lon = sum(s.nodes[z].lon for z in zones) / len(zones)
    lat = sum(s.nodes[z].lat for z in zones) / len(zones)
    return sum(distance_m((s.nodes[z].lon, s.nodes[z].lat), (lon, lat)) for z in zones) / 1000


def solve(s: Scenario, q3: dict) -> dict:
    components, relay_zones = coupled_components(s, q3)
    stock = dict(A_aircraft=sum(g == "A" for g in s.aircraft.values()),
                 B_aircraft=sum(g == "B" for g in s.aircraft.values()),
                 C_aircraft=sum(g == "C" for g in s.aircraft.values()),
                 A_batteries=s.battery_count["A"], B_batteries=s.battery_count["B"],
                 C_batteries=s.battery_count["C"], relay_aircraft=len(s.relays),
                 relay_components=s.relay_energy_count)
    all_zones = set(s.zone_boxes)
    pooled = resource_need(s, q3, all_zones, relay_zones)
    answers = {}
    for k in (2, 3):
        if len(components) < k:
            answers[str(k)] = dict(feasible=False, reason="Fewer atomic components than groups")
            continue
        options = []
        checked = 0
        for labels in _canonical_partitions(len(components), k):
            zone_groups = [set() for _ in range(k)]
            for component, label in zip(components, labels):
                zone_groups[label].update(component)
            if any(not group for group in zone_groups):
                continue
            checked += 1
            details = [resource_need(s, q3, zones, relay_zones) for zones in zone_groups]
            strict_sum = {key: sum(x["strict_ids"][key] for x in details) for key in RESOURCE_KEYS}
            min_sum = {key: sum(x["minimum_relabelled"][key] for x in details) for key in RESOURCE_KEYS}
            shortage = {key: max(0, strict_sum[key] - stock[key]) for key in RESOURCE_KEYS}
            relabel_shortage = {key: max(0, min_sum[key] - stock[key]) for key in RESOURCE_KEYS}
            overhead = {key: strict_sum[key] - pooled["strict_ids"][key] for key in RESOURCE_KEYS}
            work = [x["work_seconds"] for x in details]
            mean_work = sum(work) / k
            cv = sqrt(sum((x - mean_work) ** 2 for x in work) / k) / mean_work if mean_work else 0.0
            spread = sum(_dispersion(s, zones) for zones in zone_groups)
            score = (sum(shortage.values()), sum(overhead.values()), cv, spread)
            options.append((score, dict(feasible=True,
                                    groups=[dict(id=f"G{i + 1}", zones=sorted(zones), **details[i])
                                            for i, zones in enumerate(zone_groups)],
                                    resource_total_strict=strict_sum,
                                    resource_total_minimum_relabelled=min_sum,
                                    stock=stock, shortage_strict=shortage,
                                    shortage_minimum_relabelled=relabel_shortage,
                                    partition_overhead=overhead, workload_cv=cv,
                                    spatial_dispersion_km=spread)))
        assert options
        best = min(options, key=lambda item: item[0])[1]
        least_short = min(item[0][0] for item in options)
        balanced = min(options, key=lambda item: (item[0][2], item[0][0], item[0][1]))[1]
        near = min((item for item in options if item[0][0] <= least_short + 2),
                   key=lambda item: (item[0][2], item[0][0], item[0][1]))[1]
        best["partitions_checked"] = checked
        def alternative(plan: dict) -> dict:
            return dict(groups=plan["groups"], resource_total_strict=plan["resource_total_strict"],
                        resource_total_minimum_relabelled=plan["resource_total_minimum_relabelled"],
                        total_shortage=sum(plan["shortage_strict"].values()),
                        shortage=plan["shortage_strict"], workload_cv=plan["workload_cv"])
        best["alternatives"] = {
            "best_balance": alternative(balanced),
            "best_balance_within_two_extra_resources": alternative(near),
        }
        answers[str(k)] = best
    return dict(atomic_components=components, relay_zone_sets=relay_zones,
                pooled_need=pooled, partitions=answers)
