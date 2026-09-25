"""Exact continuous-time retiming for a fixed, certified Q3 task structure.

The selected routes, drone/battery identities, relay sites, and original
relay coverage obligations are frozen. Only launch times and relay service
durations change. Independent recertification may choose a different relay
over an overlap; the actual relation and Q4 are then rebuilt and audited.
The LP optimizes weighted delivery, makespan, then relay service energy in
lexicographic order. Optimality applies to this fixed-structure LP only.
Optional Q4 resource chains can also be frozen to preserve a group peak bound.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path

from ortools.linear_solver import pywraplp

from model import Scenario
from q2_exact_schedule import assemble_plan
from q3_certificate import accept
from q4_audit import audit, verify_fixed_q3
from q4_exact import solve as solve_q4
from q3_upgrade import dump_json


ROOT = Path(__file__).resolve().parent
EPS = 1e-4


def optimize(s: Scenario, original: dict, buffer: float,
             policy: str = 'timely', delivery_cap: float | None = None,
             makespan_cap: float | None = None,
             allow_soft_delay: bool = False,
             preserve_q4_resources: list[tuple[str, str, str]] | None = None
             ) -> tuple[dict, dict, dict]:
    """Return certified plan, exact Q4, and the fixed-structure LP record."""
    if not isfinite(buffer) or buffer < 0:
        raise ValueError('Resource handoff buffer must be nonnegative')
    if any(x is not None and (not isfinite(x) or x < 0)
           for x in (delivery_cap, makespan_cap)):
        raise ValueError('Objective caps must be finite and nonnegative')
    if policy not in ('timely', 'energy'):
        raise ValueError('Unknown timing objective policy')
    verify_fixed_q3(s, original)
    solver = pywraplp.Solver.CreateSolver('GLOP')
    if solver is None:
        raise RuntimeError('OR-Tools GLOP is unavailable')
    infinity = solver.infinity()
    transports = {r['id']: r for r in original['transport_sorties']}
    relays = {r['id']: r for r in original['relay_sorties']}
    starts = {tid: solver.NumVar(0., infinity, 'start_' + tid) for tid in transports}
    launches = {rid: solver.NumVar(0., infinity, 'launch_' + rid) for rid in relays}
    service_ends = {rid: solver.NumVar(0., infinity, 'service_end_' + rid) for rid in relays}
    makespan = solver.NumVar(0., infinity, 'makespan')
    relay_physics = {}
    for rid, row in relays.items():
        lead = row['established'] - row['start']
        back = row['return_time'] - row['service_end']
        slope = (s.relay.hover_kw + s.relay.comm_kw) / 3600.
        service = row['service_end'] - row['established']
        limit = s.relay.battery_kwh * (1 - s.relay.reserve)
        max_service = service + (limit - row['energy_kwh']) / slope
        if max_service < service - 1e-6:
            raise ValueError('Original relay exceeds its energy limit')
        solver.Add(service_ends[rid] >= launches[rid] + lead)
        solver.Add(service_ends[rid] <= launches[rid] + lead + max_service - EPS)
        solver.Add(makespan >= service_ends[rid] + back)
        relay_physics[rid] = dict(lead=lead, back=back, slope=slope,
                                  base_energy=row['energy_kwh'] - slope * service)
    soft_terms = []
    for tid, row in transports.items():
        solver.Add(makespan >= starts[tid] + row['route']['duration'])
        for bid, relative in row['route']['delivery'].items():
            box = s.boxes[bid]
            if box.hard_deadline is not None:
                solver.Add(starts[tid] + relative <= box.hard_deadline)
            elif allow_soft_delay:
                late = solver.NumVar(0., infinity, 'late_' + bid)
                solver.Add(late >= starts[tid] + relative - box.desired)
                soft_terms.append(box.priority * late)
            else:
                solver.Add(starts[tid] + relative <= box.desired)
    # Freeze the actual physical-resource sequence, but allow all start times
    # to move earlier or later. Charging is already included in battery_ready.
    for rows, key, end in (
        (transports.values(), 'drone', 'return_time'),
        (transports.values(), 'battery', 'battery_ready'),
        (relays.values(), 'relay', 'relay_ready'),
        (relays.values(), 'energy_component', 'component_ready'),
    ):
        chains = defaultdict(list)
        for row in rows:
            chains[row[key]].append(row)
        for chain in chains.values():
            chain.sort(key=lambda row: (row['start'], row['id']))
            for old, new in zip(chain, chain[1:]):
                if key in ('drone', 'battery'):
                    elapsed = old[end] - old['start']
                    solver.Add(starts[new['id']] >= starts[old['id']] + elapsed + buffer + EPS)
                else:
                    elapsed = old[end] - old['service_end']
                    solver.Add(launches[new['id']] >= service_ends[old['id']] + elapsed + buffer + EPS)
    # Preserve selected Q4 resource chains while refining the Q3 timetable.
    # Q4 can reassign a battery within a group, so freezing only its Q3
    # physical identity can otherwise undo a group peak-count improvement.
    preserved = []
    original_q4 = None
    if preserve_q4_resources:
        original_q4 = solve_q4(s, original,
            json.dumps(original, ensure_ascii=False).encode('utf8'))
        for partition_id, group_id, resource_key in preserve_q4_resources:
            if resource_key not in {f'{g}_{kind}' for g in 'ABC'
                                    for kind in ('aircraft', 'batteries')}:
                raise ValueError('Q4 chain preservation supports transport resources only')
            partition = next((p for p in original_q4['partitions']
                              if p['id'] == partition_id), None)
            if partition is None or group_id not in partition['certificates']:
                raise ValueError('Unknown Q4 partition or group')
            cert = partition['certificates'][group_id][resource_key]
            end = 'battery_ready' if resource_key.endswith('batteries') else 'return_time'
            for chain in cert['chains']:
                for predecessor, successor in zip(chain, chain[1:]):
                    row = transports[predecessor]
                    elapsed = row[end] - row['start']
                    solver.Add(starts[successor] >= starts[predecessor] +
                               elapsed + buffer + EPS)
            preserved.append(dict(partition=partition_id, group=group_id,
                                  resource=resource_key,
                                  maximum=cert['minimum_resources']))
    # A previously certified relay interval remains on the same relay. Its
    # geometry is invariant under a pure shift of the transport trajectory.
    relay_rows = 0
    for interval in original['communication_intervals']:
        if interval['mode'] != 'relay':
            continue
        tid, rid = interval['sortie'], interval['relay_sortie']
        old_start = transports[tid]['start']
        solver.Add(launches[rid] + relay_physics[rid]['lead'] + EPS <=
                   starts[tid] + interval['start'] - old_start)
        solver.Add(service_ends[rid] >= starts[tid] + interval['end'] - old_start + EPS)
        relay_rows += 1
    priority = {tid: sum(s.boxes[bid].priority for bid in row['route']['box_ids'])
                for tid, row in transports.items()}
    weighted = solver.Sum(priority[tid] * starts[tid] for tid in starts)
    delivery_offset = sum(s.boxes[bid].priority * relative
                          for row in transports.values()
                          for bid, relative in row['route']['delivery'].items())
    relay_duration = solver.Sum(service_ends[rid] - launches[rid] - relay_physics[rid]['lead']
                                for rid in relays)
    if delivery_cap is not None:
        solver.Add(weighted + delivery_offset <= delivery_cap)
    if makespan_cap is not None:
        solver.Add(makespan <= makespan_cap)
    stages = []
    objectives = dict(weighted_start=weighted, makespan=makespan,
                      relay_service_seconds=relay_duration)
    if allow_soft_delay:
        objectives['weighted_soft_delay'] = solver.Sum(soft_terms)
    order = (['weighted_start', 'makespan', 'relay_service_seconds'] if policy == 'timely'
             else ['relay_service_seconds', 'weighted_start', 'makespan'])
    if allow_soft_delay:
        order.insert(0, 'weighted_soft_delay')
    for index, name in enumerate(order):
        expression = objectives[name]
        solver.Minimize(expression)
        status = solver.Solve()
        if status != pywraplp.Solver.OPTIMAL:
            raise RuntimeError(f'Fixed-structure timing LP {name}: {status}')
        value = solver.Objective().Value()
        stages.append(dict(objective=name, value=value, status='OPTIMAL'))
        if index < len(order)-1:
            solver.Add(expression <= value + 1e-5)
    result = deepcopy(original)
    for row in result['transport_sorties']:
        old_start = row['start']
        row['start'] = starts[row['id']].solution_value()
        delta = row['start'] - old_start
        row['return_time'] += delta
        row['battery_ready'] += delta
    for row in result['relay_sorties']:
        rid = row['id']
        row['start'] = launches[rid].solution_value()
        row['established'] = row['start'] + relay_physics[rid]['lead']
        row['service_end'] = service_ends[rid].solution_value()
        trip = s.relay_sortie(row['lon'], row['lat'], row['agl'],
                              row['service_end'] - row['established'])
        row['return_time'] = row['start'] + trip['duration']
        row['relay_ready'] = row['return_time'] + s.relay.turnaround
        row['component_ready'] = row['return_time'] + s.charge_time(trip['return_soc'],s.relay_charge)
        row['energy_kwh'] = trip['energy_kwh']
        row['return_soc'] = trip['return_soc']
    transport = assemble_plan(s, result['transport_sorties'])
    result['deliveries'] = transport['deliveries']
    result['objective'] = dict(weighted_soft_delay=transport['objective']['weighted_soft_delay_seconds'],
        weighted_delivery_seconds=transport['objective']['weighted_delivery_seconds'],
        makespan=max(r['return_time'] for r in result['transport_sorties']+result['relay_sorties']),
        energy_kwh=sum(r['energy_kwh'] for r in result['transport_sorties']+result['relay_sorties']),
        transport_energy_kwh=sum(r['energy_kwh'] for r in result['transport_sorties']),
        relay_energy_kwh=sum(r['energy_kwh'] for r in result['relay_sorties']),
        transport_sorties=len(result['transport_sorties']),relay_sorties=len(result['relay_sorties']),
        total_sorties=len(result['transport_sorties'])+len(result['relay_sorties']))
    old_search = original.get('search',{})
    result['search'] = dict(method='continuous_fixed_structure_lp',
        source_search=old_search,
        timing_refinement='fixed routes, physical resource order and original relay coverage obligations',
        resource_buffer_seconds=buffer,
        q4_constraint=old_search.get('q4_constraint',False),
        independent_groups=old_search.get('independent_groups',False),
        timing_lp_stages=stages,
        timing_bound_scope='Optimal only for the continuous timing LP with frozen routes, aircraft/battery sequence, relay sites and original relay coverage obligations')
    if preserved:
        result['search']['preserved_q4_resources']=preserved
    result['q2_seed'] = f"timing_lp_buffer_{buffer:g}"
    result = accept(s, result)
    verify_fixed_q3(s, result)
    original_starts = {r['id']: r['start'] for r in original['transport_sorties']}
    updated_starts = {r['id']: r['start'] for r in result['transport_sorties']}
    updated_intervals = defaultdict(list)
    for interval in result['communication_intervals']:
        updated_intervals[interval['sortie'], interval['phase_index']].append(interval)
    relation_changed_seconds = 0.
    relation_changed_intervals = 0
    for interval in original['communication_intervals']:
        if interval['mode'] != 'relay':
            continue
        shift = updated_starts[interval['sortie']] - original_starts[interval['sortie']]
        left, right = interval['start'] + shift, interval['end'] + shift
        changed = sum(max(0., min(right, row['end']) - max(left, row['start']))
            for row in updated_intervals[interval['sortie'], interval['phase_index']]
            if row['mode'] != 'relay' or row['relay_sortie'] != interval['relay_sortie'])
        if changed > 1e-6:
            relation_changed_intervals += 1
            relation_changed_seconds += changed
    payload = json.dumps(result, ensure_ascii=False, indent=2).encode('utf8')
    q4 = solve_q4(s, result, payload)
    checked = audit(s, result, q4)
    if checked['status'] != 'PASS':
        raise AssertionError('Exact Q4 audit failed')
    for cap in preserved:
        new_partition = next((p for p in q4['partitions']
                              if p['id'] == cap['partition']), None)
        if new_partition is None:
            raise AssertionError('Preserved Q4 partition disappeared')
        new_group = next((g for g in new_partition['groups']
                          if g['id'] == cap['group']), None)
        if new_group is None or new_group['resource_need'][cap['resource']] > cap['maximum']:
            raise AssertionError('Q4 resource cap lost after timing refinement')
    scope='fixed routes, resource order, relay sites and original relay coverage obligations'
    if preserved:
        scope+=', plus selected Q4 group resource chains'
    report = dict(status='PASS',scope=scope,
                  buffer_seconds=buffer, policy=policy, allow_soft_delay=allow_soft_delay,
                  delivery_cap=delivery_cap,
                  makespan_cap=makespan_cap, fixed_relay_intervals=relay_rows,
                  input_sha256=sha256(json.dumps(original,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
                  lp_stages=stages, old_objective=original['objective'],
                  new_objective=result['objective'],
                  recertified_relation_changed_seconds=relation_changed_seconds,
                  recertified_relation_changed_intervals=relation_changed_intervals,
                  q3_independent_validation='PASS',q4_independent_validation=checked['status'],
                  q4=[dict(id=p['id'],groups=p['group_count'],shortage=p['shortage_total'],
                           resources=p['resource_total'],cv=p['workload_cv']) for p in q4['partitions']])
    if preserved:
        report['preserved_q4_resources']=preserved
    return result, q4, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,default=ROOT/'results_q3_q4_resilience/selection/primary/q3_plan.json')
    parser.add_argument('--output',type=Path,default=ROOT/'results_q3_q4_next/timing_lp')
    parser.add_argument('--buffer',type=float,default=5.)
    parser.add_argument('--policy',choices=['timely','energy'],default='timely')
    parser.add_argument('--delivery-cap',type=float)
    parser.add_argument('--makespan-cap',type=float)
    parser.add_argument('--allow-soft-delay',action='store_true')
    parser.add_argument('--preserve-q4-resource',action='append',default=[],
                        help='Preserve a Q4 resource chain, e.g. P4:G2:A_batteries')
    args = parser.parse_args()
    s = Scenario()
    original = json.loads(args.input.read_text(encoding='utf8'))
    preserved = []
    for item in args.preserve_q4_resource:
        parts = item.split(':')
        if len(parts) != 3:
            parser.error('Use --preserve-q4-resource P4:G2:A_batteries')
        preserved.append(tuple(parts))
    plan, q4, report = optimize(s, original, args.buffer, args.policy,
                                args.delivery_cap, args.makespan_cap,
                                args.allow_soft_delay, preserved)
    dump_json(args.output/'q3_plan.json',plan)
    dump_json(args.output/'q4_results.json',q4)
    dump_json(args.output/'report.json',report)
    print(json.dumps(report,ensure_ascii=False),flush=True)


if __name__ == '__main__':
    main()
