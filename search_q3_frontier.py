"""Nominal Q3 search and fixed-input Q4 assessment, with no zero-shortage constraint."""
from __future__ import annotations
import argparse
from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
import json
from pathlib import Path
from time import perf_counter

from model import Scenario
from q3_joint import Geometry, site_pool, solve, data_signature
from q3_certificate import accept
from q3_upgrade import dump_json, Site
from q4_exact import solve as partition
from q4_audit import audit
from run_q3_joint import nondominated, lower_bounds

ROOT=Path(__file__).resolve().parent


def read(path):return json.loads(path.read_text(encoding='utf8'))


def route_options(s,visits):
    options=[]
    for model in s.transport:
        try:options.append(s.route(model,visits))
        except ValueError:pass
    return options


def neighbors(s,plan):
    """Exact one-box neighborhood restricted to a zone already visited by target."""
    rows=plan['transport_sorties'];variants=[];seen=set()
    old_energy=sum(min(r['energy_kwh'] for r in route_options(s,t['visits'])) for t in rows)
    old_duration=sum(min(r['duration'] for r in route_options(s,t['visits'])) for t in rows)
    for i,row in enumerate(rows):
        for zone,boxes in row['visits']:
            for bid in boxes:
                for j,target in enumerate(rows):
                    if i==j or zone not in [z for z,_ in target['visits']]:continue
                    source_visits=[(z,[b for b in bs if b!=bid]) for z,bs in row['visits']]
                    source_visits=[(z,bs) for z,bs in source_visits if bs]
                    target_visits=[(z,bs+[bid] if z==zone else list(bs)) for z,bs in target['visits']]
                    a=route_options(s,source_visits) if source_visits else [dict(energy_kwh=0.,duration=0.)]
                    b=route_options(s,target_visits)
                    if not a or not b:continue
                    new=deepcopy(rows);new[i]['visits']=source_visits;new[j]['visits']=target_visits
                    if not source_visits:new.pop(i)
                    signature=json.dumps(sorted((tuple((z,tuple(sorted(bs))) for z,bs in r['visits']) for r in new)))
                    if signature in seen:continue
                    seen.add(signature)
                    energy=duration=0.
                    for r in new:
                        choices=route_options(s,r['visits'])
                        energy+=min(x['energy_kwh'] for x in choices)
                        duration+=min(x['duration'] for x in choices)
                        if r['model'] not in [x['model'] for x in choices]:
                            r['model']=min(choices,key=lambda x:x['energy_kwh'])['model']
                    variants.append(dict(label=f'move_{bid}_{row["id"]}_{target["id"]}',
                                         energy_delta=energy-old_energy,duration_delta=duration-old_duration,
                                         source=dict(sorties=new,relay_sorties=plan['relay_sorties'])))
    # Interleave energy and aggregate-duration rankings, without calling them bounds on the joint objective.
    ranked=[];used=set()
    for a,b in zip(sorted(variants,key=lambda x:x['energy_delta']),sorted(variants,key=lambda x:x['duration_delta'])):
        for v in (a,b):
            if v['label'] not in used:ranked.append(v);used.add(v['label'])
    return ranked


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'results_q3_q4_frontier')
    parser.add_argument('--input',type=Path,default=ROOT/'results_q3_complete/q3_plan.json')
    parser.add_argument('--phase',choices=['schedule','neighbors','low_sorties'],default='schedule')
    parser.add_argument('--seconds',type=float,default=30.)
    parser.add_argument('--limit',type=int,default=12)
    parser.add_argument('--offset',type=int,default=0)
    parser.add_argument('--seed',type=int,default=2030)
    parser.add_argument('--expanded-sites',action='store_true')
    parser.add_argument('--refine-sites',action='store_true')
    parser.add_argument('--sites-json',type=Path,help='Explicit candidate Site records from a documented screening run')
    parser.add_argument('--extra-loss',type=float,default=0.)
    parser.add_argument('--buffer',type=float,default=0.)
    parser.add_argument('--allow-soft-delay',action='store_true')
    parser.add_argument('--interval',type=float,default=90.)
    parser.add_argument('--fixed-models',action='store_true')
    parser.add_argument('--max-makespan',type=float)
    parser.add_argument('--max-weighted-delivery',type=float,
                        help='Cap the finite CP weighted-delivery objective in priority-seconds')
    parser.add_argument('--max-relay-sorties',type=int,
                        help='Cap the number of relay missions while searching the energy frontier')
    parser.add_argument('--policies',nargs='+',choices=['timely','energy','makespan'],default=['timely','energy','makespan'])
    args=parser.parse_args();s=Scenario();baseline=read(args.input);out=args.output
    sites=site_pool(s,baseline,refine=args.refine_sites)
    if args.sites_json:
        sites=[Site(**row) for row in read(args.sites_json)]
    if args.expanded_sites:
        old=read(ROOT/'results_q3_upgrade/q3_plan.json')
        unique={(x.lon,x.lat,x.agl):x for x in sites+site_pool(s,old)}
        sites=[replace(x,id=f'F{i:04d}') for i,x in enumerate(unique.values())]
    geometry=Geometry(s,sites,extra=args.extra_loss,interval=args.interval)
    source=dict(sorties=baseline['transport_sorties'],relay_sorties=baseline['relay_sorties'])
    if args.phase=='schedule':
        cases=[dict(label=f'{policy}_{seed}',source=source,policy=policy,seed=seed)
               for seed in (args.seed,args.seed+1) for policy in args.policies]
    elif args.phase=='low_sorties':
        paths=list((ROOT/'results_q2_extended/plans').glob('*.json'))
        cases=[dict(label=p.stem,source=read(p),policy=args.policies[0],seed=args.seed) for p in paths]
    else:
        cases=neighbors(s,baseline)
        dump_json(out/'neighborhood.json',[{k:v for k,v in c.items() if k!='source'} for c in cases])
    cases=cases[args.offset:args.offset+args.limit]
    dump_json(out/'configuration.json',dict(args={k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items()},
             baseline_sha256=sha256(args.input.read_bytes()).hexdigest(),input_fingerprint=data_signature(s),
             site_records=[asdict(x) for x in sites],
             candidate_sites=len(sites),objective_policy=('soft delay minimized' if args.allow_soft_delay else 'zero soft delay')+'; policy-specific lexicographic branches',
             q4_policy='fixed Q3 partitions; report shortages, no stock-feasibility constraint'))
    for index,case in enumerate(cases):
        label=case['label'];start=perf_counter();report={}
        print(json.dumps(dict(stage='starting',label=label,sites=len(sites),interval=args.interval)),flush=True)
        try:
            caps={} if args.allow_soft_delay else {'soft':0}
            if args.max_makespan is not None:caps['makespan']=args.max_makespan
            if args.max_weighted_delivery is not None:caps['weighted']=args.max_weighted_delivery
            if args.max_relay_sorties is not None:caps['sorties']=args.max_relay_sorties
            plan,report=solve(s,case['source'],geometry,seconds=args.seconds,
                              seed=case.get('seed',args.seed+args.offset+index),
                              policy=case.get('policy','timely'),variable_models=not args.fixed_models,
                              objective_caps=caps,resource_buffer=args.buffer)
            if plan is None:
                report['status']='INFEASIBLE_FINITE_MODEL' if report['stages'][-1]['status']=='INFEASIBLE' else 'BUDGET_EXHAUSTED'
            else:
                plan['q2_seed']=label;plan=accept(s,plan,geometry.extra)
                result=partition(s,plan,json.dumps(plan,ensure_ascii=False).encode('utf8'))
                checked=audit(s,plan,result)
                report.update(status='PASS',objective=plan['objective'],components=len(result['components']['components']),
                              q4_validation=checked['status'],q4=[{k:p[k] for k in ('id','group_count','resource_total','shortage_total','workload_cv')} for p in result['partitions']])
                dump_json(out/'plans'/f'{label}.json',plan)
                dump_json(out/'q4'/f'{label}.json',result)
        except (ValueError,AssertionError) as exc:report.update(status='REJECTED',error=str(exc))
        report.update(label=label,elapsed_wall_seconds=perf_counter()-start)
        dump_json(out/'runs'/f'{label}.json',report)
        print(json.dumps(dict(label=label,status=report['status'],objective=report.get('objective'),error=report.get('error')),ensure_ascii=False),flush=True)
    plans=[read(p) for p in (out/'plans').glob('*.json')]
    comparable=[p for p in [baseline]+plans if p.get('extra_loss_db',0.)==args.extra_loss
                and p.get('search',{}).get('resource_buffer_seconds',0.)==args.buffer]
    dump_json(out/'pareto.json',[dict(label=p['q2_seed'],**p['objective']) for p in nondominated(comparable)])
    dump_json(out/'global_relaxation_bounds.json',lower_bounds(s))


if __name__=='__main__':main()
