"""Search Q3 task-group bindings, then freeze and independently audit Q4.

Partition shortage caps apply to the sum of per-group resource peaks while
Q3 continues to use only the real global inventory. Ranking is a heuristic;
all reported CVs include actual relay work after continuous certification.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha256
from itertools import combinations
import json
from math import sqrt
from pathlib import Path

from model import Scenario
from q3_joint import Geometry, site_pool, solve, prepare_modes
from q3_certificate import accept
from q3_upgrade import dump_json
from q4 import coupled_components, _canonical_partitions
from q4_exact import solve as solve_q4
from q4_audit import audit
from refine_q3_q4 import rebatch_bottleneck

ROOT = Path(__file__).resolve().parent


def regional_source(s, source):
    """Split only cross-region visits, with original boxes conserved.

    Regions follow the three incumbent relay coverage areas. Direct zones
    retain their own block and can be allocated by workload in the search.
    """
    regions = [['S003','S005','S007','S015'], ['S002','S004','S008','S009'],
               ['S010','S012','S013','S014'], ['S001'], ['S006'], ['S011']]
    owner = {z:i for i,g in enumerate(regions) for z in g}
    result=deepcopy(source); rows=[]; changes=[]
    for row in result['sorties']:
        visits=row['visits']
        labels=sorted({owner[z] for z,_ in visits})
        if len(labels)==1:
            rows.append(row); continue
        for index, label in enumerate(labels):
            part=deepcopy(row);part['id']=row['id']+f'_{index+1}'
            part['visits']=[(z,bs) for z,bs in visits if owner[z]==label]
            options=[]
            for typ in s.transport:
                try: options.append(s.route(typ,part['visits']))
                except ValueError: pass
            if not options: raise ValueError('Regional route cannot carry its boxes')
            part['model']=min(options,key=lambda r:r['energy_kwh'])['model']
            rows.append(part)
        changes.append(dict(split_sortie=row['id'],regions=labels))
    result['sorties']=rows
    return result,regions,changes


def rank_groups(s, source, geometry, count, blocks=None):
    if blocks is None:
        blocks, _ = coupled_components(s, dict(transport_sorties=source['sorties'],
                                              relay_sorties=[], communication=[]))
    modes = prepare_modes(s, source, geometry)
    work = [min(m['route']['duration'] for m in options) for options in modes]
    # Screening uses the current route model, not an optimistic union of modes.
    demands = []
    for row, options in zip(source['sorties'], modes):
        option = next((m for m in options if m['model'] == row['model']), options[0])
        demands.append([set(d['sites']) for d in option['demands']])
    ranked = []
    for labels in _canonical_partitions(len(blocks), count):
        groups = [sorted(z for block, label in zip(blocks, labels) if label == g
                         for z in block) for g in range(count)]
        loads = []; site_counts = []
        for group in groups:
            jobs = [i for i, row in enumerate(source['sorties'])
                    if {z for z, _ in row['visits']} <= set(group)]
            loads.append(sum(work[i] for i in jobs))
            needs = [d for i in jobs for d in demands[i]]
            minimum = 0
            if needs:
                for n in range(1, len(geometry.sites)+1):
                    if any(all(set(c) & d for d in needs)
                           for c in combinations(range(len(geometry.sites)), n)):
                        minimum = n
                        break
            site_counts.append(minimum)
        mean = sum(loads)/count
        cv = sqrt(sum((v-mean)**2 for v in loads)/count)/mean
        ranked.append(dict(groups=groups, transport_cv=cv, site_counts=site_counts,
                           # Prefer reduced relay duplication, then balanced work.
                           score=cv + .15*sum(site_counts)))
    return sorted(ranked, key=lambda x:(x['score'], x['groups']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=ROOT/'results_q3_energy_upgrade/selection/low_energy_26/q3_plan.json')
    parser.add_argument('--output', type=Path, default=ROOT/'results_q3_q4_balance')
    parser.add_argument('--groups', type=int, choices=[2,3], default=2)
    parser.add_argument('--shortage', type=int, default=0)
    parser.add_argument('--seconds', type=float, default=12.)
    parser.add_argument('--limit', type=int, default=5)
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--buffer', type=float, default=5.)
    parser.add_argument('--soft-cap', type=float, default=0.)
    parser.add_argument('--rebatch', action='store_true')
    parser.add_argument('--regional', action='store_true')
    parser.add_argument('--fixed-models', action='store_true')
    parser.add_argument('--policy', choices=['timely','energy','makespan'], default='timely')
    parser.add_argument('--extra-loss', type=float, default=1.)
    args = parser.parse_args()
    s=Scenario(); baseline=json.loads(args.input.read_text(encoding='utf8'))
    source=deepcopy(dict(sorties=baseline['transport_sorties'], relay_sorties=baseline['relay_sorties']))
    changes=[]
    if args.rebatch:
        source,changes=rebatch_bottleneck(s, source)
    blocks=None
    if args.regional:
        source,blocks,splits=regional_source(s,source)
        changes+=splits
    geometry=Geometry(s,site_pool(s,baseline),extra=args.extra_loss,interval=90.)
    ranked=rank_groups(s,source,geometry,args.groups,blocks)
    out=args.output
    dump_json(out/'configuration.json',dict(arguments={k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items()},
        input_sha256=sha256(args.input.read_bytes()).hexdigest(),box_transfers=changes,
        ranking_scope='transport-only workload plus finite-site count heuristic; final CV includes relay work'))
    dump_json(out/'ranked_groups.json',ranked)
    for index,item in list(enumerate(ranked))[args.offset:args.offset+args.limit]:
        label=f'g{args.groups}_{index:03d}'
        print(json.dumps(dict(stage='starting',label=label,**item)),flush=True)
        report={}
        try:
            plan,report=solve(s,source,geometry,seconds=args.seconds,seed=2037+index,
                q4_groups=item['groups'],resource_buffer=args.buffer,
                variable_models=not args.fixed_models,
                independent_groups=args.shortage==0,
                max_partition_shortage=args.shortage,policy=args.policy,
                objective_caps={'soft':args.soft_cap})
            if plan is None:
                report['status']='INFEASIBLE_FINITE_MODEL' if any(x['status']=='INFEASIBLE' for x in report['stages']) else 'NO_INCUMBENT_WITHIN_BUDGET'
            else:
                plan['q2_seed']=label
                plan=accept(s,plan)
                payload=json.dumps(plan,ensure_ascii=False,indent=2).encode('utf8')
                q4=solve_q4(s,plan,payload); checked=audit(s,plan,q4)
                target={frozenset(g) for g in item['groups']}
                selected=next(p for p in q4['partitions'] if {frozenset(g['zones']) for g in p['groups']}==target)
                if selected['shortage_total']>args.shortage:
                    raise AssertionError('Independent Q4 shortage exceeds CP cap')
                report.update(status='PASS',objective=plan['objective'],q4_validation=checked['status'],
                    target_partition=selected['id'],target_shortage=selected['shortage_total'],
                    target_cv=selected['workload_cv'],target_resources=selected['resource_need'],
                    q4=[{k:p[k] for k in ('id','group_count','shortage_total','workload_cv','resource_total')} for p in q4['partitions']])
                dump_json(out/'plans'/f'{label}.json',plan)
                dump_json(out/'q4'/f'{label}.json',q4)
        except (ValueError,AssertionError) as exc:
            report.update(status='REJECTED',error=str(exc))
        report.update(label=label,requested_groups=item['groups'])
        dump_json(out/'runs'/f'{label}.json',report)
        print(json.dumps({k:v for k,v in report.items() if k in ('label','status','objective','target_shortage','target_cv','error')},ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
