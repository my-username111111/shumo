"""Independent task-by-task replay of the Q4 partition and resource exports."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections import deque
from hashlib import sha256
import json
from math import isclose, sqrt
from itertools import product

from q4 import RESOURCE_KEYS
from q4_exact import EPS, fixed_input


def verify_fixed_q3(s, plan):
    """Verify the saved communication relation without selecting new relays."""
    from q3_certificate import physical_audit, margin
    from model import phase_position
    physical = physical_audit(s, plan)
    fixed = fixed_input(s, plan)
    home = s.nodes['O01']
    gateway = (home.lon, home.lat, home.ground+s.gateway_agl)
    extra = plan.get('extra_loss_db',0.)
    from math import isfinite
    if not isfinite(extra) or extra < 0 or plan['certificate'].get('extra_loss_db', 0.) != extra:
        raise AssertionError('Q3 plan and certificate communication scenarios disagree')
    for row in plan['communication_intervals']:
        transport = fixed['transport'][row['sortie']]
        phase = transport['route']['phases'][row['phase_index']]
        p0 = phase_position(phase,row['start']-transport['start'])
        p1 = phase_position(phase,row['end']-transport['start'])
        if row['mode']=='direct':
            lower,_ = margin(s,gateway,p0,p1,'transport','gateway',extra)
        else:
            relay = fixed['relay'][row['relay_sortie']]
            if 'service_zones' in relay and not set(fixed['transport_zones'][transport['id']])<=set(relay['service_zones']):
                raise AssertionError('Saved communication violates relay group policy')
            point = (relay['lon'],relay['lat'],relay['altitude'])
            access,_ = margin(s,point,p0,p1,'relay_access','transport',extra)
            back,_ = margin(s,gateway,point,point,'relay_backhaul','gateway',extra)
            lower = min(access,back)
        if lower < 0 or row['margin_lower_db'] > lower+1e-7:
            raise AssertionError('Saved communication interval cannot be independently certified')
    return dict(status='PASS',physical=physical,communication_intervals=len(plan['communication_intervals']),
                communication_relation_preserved=True)


def flow_matching(tasks, threshold):
    """Independent unit-capacity Edmonds-Karp flow (solver uses DFS matching)."""
    capacity = defaultdict(dict)
    def edge(a, b):
        capacity[a][b] = 1
        capacity[b].setdefault(a, 0)
    for tid in tasks:
        edge('source', ('L', tid))
        edge(('R', tid), 'sink')
    for a, (_, end) in tasks.items():
        for b, (start, _) in tasks.items():
            if a != b and start >= end and start-end >= threshold:
                edge(('L', a), ('R', b))
    value = 0
    while True:
        previous = {'source': None}
        queue = deque(['source'])
        while queue and 'sink' not in previous:
            node = queue.popleft()
            for dst, cap in capacity[node].items():
                if cap and dst not in previous:
                    previous[dst] = node
                    queue.append(dst)
        if 'sink' not in previous:
            break
        node = 'sink'
        while previous[node] is not None:
            src = previous[node]
            capacity[src][node] -= 1
            capacity[node][src] += 1
            node = src
        value += 1
    return value


def verify_certificate(tasks, cert, assignments):
    """Recompute the bottleneck and its next-threshold impossibility proof."""
    needed = cert['minimum_resources']
    required = len(tasks)-needed
    full = flow_matching(tasks, 0.)
    if full != required or full != cert['maximum_matching_size']:
        raise AssertionError('Independent maximum matching size differs')
    thresholds = sorted({start-end for a, (_, end) in tasks.items()
                         for b, (start, _) in tasks.items() if a != b and start >= end})
    optimum = None
    next_threshold = None
    next_size = None
    if required:
        for i in range(len(thresholds)-1, -1, -1):
            if flow_matching(tasks, thresholds[i]) == required:
                optimum = thresholds[i]
                if i+1 < len(thresholds):
                    next_threshold = thresholds[i+1]
                    next_size = flow_matching(tasks, next_threshold)
                break
        if optimum is None:
            raise AssertionError('Independent bottleneck search failed')
    for name, expected in [('optimal_min_handoff_slack_seconds', optimum),
                           ('next_threshold_seconds', next_threshold),
                           ('matching_size_at_next_threshold', next_size)]:
        saved = cert[name]
        if (saved is None) != (expected is None) or (expected is not None and
                not isclose(saved, expected, rel_tol=0., abs_tol=EPS)):
            raise AssertionError('Independent optimality certificate mismatch: '+name)
    edges = {(r['task'], r['next_task']): r['handoff_slack_seconds']
             for r in assignments if r['next_task']}
    saved_edges = {(e['predecessor'], e['successor']): e['slack_seconds'] for e in cert['matching_edges']}
    if len(saved_edges) != len(cert['matching_edges']) or edges != saved_edges or len(edges) != required:
        raise AssertionError('Certificate matching edges differ from actual chains')
    if edges and not isclose(min(edges.values()), optimum, rel_tol=0., abs_tol=EPS):
        raise AssertionError('Assigned chains do not attain optimal bottleneck')
    chains = defaultdict(list)
    for row in assignments:
        chains[row['resource_id']].append(row)
    actual = sorted(tuple(r['task'] for r in sorted(rows, key=lambda x:x['start'])) for rows in chains.values())
    if actual != sorted(tuple(chain) for chain in cert['chains']):
        raise AssertionError('Certificate chains differ from assignment table')
    witness = cert['peak_witness']
    active = sorted(t for t,(a,b) in tasks.items() if a <= witness['time'] < b) if tasks else []
    if active != witness['tasks'] or len(active) != needed or cert['task_count'] != len(tasks):
        raise AssertionError('Invalid peak witness or task count')


def audit(s, plan: dict, result: dict) -> dict:
    fingerprint = sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    if result['components'].get('canonical_plan_sha256') != fingerprint:
        raise AssertionError('Q4 result is not bound to the supplied Q3 plan')
    fixed = fixed_input(s, plan)
    if fixed["components"] != result["components"]["components"]:
        raise AssertionError("Component graph differs from saved result")
    # Enumerate labelled assignments independently, then remove label symmetry.
    blocks = fixed['components']
    legal = set()
    for count in (2,3):
        for labels in product(range(count), repeat=len(blocks)):
            if len(set(labels)) != count: continue
            legal.add(tuple(sorted(tuple(sorted(z for block,label in zip(blocks,labels)
                                                if label==g for z in block)) for g in range(count))))
    saved = [tuple(sorted(tuple(sorted(g['zones'])) for g in p['groups'])) for p in result['partitions']]
    if len(saved)!=len(set(saved)) or set(saved)!=legal:
        raise AssertionError('Legal partitions missing, duplicated or added')
    for count in (2,3):
        choices=[p for p in result['partitions'] if p['group_count']==count]
        efficient=[]
        for candidate in choices:
            dominated=False
            for other in choices:
                differences=[other['resource_need'][k]-candidate['resource_need'][k] for k in RESOURCE_KEYS]
                differences.append(other['workload_cv']-candidate['workload_cv'])
                if max(differences)<=1e-10 and min(differences)<-1e-10:dominated=True;break
            if not dominated:efficient.append(candidate['id'])
        if set(result.get('pareto_partitions',{}).get(str(count),[]))!=set(efficient):
            raise AssertionError('Resource-balance Pareto set differs')
    fields = ('partition','group','resource_type','resource_id','physical_id','inventory_status')
    mapped = [tuple(row[k] for k in fields) for row in result['inventory_mapping']]
    assigned = {tuple(row[k] for k in fields) for row in result['assignments']}
    if len(mapped)!=len({row[:4] for row in mapped}) or set(mapped)!=assigned:
        raise AssertionError('Inventory mapping table differs from assignments')
    all_zones = set(s.zone_boxes)
    all_transport = set(fixed["transport"])
    all_relay = set(fixed["relay"])
    inventory = {f'{g}_aircraft': {u for u, typ in s.aircraft.items() if typ == g} for g in s.transport}
    inventory.update({f'{g}_batteries': {f'{g}{i:02d}' for i in range(1,n+1)}
                      for g,n in s.battery_count.items()})
    inventory['relay_aircraft'] = set(s.relays)
    inventory['relay_components'] = {f'RE{i:02d}' for i in range(1,s.relay_energy_count+1)}
    if result['stock'] != {key:len(ids) for key,ids in inventory.items()}:
        raise AssertionError('Reported inventory differs from source data')
    assignment_count = 0
    partition_checks = {}
    assignments_by_partition = defaultdict(list)
    for row in result["assignments"]:
        assignments_by_partition[row["partition"]].append(row)
    if set(assignments_by_partition) != {p["id"] for p in result["partitions"]}:
        raise AssertionError("Missing or extra partition assignments")
    for partition in result["partitions"]:
        pid = partition["id"]
        groups = partition["groups"]
        group_by_id = {g["id"]: g for g in groups}
        zone_sets = [set(g["zones"]) for g in groups]
        if len(groups) != partition["group_count"] or any(not z for z in zone_sets):
            raise AssertionError(f"Empty or wrong group count in {pid}")
        if set.union(*zone_sets) != all_zones or sum(map(len, zone_sets)) != len(all_zones):
            raise AssertionError(f"Service-zone partition invalid in {pid}")
        if any(not any(set(c) <= z for z in zone_sets) for c in fixed["components"]):
            raise AssertionError(f"A task block was split in {pid}")
        if Counter(t for g in groups for t in g["transport_tasks"]) != Counter(all_transport):
            raise AssertionError(f"Transport tasks not conserved in {pid}")
        if Counter(t for g in groups for t in g["relay_tasks"]) != Counter(all_relay):
            raise AssertionError(f"Relay tasks not conserved in {pid}")
        expected = {}
        for group in groups:
            gid = group["id"]
            zset = set(group["zones"])
            for tid in group["transport_tasks"]:
                if not set(fixed["transport_zones"][tid]) <= zset:
                    raise AssertionError(f"Transport/zone mismatch in {pid} {gid}")
            for rid in group["relay_tasks"]:
                if not set(fixed["relay_zones"][rid]) <= zset:
                    raise AssertionError(f"Relay/zone mismatch in {pid} {gid}")
            group_transport = [fixed["transport"][tid] for tid in group["transport_tasks"]]
            group_relay = [fixed["relay"][rid] for rid in group["relay_tasks"]]
            box_ids = [b for task in group_transport for b in task["route"]["box_ids"]]
            if len(box_ids) != group["boxes"] or not isclose(
                    sum(s.boxes[b].kg for b in box_ids), group["cargo_kg"], abs_tol=EPS):
                raise AssertionError(f"Group cargo mismatch in {pid} {gid}")
            if len(group_transport) != group["transport_sorties"] or len(group_relay) != group["relay_sorties"]:
                raise AssertionError(f"Group sortie count mismatch in {pid} {gid}")
            if not isclose(sum(t["return_time"] - t["start"] for t in group_transport + group_relay),
                           group["work_seconds"], abs_tol=EPS):
                raise AssertionError(f"Group work mismatch in {pid} {gid}")
            if not isclose(sum(t["energy_kwh"] for t in group_transport + group_relay),
                           group["energy_kwh"], abs_tol=EPS):
                raise AssertionError(f"Group energy mismatch in {pid} {gid}")
            all_group_tasks = group_transport + group_relay
            last_release = max(max(t["return_time"], t.get("battery_ready", t["return_time"]),
                                   t.get("relay_ready", t["return_time"]),
                                   t.get("component_ready", t["return_time"]))
                               for t in all_group_tasks)
            horizon = last_release - min(t["start"] for t in all_group_tasks)
            if not isclose(horizon, group["horizon_seconds"], abs_tol=EPS):
                raise AssertionError(f"Group resource horizon mismatch in {pid} {gid}")
            for key in RESOURCE_KEYS:
                if key.startswith("relay_"):
                    tasks = [fixed["relay"][rid] for rid in group["relay_tasks"]]
                    end_field = "relay_ready" if key == "relay_aircraft" else "component_ready"
                else:
                    tasks = [fixed["transport"][tid] for tid in group["transport_tasks"]
                             if fixed["transport"][tid]["model"] == key[0]]
                    end_field = "return_time" if key.endswith("_aircraft") else "battery_ready"
                expected[gid, key] = {t["id"]: (t["start"], t[end_field]) for t in tasks}
        by_resource = defaultdict(list)
        physical_owner = {}
        for row in assignments_by_partition[pid]:
            gid, key, task = row["group"], row["resource_type"], row["task"]
            if (gid, key) not in expected or task not in expected[gid, key]:
                raise AssertionError(f"Assignment has wrong task type/group: {pid} {task}")
            start, release = expected[gid, key][task]
            if not isclose(row["start"], start, abs_tol=EPS) or not isclose(row["release"], release, abs_tol=EPS):
                raise AssertionError(f"Assignment changed a fixed task interval: {pid} {task}")
            if not row["resource_id"].startswith(f"{gid}-{key}-"):
                raise AssertionError(f"Resource ID shared across group or type: {pid} {task}")
            physical = row['physical_id']
            identity = (key, physical)
            if identity in physical_owner and physical_owner[identity] != row['resource_id']:
                raise AssertionError('Physical inventory item assigned to multiple logical resources')
            physical_owner[identity] = row['resource_id']
            if row['inventory_status'] == 'existing':
                if physical not in inventory[key]:
                    raise AssertionError('Unknown existing physical resource')
            elif row['inventory_status'] != 'procurement_required' or not physical.startswith(f'NEW-{key}-'):
                raise AssertionError('Invalid procurement resource label')
            by_resource[gid, key, row["resource_id"]].append(row)
        for (gid, key), tasks in expected.items():
            observed = [r["task"] for r in assignments_by_partition[pid]
                        if r["group"] == gid and r["resource_type"] == key]
            if Counter(observed) != Counter(tasks.keys()):
                raise AssertionError(f"Task coverage failed for {pid} {gid} {key}")
            ids = {rid for g, kind, rid in by_resource if g == gid and kind == key}
            needed = partition["certificates"][gid][key]["minimum_resources"]
            if len(ids) != needed or group_by_id[gid]['resource_need'][key] != needed or partition["resource_need"][key] < needed:
                raise AssertionError(f"Resource count differs in {pid} {gid} {key}")
            events = sorted([(a, 1) for a, _ in tasks.values()] +
                            [(b, -1) for _, b in tasks.values()], key=lambda x: (x[0], x[1]))
            concurrent = peak = 0
            for _, change in events:
                concurrent += change
                peak = max(peak, concurrent)
            if peak != needed or peak != partition["certificates"][gid][key]["peak_witness"]["count"]:
                raise AssertionError(f"Independent peak disagrees in {pid} {gid} {key}")
            verify_certificate(tasks, partition['certificates'][gid][key],
                               [r for r in assignments_by_partition[pid]
                                if r['group']==gid and r['resource_type']==key])
            original = defaultdict(list)
            for tid,interval in tasks.items():
                if key.startswith('relay_'):
                    row=fixed['relay'][tid]
                    old=row['relay' if key=='relay_aircraft' else 'energy_component']
                else:
                    row=fixed['transport'][tid]
                    old=row['drone' if key.endswith('_aircraft') else 'battery']
                original[old].append(interval)
            old_gaps=[]
            for intervals in original.values():
                ordered=sorted(intervals)
                old_gaps.extend(b[0]-a[1] for a,b in zip(ordered,ordered[1:]))
            old_min=min(old_gaps) if old_gaps else None
            saved_min=partition['certificates'][gid][key]['original_id_min_handoff_slack_seconds']
            if (old_min is None)!=(saved_min is None) or (old_min is not None and
                    not isclose(old_min,saved_min,rel_tol=0.,abs_tol=EPS)):
                raise AssertionError('Original resource handoff comparison differs')
            horizon = group_by_id[gid]["horizon_seconds"]
            occupancy = (sum(b - a for a, b in tasks.values()) / (needed * horizon)
                         if needed else 0.0)
            if not isclose(occupancy, group_by_id[gid]["resource_occupancy_ratio"][key], abs_tol=EPS):
                raise AssertionError(f"Occupancy ratio mismatch in {pid} {gid} {key}")
        for (gid, key, rid), rows in by_resource.items():
            rows.sort(key=lambda r: (r["start"], r["task"]))
            for before, after in zip(rows, rows[1:]):
                if before["release"] > after["start"] + EPS:
                    raise AssertionError(f"Occupied resource reused too early: {pid} {rid}")
                if before["next_task"] != after["task"]:
                    raise AssertionError(f"Broken chain: {pid} {rid}")
                if not isclose(before["handoff_slack_seconds"],
                               after["start"] - before["release"], abs_tol=EPS):
                    raise AssertionError(f"Wrong handoff slack: {pid} {rid}")
            if rows[-1]["next_task"] or rows[-1]["handoff_slack_seconds"] is not None:
                raise AssertionError(f"Last chain task has a successor: {pid} {rid}")
        for key in RESOURCE_KEYS:
            demand = sum(g["resource_need"][key] for g in groups)
            if demand != partition["resource_need"][key]:
                raise AssertionError(f"Resource aggregation mismatch in {pid} {key}")
            if max(0, demand - result["stock"][key]) != partition["shortage"][key]:
                raise AssertionError(f"Stock shortage mismatch in {pid} {key}")
            new_ids = {r['physical_id'] for r in assignments_by_partition[pid]
                       if r['resource_type']==key and r['inventory_status']=='procurement_required'}
            if len(new_ids) != partition['shortage'][key]:
                raise AssertionError('Procurement mapping differs from resource shortage')
        work = [g['work_seconds'] for g in groups]
        mean = sum(work)/len(work)
        cv = sqrt(sum((w-mean)**2 for w in work)/len(work))/mean
        if (partition['resource_total']!=sum(partition['resource_need'].values()) or
                partition['shortage_total']!=sum(partition['shortage'].values()) or
                partition['stock_sufficient']!=(not any(partition['shortage'].values())) or
                not isclose(cv,partition['workload_cv'],rel_tol=0.,abs_tol=EPS)):
            raise AssertionError('Partition totals, stock feasibility or workload CV differ')
        assignment_count += len(assignments_by_partition[pid])
        partition_checks[pid] = dict(status="PASS", groups=len(groups),
                                     transport_tasks=len(all_transport), relay_tasks=len(all_relay),
                                     assignment_rows=len(assignments_by_partition[pid]),
                                     stock_sufficient=not any(partition["shortage"].values()),
                                     executable_after_procurement=True)
    return dict(status="PASS", scope="fixed Q3, all legal Q4 partitions and all eight resource types",
                component_methods="union-find and independent graph traversal",
                proof_methods="independent interval peak, Edmonds-Karp flow, descending bottleneck thresholds and assignment replay",
                optimality_certificates_independently_verified=True,
                physical_inventory_mapping_verified=True,
                assignment_rows=assignment_count, partitions=partition_checks,
                q3_tasks_unchanged=True, all_boxes_delivered_once=True,
                all_communication_intervals_certified=True,
                resources_not_shared_between_groups=True,
                ready_before_reuse=True)
