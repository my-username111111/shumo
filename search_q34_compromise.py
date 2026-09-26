"""Reproducible Q3 quality-first experiments with exact, frozen-input Q4 audit.

All paths are explicit. Original problem data are read-only. Each candidate
is independently certified before its full two/three-group frontier is used.
"""
from pathlib import Path
from copy import deepcopy
import argparse, json, hashlib, time
from model import Scenario
from q3_joint import Geometry, site_pool, solve
from q3_certificate import accept
from q3_upgrade import dump_json
from q4_exact import solve as solve_q4
from q4_audit import audit, verify_fixed_q3

ROOT = Path(__file__).resolve().parent
DATA = Path(r'C:\Users\user\Desktop\建模\第二十三届中国研究生数学建模竞赛 - 中文题目\中文题目\D题\数据')
BASE = ROOT/'results_q3_q4_balance/selection/two_zero_shortage/q3_plan.json'

def read(p): return json.loads(Path(p).read_text(encoding='utf8'))

def summarize(plan, q4):
    result=dict(objective=plan['objective'],communication=plan['communication'],
                components=q4['components']['components'],partitions=[])
    for k in (2,3):
        rows=[p for p in q4['partitions'] if p['group_count']==k]
        for p in rows:
            shares=[g['work_seconds']/q4['total_work_seconds'] for g in p['groups']]
            row={key:p[key] for key in ('id','group_count','shortage_total','resource_total','workload_cv','resource_need','shortage')}
            row.update(shares=shares,zones=[g['zones'] for g in p['groups']])
            result['partitions'].append(row)
    return result

def save_candidate(s,plan,path):
    plan=accept(s,plan)
    dump_json(path/'q3_plan.json',plan)
    checked=verify_fixed_q3(s,plan)
    q4=solve_q4(s,plan,(path/'q3_plan.json').read_bytes())
    validation=audit(s,plan,q4)
    dump_json(path/'q4_results.json',q4)
    dump_json(path/'validation.json',dict(q3=checked,q4=validation))
    summary=summarize(plan,q4);dump_json(path/'summary.json',summary)
    return plan,q4,summary

def main():
    p=argparse.ArgumentParser();p.add_argument('--input',type=Path,default=BASE)
    p.add_argument('--name',required=True);p.add_argument('--groups',choices=['two','three','none'],default='two')
    p.add_argument('--shortage',type=int,default=4);p.add_argument('--soft',type=float,default=0)
    p.add_argument('--makespan',type=float,default=7600);p.add_argument('--energy',type=float,default=79)
    p.add_argument('--seconds',type=float,default=45);p.add_argument('--seed',type=int,default=2061)
    p.add_argument('--interval',type=float,default=12);p.add_argument('--fixed-models',action='store_true')
    p.add_argument('--policy',choices=['timely','energy','makespan'],default='timely')
    p.add_argument('--slots',type=int,default=2);p.add_argument('--data-dir',type=Path,default=DATA)
    p.add_argument('--groups-file',type=Path)
    a=p.parse_args();s=Scenario(a.data_dir);original=read(a.input)
    groups=([['S001','S002','S004','S006','S008','S009','S011'],['S003','S005','S007','S010','S012','S013','S014','S015']]
        if a.groups=='two' else [['S001','S006','S010','S011','S012','S013','S014'],['S002','S004','S008','S009'],['S003','S005','S007','S015']])
    if a.groups=='none':groups=None
    if a.groups_file:groups=read(a.groups_file)
    source=deepcopy(dict(sorties=original['transport_sorties'],relay_sorties=original['relay_sorties']))
    out=ROOT/'results_q34_compromise'/a.name
    config={k:str(v) if isinstance(v,Path) else v for k,v in vars(a).items()}
    config.update(source_sha256=hashlib.sha256(a.input.read_bytes()).hexdigest(),q4_groups=groups)
    dump_json(out/'configuration.json',config)
    print(json.dumps(dict(stage='prepare',case=a.name),ensure_ascii=False),flush=True)
    geometry=Geometry(s,site_pool(s,original),extra=1.,interval=a.interval)
    caps=dict(soft=a.soft,makespan=a.makespan,energy=a.energy*1e6)
    plan,report=solve(s,source,geometry,seconds=a.seconds,seed=a.seed,slots=a.slots,
        variable_models=not a.fixed_models,policy=a.policy,q4_groups=groups,
        resource_buffer=5.,objective_caps=caps,max_partition_shortage=a.shortage if groups else None)
    dump_json(out/'solver_report.json',report)
    if plan is None:
        print(json.dumps(dict(case=a.name,status='NO_ACCEPTED_PLAN',stages=report['stages'])),flush=True);return
    try:
        plan,q4,summary=save_candidate(s,plan,out)
        print(json.dumps(dict(case=a.name,status='PASS',objective=plan['objective'],
            two=min((r for r in summary['partitions'] if r['group_count']==2),key=lambda r:(r['workload_cv']>.2,r['shortage_total'],r['workload_cv'])),
            three=min((r for r in summary['partitions'] if r['group_count']==3),key=lambda r:(r['workload_cv']>.55,r['shortage_total'],r['workload_cv']))),ensure_ascii=False),flush=True)
    except (ValueError,AssertionError) as e:
        dump_json(out/'rejection.json',dict(error=str(e)));print('REJECTED',str(e),flush=True)

if __name__=='__main__':main()
