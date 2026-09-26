"""Shorten Q3 makespan while protecting the same two/three Q4 partitions.

Search outputs never replace the previous delivered plan. Only independently
certified candidates within explicit resource, balance and quality limits are
marked accepted. CP-SAT bounds describe its finite conservative model only.
"""
from pathlib import Path
import argparse
from copy import deepcopy
import hashlib
import json

from model import Scenario, BASE
from q3_joint import Geometry, site_pool, solve
from q3_upgrade import dump_json
from optimize_q3_timing import optimize
from q4_exact import solve as solve_q4
from deliver_q34_compromise import choose
from search_q34_compromise import save_candidate

ROOT = Path(__file__).resolve().parent
DEFAULT = ROOT/'results_q34_compromise/selection/primary/q3_plan.json'


def matching(q4, old):
    zones = {frozenset(g['zones']) for g in old['groups']}
    return next((p for p in q4['partitions']
                 if {frozenset(g['zones']) for g in p['groups']} == zones), None)


def evaluate(plan, q4, baseline, protected, cv_tolerance=.01, quality_tolerance=.01,
             resource_policy='typed', resource_allowance=0):
    failures, partitions = [], {}
    for old in protected:
        new = matching(q4, old)
        if new is None:
            failures.append(f"{old['group_count']}-group partition disappeared")
            continue
        partitions[str(old['group_count'])] = {
            k: new[k] for k in ('id','resource_need','shortage_total','resource_total','workload_cv','groups')}
        if resource_policy == 'typed' and any(new['resource_need'][k] > v for k,v in old['resource_need'].items()):
            failures.append(f"{old['group_count']}-group typed resources increased")
        if (new['shortage_total'] > old['shortage_total'] + resource_allowance
                or new['resource_total'] > old['resource_total'] + resource_allowance):
            failures.append(f"{old['group_count']}-group shortage or total resources increased")
        if new['workload_cv'] > old['workload_cv'] + cv_tolerance + 1e-7:
            failures.append(f"{old['group_count']}-group CV exceeded tolerance")
    obj = plan['objective']
    if obj['makespan'] >= baseline['objective']['makespan'] - 1e-3:
        failures.append('No makespan improvement')
    if obj['weighted_soft_delay'] > 1e-6:
        failures.append('Soft delay is positive')
    for key in ('energy_kwh','weighted_delivery_seconds'):
        if obj[key] > baseline['objective'][key]*(1+quality_tolerance) + 1e-6:
            failures.append(f'{key} exceeded tolerance')
    return dict(status='ACCEPTED' if not failures else 'REJECTED', failures=failures,
                objective=obj, partitions=partitions,
                saved_seconds=baseline['objective']['makespan']-obj['makespan'])


