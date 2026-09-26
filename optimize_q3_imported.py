"""Audit an imported Q3 plan and optimize it under explicit Q4 safeguards."""
import argparse
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import shutil

from model import Scenario
from q3_upgrade import dump_json
from q3_joint import Geometry, site_pool, solve
from q4_audit import verify_fixed_q3
from q4_exact import solve as solve_q4
from search_q34_compromise import save_candidate
from deliver_q34_compromise import choose
from optimize_q3_timing import optimize
from optimize_q3_time_guarded import matching

ROOT = Path(__file__).resolve().parent
OUT = ROOT/'results_q3_imported'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf8'))


def compact(plan, q4, protected=None):
    return dict(objective=plan['objective'],communication=plan['communication'],
                partitions={str(k):{f:p[f] for f in ('id','shortage_total','resource_total','workload_cv','resource_need','groups')}
                for k in (2,3) for p in [matching(q4,next(old for old in protected if old['group_count']==k)) if protected else choose(q4,k)]
                if p is not None})


def partition_atoms(protected):
    """Common refinement, including when the chosen two/three groups cross."""
    atoms=[set(g['zones']) for g in protected[0]['groups']]
    for p in protected[1:]:
        atoms=[left & set(g['zones']) for left in atoms for g in p['groups']
               if left & set(g['zones'])]
    return sorted([sorted(g) for g in atoms],key=lambda g:g[0])


def polish(s,source,baseline,protected,policy):
    source_q4=solve_q4(s,source,json.dumps(source).encode())
    two,three=[matching(source_q4,p) for p in protected]
    if two is None or three is None:raise ValueError('Protected partition not available')
    chains=[(two['id'],g['id'],k) for g in two['groups'] for k in g['resource_need']
            if k.endswith('_batteries') or (k.endswith('_aircraft') and k!='relay_aircraft')]
    return optimize(s,source,5.,policy=policy,
        makespan_cap=baseline['objective']['makespan']+1e-5,
        delivery_cap=baseline['objective']['weighted_delivery_seconds']+1e-5,
        preserve_partition=three['id'],preserve_q4_resources=chains)


