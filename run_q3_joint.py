"""Reproducible Q3 experiment with acceptance gates and a nondominated archive."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
from math import ceil, exp
from pathlib import Path
from random import Random
from time import perf_counter

from model import Scenario, distance_m
from q2 import Dispatch, Job
from q2_alns import jobs_from_plan, _relocate, _exchange, _merge, _split, _reorder, _ruin_recreate, _insert_box
from q3_joint import Geometry, solve, site_pool, refine_pool, data_signature
from q3_certificate import accept, certify
from q3_upgrade import dump_json
from run_q3_upgrade import emit, write_csv, sha256, plan_candidates


ROOT=Path(__file__).resolve().parent
KEYS=('weighted_soft_delay','weighted_delivery_seconds','makespan','energy_kwh','total_sorties')
VECTOR=(*KEYS[:4],'transport_sorties','relay_sorties')


def key(plan): return tuple(plan['objective'][x] for x in KEYS)


def nondominated(plans):
    output=[]
    for plan in plans:
        values=tuple(plan['objective'][x] for x in VECTOR)
        if any(all(other['objective'][x]<=v+1e-8 for x,v in zip(VECTOR,values)) and
               any(other['objective'][x]<v-1e-8 for x,v in zip(VECTOR,values)) for other in plans): continue
        if not any(all(abs(old['objective'][x]-v)<1e-8 for x,v in zip(VECTOR,values)) for old in output): output.append(plan)
    return output


def repack(jobs,rng,dispatcher,size):
    """Bounded k->k-1 box redistribution; exact partition checked by the caller."""
    if len(jobs)<size: return None
    selected=rng.sample(range(len(jobs)),size)
    source=min(selected,key=lambda i:len(jobs[i].box_ids))
    targets=[i for i in selected if i!=source];result=list(jobs)
    for bid in sorted(jobs[source].box_ids,key=lambda b:-dispatcher.s.boxes[b].kg):
        options=[]
        for index in targets:
            for position in range(len(result[index].visits)+1):
                candidate=_insert_box(result[index],bid,dispatcher.s.boxes[bid].zone,position)
                energies=[r['energy_kwh'] for g in dispatcher.s.transport if (r:=dispatcher.evaluate(candidate,g)) is not None]
                if energies: options.append((min(energies),index,candidate))
        if not options:return None
        _,index,candidate=min(options,key=lambda x:x[0]);result[index]=candidate
    return [job for i,job in enumerate(result) if i!=source]


def source_from_jobs(s,jobs):
    rows=[]
    for i,job in enumerate(jobs):
        choices=[]
        for g in s.transport:
            try: choices.append(s.route(g,job.visits))
            except ValueError: pass
        if not choices: return None
        route=min(choices,key=lambda r:r['energy_kwh'])
        rows.append(dict(id=f'T{i+1:03d}',job=job.id,model=route['model'],visits=route['visits']))
    if sorted(b for r in rows for z,bs in r['visits'] for b in bs)!=sorted(s.boxes):
        raise AssertionError('Route operator broke exact box partition')
    return dict(sorties=rows)


def lower_bounds(s):
    nt=max(ceil(sum(b.kg for b in s.boxes.values())/max(t.max_kg for t in s.transport.values())),
           ceil(sum(b.volume for b in s.boxes.values())/max(t.max_volume for t in s.transport.values())))
    weighted=completion=0.
    home=s.nodes['O01']
    for box in s.boxes.values():
        node=s.nodes[box.zone];distance=distance_m((home.lon,home.lat),(node.lon,node.lat))
        types=[t for t in s.transport.values() if t.max_kg>=box.kg and t.max_volume>=box.volume]
        earliest=min(t.prepare+t.load_each+t.handoff+t.handoff_each+distance/t.speed for t in types)
        ret=min(t.prepare+t.load_each+t.handoff+t.handoff_each+2*distance/t.speed for t in types)
        weighted+=box.priority*earliest;completion=max(completion,ret)
    return dict(transport_sorties=nt,weighted_delivery_seconds=weighted,makespan=completion,
        weighted_soft_delay=0.,energy_kwh=0.,scope='Original problem relaxation: capacity totals and individual-box great-circle flight lower bounds; no DEM climbs, queuing, communication, or shared resources.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'results_q3_complete')
    parser.add_argument('--warm-start',type=Path,help='Explicit independently rechecked Q3 incumbent')
    parser.add_argument('--data',type=Path)
    parser.add_argument('--seconds',type=float,default=20.,help='CP-SAT budget per lexicographic stage')
    parser.add_argument('--iterations',type=int,default=12)
    parser.add_argument('--seeds',type=int,nargs='+',default=[2026,2027])
    parser.add_argument('--skip-robust',action='store_true')
    parser.add_argument('--skip-refine',action='store_true')
    args=parser.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=True)
    s=Scenario(args.data) if args.data else Scenario();started=perf_counter()
    baseline=json.loads((ROOT/'results_q3_upgrade/q3_plan.json').read_text(encoding='utf-8'))
    archive=[];runs=[];trajectory=[]
    def store(plan,label):
        plan['q2_seed']=label
        accept(s,plan,plan.get('extra_loss_db',0.))
        dump_json(out/'plans'/(label+'.json'),plan)
        archive.append(plan)
        trajectory.append(dict(label=label,**plan['objective']))
        print(json.dumps(dict(stage='accepted',label=label,objective=plan['objective']),ensure_ascii=False),flush=True)
        return plan
    def attempt(source,geometry,label,**kwargs):
        try:
            plan,report=solve(s,source,geometry,seconds=args.seconds,**kwargs)
            report['label']=label;runs.append(report)
            if plan:return store(plan,label)
        except (ValueError,AssertionError) as exc:
            if runs and runs[-1].get('label')==label:
                runs[-1].update(status='REJECTED',error=str(exc))
            else:
                runs.append(dict(label=label,status='REJECTED',error=str(exc)))
            print(json.dumps(runs[-1]),flush=True)
        return None
    # Recheck the old numerical plan under the stronger acceptance gate.
    try: store(copy.deepcopy(baseline),'baseline_recertified')
    except (ValueError,AssertionError) as exc:runs.append(dict(label='baseline',status='REJECTED',error=str(exc)))
    if args.warm_start:
        store(json.loads(args.warm_start.read_text(encoding='utf-8')),'warm_start')
    base_sites=site_pool(s,baseline);geometry=Geometry(s,base_sites)
    sources=[('baseline_routes',dict(sorties=baseline['transport_sorties'],relay_sorties=baseline['relay_sorties']))]
    for label,path in plan_candidates(ROOT):
        if path.exists():sources.append((label,json.loads(path.read_text(encoding='utf-8'))))
    for label,source in sources:
        for seed in args.seeds:
            attempt(source,geometry,f'{label}_{seed}',seed=seed,variable_models=False)
    if not archive: raise SystemExit('No independently certified Q3 plan')
    best=min(archive,key=key);refinement={}
    if not args.skip_refine:
        refined_sites,refinement=refine_pool(s,best)
        # Preserve the initial nine sites; they can support different Q2 routes.
        unique={(x.lon,x.lat,x.agl):x for x in base_sites+refined_sites}
        geometry=Geometry(s,list(unique.values()))
        attempt(dict(sorties=best['transport_sorties'],relay_sorties=best['relay_sorties']),geometry,'refined_timely',seed=args.seeds[0])
        attempt(dict(sorties=best['transport_sorties'],relay_sorties=best['relay_sorties']),geometry,'refined_energy',seed=args.seeds[0],policy='energy')
        best=min(archive,key=key)
    dispatcher=Dispatch(s);rng=Random(args.seeds[0]);current=best
    operators={'relocate':_relocate,'exchange':_exchange,'merge':_merge,'split':_split,
        'reorder':_reorder,'ruin_recreate':_ruin_recreate,
        'three_to_two':lambda j,r,d:repack(j,r,d,3),
        'four_to_three':lambda j,r,d:repack(j,r,d,4)}
    weights={name:1. for name in operators};stats={name:dict(proposed=0,formed=0,feasible=0,accepted=0) for name in operators}
    for iteration in range(args.iterations):
        names=list(operators)
        name=names[iteration] if iteration<len(names) else rng.choices(names,weights=[weights[n] for n in names])[0]
        jobs=jobs_from_plan(dict(sorties=current['transport_sorties']),Job)
        stats[name]['proposed']+=1
        # Reattempt operator construction, but never pass unchecked partitions.
        proposed=None
        for _ in range(15):
            proposed=operators[name](jobs,rng,dispatcher)
            if proposed is not None:break
        if proposed is None:continue
        source=source_from_jobs(s,proposed)
        if source is None:continue
        stats[name]['formed']+=1
        candidate=attempt(source,geometry,f'alns_{iteration:03d}_{name}',seed=args.seeds[0]+iteration)
        if candidate is None:weights[name]=max(.1,weights[name]*.8);continue
        stats[name]['feasible']+=1
        improvement=key(candidate)<key(current)
        # SA only among zero-soft-delay solutions, with normalized W increase.
        anneal=(candidate['objective']['weighted_soft_delay']<=current['objective']['weighted_soft_delay']+1e-8 and
            rng.random()<exp(-max(0.,candidate['objective']['weighted_delivery_seconds']-current['objective']['weighted_delivery_seconds'])/
                             max(1.,20000*(1-iteration/max(1,args.iterations)))))
        if improvement or anneal:
            current=candidate;stats[name]['accepted']+=1
        weights[name]=.8*weights[name]+.2*(5 if improvement else 1)
    best=min(archive,key=key)
    # Q4 compatibility is a separate branch with the original three blocks.
    groups=baseline['q4_compatibility']['coupled_components']
    compatible=attempt(dict(sorties=baseline['transport_sorties'],relay_sorties=baseline['relay_sorties']),geometry,'q4_compatible',
        seed=args.seeds[0],q4_groups=groups,variable_models=False)
    nominal=[p for p in archive if p.get('extra_loss_db',0.)==0.]
    best=min(nominal,key=key)
    robust=[]
    if not args.skip_robust:
        for extra in (0.,1.,2.,3.):
            _,comm,cert=certify(s,best,extra,stop_on_failure=True)
            row=dict(extra_loss_db=extra,fixed_plan_status=cert['status'],
                fixed_unknown_seconds=comm['unknown_seconds'],reoptimization_status='NOT_RUN',
                reoptimized_energy_kwh=None,reoptimized_makespan=None,reoptimized_soft_delay=None)
            if extra:
                # Recompute direct gaps and both hops under the changed loss;
                # do not subtract the old certificate's minimum and call FAIL.
                sites,_=refine_pool(s,best,extra)
                pool={(x.lon,x.lat,x.agl):x for x in geometry.sites+sites}
                rg=Geometry(s,list(pool.values()),extra)
                rp=attempt(dict(sorties=best['transport_sorties'],relay_sorties=best['relay_sorties']),rg,f'robust_{int(extra)}db',seed=args.seeds[0],variable_models=False)
                row['reoptimization_status']='PASS' if rp else 'NO_CERTIFIED_SOLUTION_WITHIN_BUDGET'
                if rp:row.update(reoptimized_energy_kwh=rp['objective']['energy_kwh'],
                    reoptimized_makespan=rp['objective']['makespan'],reoptimized_soft_delay=rp['objective']['weighted_soft_delay'])
            robust.append(row)
    emit(best,out)
    dump_json(out/'q3_independent_replay.json',best['independent_replay'])
    bounds=lower_bounds(s)
    dump_json(out/'q3_bound_report.json',dict(status='FEASIBLE',upper_bound=best['objective'],
        original_problem_lower_bounds=bounds,finite_model_reports=runs,
        warning='No original-problem global optimality claim. Finite-model bounds and conditional stage gaps are not original-problem optimality gaps.'))
    write_csv(out/'q3_pareto.csv',[dict(label=p['q2_seed'],**p['objective']) for p in nondominated(nominal)])
    write_csv(out/'q3_all_candidates.csv',trajectory)
    write_csv(out/'q3_robustness.csv',robust)
    dump_json(out/'q3_search_history.json',dict(runs=runs,operators=stats,final_weights=weights,refinement=refinement))
    dump_json(out/'q3_q4_compatibility.json',dict(selected=best['q4_compatibility'],
        compatible_label=compatible['q2_seed'] if compatible else None,
        compatible_objective=compatible['objective'] if compatible else None,
        delta_from_selected={k:compatible['objective'][k]-best['objective'][k] for k in KEYS} if compatible else None,
        compatible_components=compatible['q4_compatibility'] if compatible else None))
    dump_json(out/'manifest.json',dict(selected=best['q2_seed'],policy=list(KEYS),args=vars(args)|{'output':str(out),'data':str(s.base),'warm_start':str(args.warm_start) if args.warm_start else None},
        input_fingerprint=data_signature(s),elapsed_seconds=perf_counter()-started,
        sources={str(path.relative_to(ROOT)):sha256(path) for _,path in plan_candidates(ROOT) if path.exists()},
        code={p.name:sha256(p) for p in (ROOT/'q3_joint.py',ROOT/'q3_certificate.py',ROOT/'run_q3_joint.py',ROOT/'q3_upgrade.py')},
        files={str(p.relative_to(out)):sha256(p) for p in sorted(out.rglob('*')) if p.is_file() and p.name!='manifest.json'}))
    print(json.dumps(dict(selected=best['q2_seed'],objective=best['objective'],output=str(out)),ensure_ascii=False))


if __name__=='__main__':main()