def protected_lp(s, source, protected, baseline, quality_tolerance):
    q4 = solve_q4(s, source, json.dumps(source).encode())
    two, three = [matching(q4, old) for old in protected]
    if two is None or three is None:
        raise ValueError('Cannot polish candidate with changed partition membership')
    chains=[(two['id'],g['id'],k) for g in two['groups'] for k in g['resource_need']
            if k.endswith('_batteries') or (k.endswith('_aircraft') and k!='relay_aircraft')]
    return optimize(s, source, 5., policy='makespan',
        delivery_cap=baseline['objective']['weighted_delivery_seconds']*(1+quality_tolerance),
        makespan_cap=baseline['objective']['makespan'],
        preserve_partition=three['id'], preserve_q4_resources=chains)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline',type=Path,default=DEFAULT)
    p.add_argument('--input',type=Path)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--data-dir',type=Path,default=BASE)
    p.add_argument('--mode',choices=['lp','cp'],default='cp')
    p.add_argument('--seconds',type=float,default=60.)
    p.add_argument('--seed',type=int,default=2121)
    p.add_argument('--fixed-models',action='store_true')
    p.add_argument('--cv-tolerance',type=float,default=.01)
    p.add_argument('--quality-tolerance',type=float,default=.01)
    p.add_argument('--resource-policy',choices=['typed','shortage'],default='typed')
    p.add_argument('--resource-allowance',type=int,choices=[0,1],default=0)
    a=p.parse_args()
    if not 0 <= a.cv_tolerance <= .1 or not 0 <= a.quality_tolerance <= .1:
        p.error('Tolerances must be between 0 and 0.1')
    s=Scenario(a.data_dir)
    baseline=json.loads(a.baseline.read_text(encoding='utf8'))
    source=json.loads((a.input or a.baseline).read_text(encoding='utf8'))
    q4=solve_q4(s,baseline,a.baseline.read_bytes())
    protected=[choose(q4,k) for k in (2,3)]
    groups=[g['zones'] for g in protected[1]['groups']]
    limits=[]
    for old in protected:
        blocks=[[i for i,g in enumerate(groups) if set(g)<=set(row['zones'])] for row in old['groups']]
        limit=dict(blocks=blocks,shortage_total=old['shortage_total']+a.resource_allowance,
                   resource_total=old['resource_total']+a.resource_allowance)
        if a.resource_policy=='typed':
            limit['resource_need']=old['resource_need']
        limits.append(limit)
    config={k:str(v) if isinstance(v,Path) else v for k,v in vars(a).items()}
    config.update(baseline_sha256=hashlib.sha256(a.baseline.read_bytes()).hexdigest(),
                  input_sha256=hashlib.sha256((a.input or a.baseline).read_bytes()).hexdigest(),
                  groups=groups,limits=limits)
    dump_json(a.output/'configuration.json',config)
    if a.mode=='lp':
        plan,after,report=protected_lp(s,source,protected,baseline,a.quality_tolerance)
        dump_json(a.output/'timing_report.json',report)
    else:
        geometry=Geometry(s,site_pool(s,baseline),extra=1.,interval=12.)
        candidate_source=deepcopy(dict(sorties=source['transport_sorties'],relay_sorties=source['relay_sorties']))
        caps=dict(soft=0, makespan=baseline['objective']['makespan'],
                  energy=baseline['objective']['energy_kwh']*(1+a.quality_tolerance)*1e6,
                  weighted=baseline['objective']['weighted_delivery_seconds']*(1+a.quality_tolerance),
                  sorties=len(baseline['relay_sorties']))
        plan,report=solve(s,candidate_source,geometry,seconds=a.seconds,seed=a.seed,
            slots=2,variable_models=not a.fixed_models,policy='makespan',q4_groups=groups,
            resource_buffer=5.,objective_caps=caps,partition_resource_limits=limits)
        dump_json(a.output/'solver_report.json',report)
        if plan is None:
            dump_json(a.output/'acceptance.json',dict(status='NO_PLAN',stages=report['stages']))
            return
    plan,after,_=save_candidate(s,plan,a.output)
    result=evaluate(plan,after,baseline,protected,a.cv_tolerance,a.quality_tolerance,a.resource_policy,a.resource_allowance)
    dump_json(a.output/'acceptance.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='partitions'},ensure_ascii=False),flush=True)
    if a.mode=='cp':
        try:
            polished,pq4,lp_report=protected_lp(s,plan,protected,baseline,a.quality_tolerance)
            polished,pq4,_=save_candidate(s,polished,a.output/'polished')
            dump_json(a.output/'polished/timing_report.json',lp_report)
            verdict=evaluate(polished,pq4,baseline,protected,a.cv_tolerance,a.quality_tolerance,a.resource_policy,a.resource_allowance)
            dump_json(a.output/'polished/acceptance.json',verdict)
            print(json.dumps({k:v for k,v in verdict.items() if k!='partitions'},ensure_ascii=False),flush=True)
        except (ValueError,RuntimeError,AssertionError) as exc:
            dump_json(a.output/'polish_rejection.json',dict(error=str(exc)))


if __name__=='__main__': main()