def compare(plan,q4,baseline,protected,energy_tolerance=0.,cv_tolerance=.01):
    failures=[]
    for key in ('makespan','weighted_delivery_seconds','energy_kwh'):
        cap=baseline['objective'][key]*(1+energy_tolerance if key=='energy_kwh' else 1)
        if plan['objective'][key]>cap+1e-3:
            failures.append(key+' increased')
    if plan['objective']['weighted_soft_delay']>1e-6:failures.append('soft delay')
    for old in protected:
        new=matching(q4,old)
        if new is None:
            failures.append(f"{old['group_count']}-group membership lost")
            continue
        if new['shortage_total']>old['shortage_total'] or new['resource_total']>old['resource_total']:
            failures.append(f"{old['group_count']}-group resources increased")
        if new['workload_cv']>old['workload_cv']+cv_tolerance:
            failures.append(f"{old['group_count']}-group balance worsened")
    improved=any(plan['objective'][key]<baseline['objective'][key]-1e-3
                 for key in ('makespan','weighted_delivery_seconds','energy_kwh'))
    return dict(status='ACCEPTED' if improved and not failures else 'REJECTED',
                failures=failures,improved=improved,**compact(plan,q4,protected))


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--mode',choices=['audit','lp','cp'],required=True)
    ap.add_argument('--input',type=Path,required=True)
    ap.add_argument('--baseline',type=Path,default=OUT/'baseline/q3_plan.json')
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--policy',choices=['makespan','timely','energy'],default='makespan')
    ap.add_argument('--seconds',type=float,default=60.)
    ap.add_argument('--seed',type=int,default=2401)
    ap.add_argument('--fixed-models',action='store_true')
    ap.add_argument('--discrete-time-slack',type=float,default=5.,
                    help='Rounding allowance for CP candidates; final exact time must not increase')
    ap.add_argument('--energy-tolerance',type=float,default=0.)
    ap.add_argument('--cv-tolerance',type=float,default=.01)
    args=ap.parse_args()
    s=Scenario(); source=read(args.input)
    config={k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items()}
    config['input_sha256']=sha256(args.input.read_bytes()).hexdigest()
    dump_json(args.output/'configuration.json',config)
    if args.mode=='audit':
        checked=verify_fixed_q3(s,source)
        shutil.copyfile(args.input,args.output/'input_original.json')
        dump_json(args.output/'input_verification.json',checked)
        plan,q4,_=save_candidate(s,source,args.output)
        summary=compact(plan,q4)
        dump_json(args.output/'comparison_summary.json',summary)
        print(json.dumps(dict(objective=plan['objective'],q4={k:{f:p[f] for f in ('shortage_total','resource_total','workload_cv')} for k,p in summary['partitions'].items()})),flush=True)
        return
    baseline=read(args.baseline)
    before=solve_q4(s,baseline,args.baseline.read_bytes())
    protected=[choose(before,k) for k in (2,3)]
    if args.mode=='lp':
        plan,_,report=polish(s,source,baseline,protected,args.policy)
        dump_json(args.output/'timing_report.json',report)
    else:
        groups=partition_atoms(protected)
        limits=[dict(blocks=[[i for i,g in enumerate(groups) if set(g)<=set(row['zones'])] for row in old['groups']],
                     shortage_total=old['shortage_total'],resource_total=old['resource_total']) for old in protected]
        geometry=Geometry(s,site_pool(s,baseline),extra=baseline['extra_loss_db'],interval=12.)
        caps=dict(soft=0,makespan=baseline['objective']['makespan']+args.discrete_time_slack,
                  weighted=baseline['objective']['weighted_delivery_seconds']+1500,
                  energy=baseline['objective']['energy_kwh']*(1+args.energy_tolerance)*1e6+10000,
                  sorties=len(baseline['relay_sorties']))
        # Tiny rounding allowances only in discrete search; final acceptance
        # compares exact physical totals with the imported plan, without slack.
        config.update(groups=groups,limits=limits,objective_caps=caps)
        dump_json(args.output/'configuration.json',config)
        plan,report=solve(s,deepcopy(dict(sorties=source['transport_sorties'],relay_sorties=source['relay_sorties'])),
            geometry,seconds=args.seconds,seed=args.seed,slots=2,variable_models=not args.fixed_models,
            policy=args.policy,q4_groups=groups,resource_buffer=5.,objective_caps=caps,
            partition_resource_limits=limits)
        dump_json(args.output/'solver_report.json',report)
        if plan is None:
            dump_json(args.output/'acceptance.json',dict(status='NO_PLAN',stages=report['stages']))
            return
    plan,q4,_=save_candidate(s,plan,args.output)
    verdict=compare(plan,q4,baseline,protected,args.energy_tolerance,args.cv_tolerance)
    dump_json(args.output/'acceptance.json',verdict)
    print(json.dumps(dict(status=verdict['status'],failures=verdict['failures'],objective=plan['objective'])),flush=True)
    if args.mode=='cp':
        try:
            improved,_,report=polish(s,plan,baseline,protected,args.policy)
            improved,iq4,_=save_candidate(s,improved,args.output/'polished')
            dump_json(args.output/'polished/timing_report.json',report)
            verdict=compare(improved,iq4,baseline,protected,args.energy_tolerance,args.cv_tolerance)
            dump_json(args.output/'polished/acceptance.json',verdict)
            print(json.dumps(dict(status=verdict['status'],failures=verdict['failures'],objective=improved['objective'])),flush=True)
        except (ValueError,AssertionError,RuntimeError) as exc:
            dump_json(args.output/'polish_rejection.json',dict(error=str(exc)))


if __name__=='__main__': main()
