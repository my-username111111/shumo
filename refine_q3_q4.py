"""Bounded joint experiments: explicit handoff buffers and group-owned resources."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
from time import perf_counter

from model import Scenario
from q3_joint import Geometry, site_pool, solve
from q3_certificate import accept
from q3_upgrade import dump_json
from q4 import coupled_components, _canonical_partitions
from q4_exact import solve as solve_q4
from q4_audit import audit

ROOT = Path(__file__).resolve().parent


def handoff_minima(plan):
    result = {}
    for kind, rows, key, end in (
        ('aircraft',plan['transport_sorties'],'drone','return_time'),
        ('battery',plan['transport_sorties'],'battery','battery_ready'),
        ('relay',plan['relay_sorties'],'relay','relay_ready'),
        ('component',plan['relay_sorties'],'energy_component','component_ready')):
        gaps=[]
        for rid in sorted({r[key] for r in rows}):
            tasks=sorted((r for r in rows if r[key]==rid),key=lambda r:r['start'])
            gaps.extend(b['start']-a[end] for a,b in zip(tasks,tasks[1:]))
        result[kind]=min(gaps) if gaps else None
    return result


def balanced_groups(s, plan):
    blocks,_=coupled_components(s,{**plan,'communication':[]})
    options=[]
    for labels in _canonical_partitions(len(blocks),2):
        groups=[[z for block,label in zip(blocks,labels) if label==i for z in block] for i in (0,1)]
        work=[sum(t['return_time']-t['start'] for t in plan['transport_sorties']
                  if {z for z,_ in t['visits']}<=set(group)) for group in groups]
        options.append((abs(work[0]-work[1])/sum(work),groups))
    return sorted(options,key=lambda x:(x[0],x[1]))


def rebatch_bottleneck(s, source):
    """Transfer selected boxes to existing C flights so two B routes can use A."""
    result=deepcopy(source);changes=[]
    by_id={r['id']:r for r in result['sorties']}
    for origin,bid in [('T009','S006-MED-01'),('T023','S005-HYG-01')]:
        row=by_id[origin]
        stripped=[(z,[b for b in bs if b!=bid]) for z,bs in row['visits']]
        stripped=[(z,bs) for z,bs in stripped if bs]
        s.route('A',stripped)
        options=[]
        for target in result['sorties']:
            if target['model']!='C' or any(z=='S001' for z,_ in target['visits']):continue
            for position in range(len(target['visits'])+1):
                visits=[(z,list(bs)) for z,bs in target['visits']]
                zone=s.boxes[bid].zone
                if any(z==zone for z,_ in visits):
                    for z,bs in visits:
                        if z==zone:bs.append(bid)
                else:visits.insert(position,(zone,[bid]))
                try:
                    route=s.route('C',visits)
                    options.append((route['energy_kwh']-s.route('C',target['visits'])['energy_kwh'],target['id'],visits))
                except ValueError:continue
        if options:
            delta,tid,visits=min(options,key=lambda x:(x[0],x[1]))
            row['visits']=stripped;row['model']='A';by_id[tid]['visits']=visits
            changes.append(dict(box=bid,origin=origin,target=tid,origin_model='A',extra_target_energy=delta))
    return result,changes


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,default=ROOT/'results_q3_complete/q3_plan.json')
    parser.add_argument('--output',type=Path,default=ROOT/'results_q3_q4_refined')
    parser.add_argument('--seconds',type=float,default=12.)
    parser.add_argument('--buffer',type=float,default=10.)
    parser.add_argument('--interval',type=float,default=90.)
    parser.add_argument('--cases',nargs='+',default=['timely_variable','fast_buffer'])
    parser.add_argument('--seed',type=int,default=2028)
    parser.add_argument('--allow-soft-delay',action='store_true')
    parser.add_argument('--rebatch-b',action='store_true')
    args=parser.parse_args()
    s=Scenario(); baseline=json.loads(args.input.read_text(encoding='utf8'))
    out=args.output;out.mkdir(parents=True,exist_ok=True)
    source=dict(sorties=baseline['transport_sorties'],relay_sorties=baseline['relay_sorties'])
    changes=[]
    if args.rebatch_b:source,changes=rebatch_bottleneck(s,source)
    geometry=Geometry(s,site_pool(s,baseline),interval=args.interval)
    blocks=baseline['q4_compatibility']['coupled_components']
    if len(blocks)!=3:
        raise ValueError('This bounded experiment matrix requires a certified three-block baseline')
    two=[sorted(blocks[0]+blocks[1]),blocks[2]]
    balanced=balanced_groups(s,baseline)
    configurations={
        'timely_buffer':dict(variable_models=False),
        'timely_variable':dict(variable_models=True),
        'independent_two':dict(variable_models=False,q4_groups=two,independent_groups=True),
        'independent_two_variable':dict(variable_models=True,q4_groups=two,independent_groups=True),
        'isolate_s001':dict(variable_models=True,q4_groups=[blocks[0],sorted(blocks[1]+blocks[2])],independent_groups=True),
        'outer_pair':dict(variable_models=True,q4_groups=[sorted(blocks[0]+blocks[2]),blocks[1]],independent_groups=True),
        'balanced_two':dict(variable_models=True,q4_groups=balanced[0][1],independent_groups=True),
        'balanced_two_fixed':dict(variable_models=False,q4_groups=balanced[0][1],independent_groups=True),
        'balanced_shared':dict(variable_models=True,q4_groups=balanced[0][1]),
        'balanced_shared_fixed':dict(variable_models=False,q4_groups=balanced[0][1]),
        'independent_three':dict(variable_models=True,q4_groups=blocks,independent_groups=True),
        'fast_buffer':dict(variable_models=False,policy='makespan'),
    }
    for name in args.cases:
        if name not in configurations:
            raise ValueError('Unknown case: '+name)
        settings=configurations[name]
        started=perf_counter()
        report={}
        try:
            candidate, report=solve(s,source,geometry,seconds=args.seconds,seed=args.seed,
                                   resource_buffer=args.buffer,
                                   objective_caps={} if args.allow_soft_delay else {'soft':0},**settings)
            report.update(label=name,settings=settings,box_transfers=changes)
            if candidate is not None:
                candidate['q2_seed']=name
                candidate=accept(s,candidate)
                minima=handoff_minima(candidate)
                if any(v is not None and v<args.buffer-1e-7 for v in minima.values()):
                    raise AssertionError('Exported resource handoff buffer is insufficient')
                payload=json.dumps(candidate,ensure_ascii=False,indent=2).encode('utf8')
                q4=solve_q4(s,candidate,payload)
                checked=audit(s,candidate,q4)
                report.update(status='PASS',objective=candidate['objective'],handoff_minima=minima,
                              q4=[dict(id=p['id'],groups=p['group_count'],shortage=p['shortage_total'],
                                       resources=p['resource_total'],cv=p['workload_cv']) for p in q4['partitions']],
                              q4_validation=checked['status'])
                dump_json(out/'plans'/f'{name}.json',candidate)
                dump_json(out/'q4'/f'{name}.json',q4)
            else:
                report['status']=('INFEASIBLE_FINITE_MODEL' if report['stages'][-1]['status']=='INFEASIBLE'
                                  else 'NO_SOLUTION_WITHIN_BUDGET')
        except (ValueError,AssertionError) as exc:
            report.update(label=name,status='REJECTED',error=str(exc),settings=settings)
        report['wall_seconds']=perf_counter()-started
        dump_json(out/'runs'/f'{name}.json',report)
        print(json.dumps(report,ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
