"""Exact Q4 partitions and resource chains for a fixed, certified Q3 plan.

The only decisions here are the service-zone partition and interchangeable
physical resource labels. Q3 routes, times and communication are immutable.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from hashlib import sha256
from itertools import combinations
from math import sqrt
from pathlib import Path
import json

from q4 import RESOURCE_KEYS, _canonical_partitions


EPS = 1e-8


def _unique(rows: list[dict], label: str) -> dict[str, dict]:
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)) or any(not x for x in ids):
        raise ValueError(f"Duplicate or empty {label} ID")
    return {row["id"]: row for row in rows}


def fixed_input(s, plan: dict) -> dict:
    """Validate references and build actual task coupling, with edge evidence."""
    if plan.get("certificate", {}).get("status") != "PASS":
        raise ValueError("Q3 continuous communication certificate is not PASS")
    transports = _unique(plan["transport_sorties"], "transport sortie")
    relays = _unique(plan["relay_sorties"], "relay sortie")
    zones = set(s.zone_boxes)
    transport_zones = {}
    boxes = []
    for task in transports.values():
        visited = [v[0] for v in task["visits"]]
        if not visited or set(visited) - zones or len(visited) != len(set(visited)):
            raise ValueError(f"Invalid service-zone visits in {task['id']}")
        if visited != [v[0] for v in task["route"]["visits"]]:
            raise ValueError(f"Route visits disagree in {task['id']}")
        route_boxes = task["route"]["box_ids"]
        visit_boxes = [b for _, batch in task["visits"] for b in batch]
        if len(route_boxes) != len(set(route_boxes)) or set(route_boxes) != set(visit_boxes):
            raise ValueError(f"Route box conservation failed in {task['id']}")
        if any(s.boxes[b].zone != z for z, batch in task["visits"] for b in batch):
            raise ValueError(f"Box zone mismatch in {task['id']}")
        boxes += route_boxes
        transport_zones[task["id"]] = sorted(visited)
    if Counter(boxes) != Counter(s.boxes.keys()):
        raise ValueError("Q3 does not deliver each input box exactly once")

    intervals = plan.get("communication_intervals")
    if not isinstance(intervals, list) or not intervals:
        raise ValueError("Missing actual Q3 communication intervals")
    relay_services = defaultdict(list)
    comm_by_task = defaultdict(list)
    for index, row in enumerate(intervals):
        tid, rid = row["sortie"], row.get("relay_sortie")
        if tid not in transports or row["end"] <= row["start"] + EPS:
            raise ValueError(f"Invalid communication interval {index}")
        if row.get("status") != "PASS" or row.get("mode") not in {"direct", "relay"}:
            raise ValueError(f"Uncertified communication interval {index}")
        if row["mode"] == "relay":
            if rid not in relays:
                raise ValueError(f"Unknown relay in communication interval {index}")
            relay = relays[rid]
            if row["start"] < relay["established"] - EPS or row["end"] > relay["service_end"] + EPS:
                raise ValueError(f"Relay service time mismatch at interval {index}")
            relay_services[rid].append(dict(transport=tid, start=row["start"],
                                            end=row["end"], interval_index=index))
        elif rid:
            raise ValueError(f"Direct communication names a relay at interval {index}")
        comm_by_task[tid].append(row)
    if set(comm_by_task) != set(transports):
        raise ValueError("Some transport tasks have no communication intervals")
    if set(relay_services) != set(relays):
        raise ValueError("A fixed relay task has no actual service relation")
    # Each flight phase must be certified without a gap. Preparation is not a
    # flight phase and is therefore excluded from this continuity check.
    for tid, task in transports.items():
        by_phase = defaultdict(list)
        for row in comm_by_task[tid]:
            by_phase[row["phase_index"]].append(row)
        if set(by_phase) != set(range(len(task["route"]["phases"]))):
            raise ValueError(f"Communication phase indices disagree in {tid}")
        for i, phase in enumerate(task["route"]["phases"]):
            ordered = sorted(by_phase[i], key=lambda r: r["start"])
            if not ordered:
                raise ValueError(f"Uncovered phase {i} of {tid}")
            cursor = task["start"] + phase["t0"]
            for row in ordered:
                if abs(row["start"] - cursor) > 1e-5:
                    raise ValueError(f"Communication gap/overlap in {tid} phase {i}")
                cursor = row["end"]
            if abs(cursor - (task["start"] + phase["t1"])) > 1e-5:
                raise ValueError(f"Communication phase endpoint mismatch in {tid}")

    parent = {z: z for z in zones}

    def find(z):
        while parent[z] != z:
            parent[z] = parent[parent[z]]
            z = parent[z]
        return z

    def union(edge):
        for z in edge[1:]:
            parent[find(z)] = find(edge[0])

    evidence = []
    for tid, edge in transport_zones.items():
        union(edge)
        evidence.append(dict(kind="transport", task=tid, zones=edge))
    relay_zones = {}
    for rid, services in sorted(relay_services.items()):
        bound = sorted({z for service in services for z in transport_zones[service["transport"]]})
        union(bound)
        relay_zones[rid] = bound
        evidence.append(dict(kind="relay", task=rid, zones=bound,
                             actual_transport_tasks=sorted({x["transport"] for x in services}),
                             communication_interval_indices=[x["interval_index"] for x in services]))
    components = defaultdict(list)
    for z in sorted(zones):
        components[find(z)].append(z)
    components = sorted(components.values(), key=lambda xs: xs[0])
    # Independent adjacency traversal guards the union-find construction.
    adjacency = {z: set() for z in zones}
    for item in evidence:
        for a, b in combinations(item["zones"], 2):
            adjacency[a].add(b)
            adjacency[b].add(a)
    seen, bfs_components = set(), []
    for root in sorted(zones):
        if root in seen:
            continue
        queue, group = [root], []
        seen.add(root)
        for node in queue:
            group.append(node)
            for neighbor in sorted(adjacency[node] - seen):
                seen.add(neighbor)
                queue.append(neighbor)
        bfs_components.append(sorted(group))
    if components != bfs_components:
        raise AssertionError("Union-find and graph traversal disagree")
    return dict(components=components, evidence=evidence, transport_zones=transport_zones,
                relay_zones=relay_zones, relay_services=dict(relay_services),
                transport=transports, relay=relays, boxes=len(boxes),
                communication_intervals=len(intervals))


def _intervals(key: str, transports: list[dict], relays: list[dict]) -> list[dict]:
    if key.endswith("_aircraft") and key[0] in "ABC":
        rows = [r for r in transports if r["model"] == key[0]]
        end_key, old_key = "return_time", "drone"
    elif key.endswith("_batteries"):
        rows = [r for r in transports if r["model"] == key[0]]
        end_key, old_key = "battery_ready", "battery"
    elif key == "relay_aircraft":
        rows, end_key, old_key = relays, "relay_ready", "relay"
    else:
        rows, end_key, old_key = relays, "component_ready", "energy_component"
    result = [dict(task=r["id"], start=r["start"], end=r[end_key],
                   old_resource=r[old_key]) for r in rows]
    if any(r["end"] <= r["start"] + EPS for r in result):
        raise ValueError(f"Non-positive occupation interval in {key}")
    return sorted(result, key=lambda r: (r["start"], r["task"]))


def _peak(rows: list[dict]) -> dict:
    events = sorted([(r["start"], 1, r["task"]) for r in rows] +
                    [(r["end"], 0, r["task"]) for r in rows])
    active, maximum, witness = set(), 0, dict(time=None, tasks=[])
    for t, kind, task in events:
        if kind == 0:
            active.remove(task)
        else:
            active.add(task)
            if len(active) > maximum:
                maximum = len(active)
                witness = dict(time=t, tasks=sorted(active))
    return dict(count=maximum, **witness)


def _matching(rows: list[dict], threshold: float = 0.0) -> dict[str, str]:
    """Independent augmenting-path maximum matching of task predecessor/successor."""
    successors = {}
    left = sorted(rows, key=lambda r: (r["end"], r["task"]))
    right = sorted(rows, key=lambda r: (r["start"], r["task"]))

    def augment(src, seen):
        for dst in right:
            if dst["task"] == src["task"] or dst["start"] < src["end"] or dst["start"] - src["end"] < threshold:
                continue
            if dst["task"] in seen:
                continue
            seen.add(dst["task"])
            if dst["task"] not in successors or augment(successors[dst["task"]], seen):
                successors[dst["task"]] = src
                return True
        return False

    for src in left:
        augment(src, set())
    return {src["task"]: dst for dst, src in successors.items()}


def _old_min_gap(rows: list[dict]) -> float | None:
    by_resource = defaultdict(list)
    for row in rows:
        by_resource[row["old_resource"]].append(row)
    gaps = []
    for tasks in by_resource.values():
        tasks.sort(key=lambda r: (r["start"], r["task"]))
        gaps.extend(b["start"] - a["end"] for a, b in zip(tasks, tasks[1:]))
    return min(gaps) if gaps else None


def resource_certificate(rows: list[dict], group: str, key: str) -> tuple[dict, list[dict]]:
    peak = _peak(rows)
    need = peak["count"]
    full = _matching(rows)
    if len(rows) - len(full) != need:
        raise AssertionError(f"Peak/matching minimum mismatch: {group} {key}")
    reused = len(rows) - need
    if reused:
        gaps = sorted({b["start"] - a["end"] for a in rows for b in rows
                       if a["task"] != b["task"] and b["start"] >= a["end"]})
        lo, hi = 0, len(gaps) - 1
        best = full
        best_index = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = _matching(rows, gaps[mid])
            if len(candidate) >= reused:
                best, best_index, lo = candidate, mid, mid + 1
            else:
                hi = mid - 1
        if best_index < 0 or len(best) != reused:
            raise AssertionError("Bottleneck matching search failed")
        delta = gaps[best_index]
        next_threshold = gaps[best_index + 1] if best_index + 1 < len(gaps) else None
        next_size = _matching(rows, next_threshold).__len__() if next_threshold is not None else None
        if next_size is not None and next_size >= reused:
            raise AssertionError("Next threshold is unexpectedly feasible")
    else:
        best, delta, next_threshold, next_size = {}, None, None, None
    item_by_id = {r["task"]: r for r in rows}
    predecessors = {dst: src for src, dst in best.items()}
    chains = []
    visited = set()
    for first in rows:
        if first["task"] in predecessors:
            continue
        chain, current = [], first["task"]
        while current is not None:
            if current in visited:
                raise AssertionError("Cyclic resource chain")
            visited.add(current)
            chain.append(current)
            current = best.get(current)
        chains.append(chain)
    if len(visited) != len(rows) or len(chains) != need:
        raise AssertionError("Resource chains do not cover tasks exactly once")
    assignments = []
    for number, chain in enumerate(chains, 1):
        rid = f"{group}-{key}-{number:02d}"
        for i, task in enumerate(chain):
            row = item_by_id[task]
            following = chain[i + 1] if i + 1 < len(chain) else None
            assignments.append(dict(partition="", group=group, resource_type=key,
                                    resource_id=rid, task=task, start=row["start"],
                                    release=row["end"], next_task=following or "",
                                    handoff_slack_seconds=(item_by_id[following]["start"] - row["end"]
                                                           if following else None)))
    certificate = dict(resource_type=key, task_count=len(rows), minimum_resources=need,
                       peak_witness=peak, maximum_matching_size=len(full),
                       matching_edges=[dict(predecessor=src, successor=dst,
                                            slack_seconds=item_by_id[dst]["start"] - item_by_id[src]["end"])
                                       for src, dst in sorted(best.items())],
                       chains=chains, optimal_min_handoff_slack_seconds=delta,
                       original_id_min_handoff_slack_seconds=_old_min_gap(rows),
                       next_threshold_seconds=next_threshold,
                       matching_size_at_next_threshold=next_size)
    return certificate, assignments


def _stock(s) -> dict:
    return dict(A_aircraft=sum(v == "A" for v in s.aircraft.values()),
                B_aircraft=sum(v == "B" for v in s.aircraft.values()),
                C_aircraft=sum(v == "C" for v in s.aircraft.values()),
                A_batteries=s.battery_count["A"], B_batteries=s.battery_count["B"],
                C_batteries=s.battery_count["C"], relay_aircraft=len(s.relays),
                relay_components=s.relay_energy_count)


def _partition_id(groups: list[list[int]]) -> str:
    value = {tuple(g) for g in groups}
    return {
        frozenset({(0,), (1, 2)}): "P1",
        frozenset({(1,), (0, 2)}): "P2",
        frozenset({(2,), (0, 1)}): "P3",
        frozenset({(0,), (1,), (2,)}): "P4",
    }.get(frozenset(value), "P" + "_".join("".join(str(i + 1) for i in g) for g in groups))


def solve(s, plan: dict, input_bytes: bytes) -> dict:
    fixed = fixed_input(s, plan)
    stock = _stock(s)
    components = fixed["components"]
    all_partitions = []
    all_assignments = []
    for k in (2, 3):
        for labels in _canonical_partitions(len(components), k):
            blocks = [[i for i, label in enumerate(labels) if label == g] for g in range(k)]
            pid = _partition_id(blocks)
            groups, certificates = [], {}
            for gi, block in enumerate(blocks, 1):
                gid = f"G{gi}"
                zones = sorted(z for i in block for z in components[i])
                zone_set = set(zones)
                transports = [t for t in fixed["transport"].values()
                              if set(fixed["transport_zones"][t["id"]]) <= zone_set]
                relays = [r for r in fixed["relay"].values()
                          if set(fixed["relay_zones"][r["id"]]) <= zone_set]
                if any(set(fixed["transport_zones"][t["id"]]) & zone_set and
                       not set(fixed["transport_zones"][t["id"]]) <= zone_set
                       for t in fixed["transport"].values()):
                    raise AssertionError("Transport task split across groups")
                work = sum(t["return_time"] - t["start"] for t in transports)
                work += sum(r["return_time"] - r["start"] for r in relays)
                resource_need = {}
                occupied_seconds = {}
                certificates[gid] = {}
                for key in RESOURCE_KEYS:
                    rows = _intervals(key, transports, relays)
                    cert, assignments = resource_certificate(rows, gid, key)
                    resource_need[key] = cert["minimum_resources"]
                    occupied_seconds[key] = sum(row["end"] - row["start"] for row in rows)
                    certificates[gid][key] = cert
                    for row in assignments:
                        row["partition"] = pid
                    all_assignments.extend(assignments)
                box_ids = {b for t in transports for b in t["route"]["box_ids"]}
                all_task_rows = transports + relays
                horizon = max(max(t["return_time"], t.get("battery_ready", t["return_time"]),
                                  t.get("relay_ready", t["return_time"]),
                                  t.get("component_ready", t["return_time"])) for t in all_task_rows)
                horizon -= min(t["start"] for t in all_task_rows)
                occupancy = {key: (occupied_seconds[key] / (resource_need[key] * horizon)
                                   if resource_need[key] else 0.0) for key in RESOURCE_KEYS}
                groups.append(dict(id=gid, component_indices=[i + 1 for i in block], zones=zones,
                                   transport_tasks=sorted(t["id"] for t in transports),
                                   relay_tasks=sorted(r["id"] for r in relays),
                                   boxes=len(box_ids), cargo_kg=sum(s.boxes[b].kg for b in box_ids),
                                   transport_sorties=len(transports), relay_sorties=len(relays),
                                   work_seconds=work,
                                   energy_kwh=sum(t["energy_kwh"] for t in transports + relays),
                                   resource_need=resource_need, horizon_seconds=horizon,
                                   resource_occupancy_ratio=occupancy))
            needs = {key: sum(g["resource_need"][key] for g in groups) for key in RESOURCE_KEYS}
            shortage = {key: max(0, needs[key] - stock[key]) for key in RESOURCE_KEYS}
            work = [g["work_seconds"] for g in groups]
            mean = sum(work) / k
            cv = sqrt(sum((x - mean) ** 2 for x in work) / k) / mean
            all_partitions.append(dict(id=pid, group_count=k, groups=groups,
                                       resource_need=needs, resource_total=sum(needs.values()),
                                       shortage=shortage, shortage_total=sum(shortage.values()),
                                       workload_cv=cv, structural_legal=True,
                                       stock_sufficient=not any(shortage.values()),
                                       executable_after_procurement=True,
                                       certificates=certificates))
    all_partitions.sort(key=lambda p: p["id"])
    if len(components) == 3 and [p["id"] for p in all_partitions] != ["P1", "P2", "P3", "P4"]:
        raise AssertionError("Expected exactly four fixed-Q3 partitions")
    pooled = {}
    for key in RESOURCE_KEYS:
        rows = _intervals(key, list(fixed["transport"].values()), list(fixed["relay"].values()))
        pooled[key] = _peak(rows)["count"]
    for partition in all_partitions:
        partition["sharing_loss"] = {key: partition["resource_need"][key] - pooled[key]
                                     for key in RESOURCE_KEYS}
    two = [p for p in all_partitions if p["group_count"] == 2]
    recommended = min(two, key=lambda p: (p["resource_total"], p["workload_cv"]))
    thresholds = sorted({p["workload_cv"] for p in two})
    staircase = [dict(maximum_cv=limit,
                      least_resource_partition=min((p for p in two if p["workload_cv"] <= limit + 1e-12),
                                                   key=lambda p: (p["resource_total"], p["id"]))["id"])
                 for limit in thresholds]
    total_work = sum(t["return_time"] - t["start"] for t in fixed["transport"].values())
    total_work += sum(r["return_time"] - r["start"] for r in fixed["relay"].values())
    component_work = []
    component_boxes = []
    for component in components:
        zset = set(component)
        component_boxes.append(sum(len(t["route"]["box_ids"])
                                   for t in fixed["transport"].values()
                                   if set(fixed["transport_zones"][t["id"]]) <= zset))
        component_work.append(sum(t["return_time"] - t["start"] for t in fixed["transport"].values()
                                  if set(fixed["transport_zones"][t["id"]]) <= zset) +
                              sum(r["return_time"] - r["start"] for r in fixed["relay"].values()
                                  if set(fixed["relay_zones"][r["id"]]) <= zset))
    alpha = max(component_work) / total_work
    bounds = {str(k): (k * alpha - 1) / sqrt(k - 1) for k in (2, 3)}
    components_doc = dict(input_sha256=sha256(input_bytes).hexdigest().upper(),
                          fixed_q3_commit_reference="799197c", components=components,
                          component_work_seconds=component_work,
                          component_box_counts=component_boxes,
                          coupling_evidence=fixed["evidence"],
                          relay_actual_service=fixed["relay_services"],
                          transport_task_count=len(fixed["transport"]),
                          relay_task_count=len(fixed["relay"]),
                          communication_interval_count=fixed["communication_intervals"],
                          box_count=fixed["boxes"])
    return dict(components=components_doc, stock=stock, pooled_resource_need=pooled,
                partitions=all_partitions, assignments=all_assignments,
                recommended_partition=recommended["id"],
                balance_threshold_policy=staircase,
                total_work_seconds=total_work, dominant_component_fraction=alpha,
                workload_cv_lower_bound=bounds)


def load_and_solve(s, path: Path) -> dict:
    payload = path.read_bytes()
    return solve(s, json.loads(payload.decode("utf-8")), payload)
