"""Search a certified Q3 plan with type-specific Q4 group resource caps.

The input partition fixes the Q4 groups, while transport timing, machine
assignment and relay service are re-optimized as part of Q3. Group caps apply
to conservative occupied intervals and are checked again by exact Q4 replay.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from model import Scenario
from q3_certificate import accept
from q3_joint import Geometry, site_pool, solve
from q3_upgrade import Site, dump_json
from q4_audit import audit
from q4_exact import solve as solve_q4


ROOT = Path(__file__).resolve().parent


def read(path):
    return json.loads(path.read_text(encoding='utf8'))


def parse_cap(value):
    try:
        group, kind, model, cap = value.split(':')
        return int(group.removeprefix('G'))-1, kind, model, int(cap)
    except (ValueError, AttributeError) as exc:
        raise argparse.ArgumentTypeError('Use G2:battery:A:5') from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--partition',choices=('P3','P4'),required=True)
    parser.add_argument('--group-cap',type=parse_cap,action='append',default=[])
    parser.add_argument('--seconds',type=float,default=25.)
    parser.add_argument('--seed',type=int,default=2043)
    parser.add_argument('--sites-json',type=Path)
    parser.add_argument('--fixed-models',action='store_true')
    parser.add_argument('--extra-loss',type=float,default=1.)
    parser.add_argument('--buffer',type=float,default=5.)
    parser.add_argument('--max-makespan',type=float)
    parser.add_argument('--max-weighted-delivery',type=float)
    parser.add_argument('--max-energy',type=float)
    parser.add_argument('--max-relay-sorties',type=int)
    parser.add_argument('--policy',choices=('energy','timely','makespan'),default='energy')
    args = parser.parse_args()
    s = Scenario()
    baseline = read(args.input)
    baseline_q4 = solve_q4(s,baseline,args.input.read_bytes())
    target = next(p for p in baseline_q4['partitions'] if p['id']==args.partition)
    groups = [g['zones'] for g in target['groups']]
    sites = ([Site(**row) for row in read(args.sites_json)] if args.sites_json
             else site_pool(s,baseline))
    geometry = Geometry(s,sites,extra=args.extra_loss,interval=6.)
    caps = {'soft':0}
    for name,value in (('makespan',args.max_makespan),
                       ('weighted',args.max_weighted_delivery),
                       ('energy',args.max_energy),
                       ('sorties',args.max_relay_sorties)):
        if value is not None:
            caps[name]=value
    source={'sorties':baseline['transport_sorties'],
            'relay_sorties':baseline['relay_sorties']}
    plan,report=solve(s,source,geometry,seconds=args.seconds,seed=args.seed,
        variable_models=not args.fixed_models,policy=args.policy,q4_groups=groups,
        resource_buffer=args.buffer,objective_caps=caps,
        group_resource_caps=args.group_cap)
    report.update(input=str(args.input),partition=args.partition,
                  group_zones=groups,group_resource_caps=args.group_cap)
    if plan is None:
        report['status']='NO_CERTIFIED_PLAN'
        dump_json(args.output/'report.json',report)
        print(json.dumps({'status':report['status'],'stage':report['stages'][-1]},ensure_ascii=False))
        return
    plan=accept(s,plan,args.extra_loss)
    q4=solve_q4(s,plan,json.dumps(plan,ensure_ascii=False).encode('utf8'))
    checked=audit(s,plan,q4)
    target_new=next(p for p in q4['partitions'] if p['id']==args.partition)
    for group_index,kind,typ,cap in args.group_cap:
        actual=target_new['groups'][group_index]['resource_need'][f'{typ}_{kind if kind=="aircraft" else "batteries"}']
        if actual>cap:
            raise AssertionError(f'Q4 cap not met: {group_index,kind,typ,cap,actual}')
    report.update(status=checked['status'],objective=plan['objective'],
                  q4=[dict(id=p['id'],shortage=p['shortage_total'],
                           resource_total=p['resource_total'],
                           batteries=sum(p['resource_need'][f'{g}_batteries'] for g in 'ABC'),
                           workload_cv=p['workload_cv']) for p in q4['partitions']])
    dump_json(args.output/'q3_plan.json',plan)
    dump_json(args.output/'q4_results.json',q4)
    dump_json(args.output/'report.json',report)
    print(json.dumps({'status':report['status'],'objective':plan['objective'],
                      'q4':report['q4']},ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
