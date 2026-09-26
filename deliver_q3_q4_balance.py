"""Reproduce certified shortage/balance tradeoffs from saved CP incumbents."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path

from model import Scenario
from optimize_q3_timing import optimize
from q3_upgrade import dump_json
from q3_certificate import accept
from q4_delivery import export, write_csv
from q4_audit import verify_fixed_q3
from q4_exact import solve as solve_q4

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'results_q3_q4_balance'


def read(path):
    return json.loads(path.read_text(encoding='utf8'))


def select(q4,k,cv_limit=.2):
    candidates=[p for p in q4['partitions'] if p['group_count']==k and p['workload_cv']<=cv_limit]
    if not candidates:
        raise ValueError(f'No {k}-group partition under CV {cv_limit}')
    return min(candidates,key=lambda p:(p['shortage_total'],p['workload_cv'],p['resource_total']))


def metrics(s,label,path,plan,p):
    checked=verify_fixed_q3(s,plan)
    hard=[s.boxes[b].hard_deadline-d['time'] for b,d in plan['deliveries'].items()
          if s.boxes[b].hard_deadline is not None]
    gaps=[v for v in checked['physical']['minimum_handoff_seconds'].values() if v is not None]
    return dict(case=label,group_count=p['group_count'],partition=p['id'],
        shortage=p['shortage_total'],workload_cv=p['workload_cv'],resource_total=p['resource_total'],
        **plan['objective'],hard_violations=sum(x < -1e-7 for x in hard),
        minimum_hard_margin_seconds=min(hard),minimum_handoff_seconds=min(gaps),
        communication_extra_loss_db=plan['extra_loss_db'],
        communication_unknown_seconds=plan['communication']['unknown_seconds'],
        communication_outage_seconds=plan['communication']['outage_seconds'],
        delivered_boxes=len(plan['deliveries']),q3_validation=checked['status'],
        plan_path=str(path.relative_to(ROOT)).replace('\\','/'),sha256=sha256(path.read_bytes()).hexdigest())


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',required=True)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--groups',type=int,choices=[2,3],required=True)
    parser.add_argument('--cv-limit',type=float,default=.2)
    args=parser.parse_args();s=Scenario();source=accept(s,read(args.input))
    q4=solve_q4(s,source,json.dumps(source,ensure_ascii=False).encode('utf8'))
    chosen=select(q4,args.groups,args.cv_limit)
    plan,q4,report=optimize(s,source,buffer=5.,allow_soft_delay=True,
                            preserve_partition=chosen['id'])
    chosen=select(q4,args.groups,args.cv_limit)
    target=OUT/'selection'/args.case
    path=target/'q3_plan.json'
    dump_json(path,plan);dump_json(target/'timing_lp_report.json',report)
    q4,validation=export(s,path,target/'q4',figures=False)
    chosen=select(q4,args.groups,args.cv_limit)
    dump_json(target/'balanced_selection.json',dict(
        policy='Among partitions with CV <= threshold, minimize shortage then CV then total resources',
        maximum_cv=args.cv_limit,selected_partition=chosen,
        legacy_resource_priority_partition=q4['recommended_partition'],
        q4_validation=validation['status']))
    # Both Q4 alternatives must be available for the SAME frozen Q3. For the
    # zero-shortage primary, keep a practical three-group compromise alongside
    # the separate, more balanced Q3 redesign for three groups.
    if args.case=='two_zero_shortage':
        options=[p for p in q4['partitions'] if p['group_count']==3 and p['shortage_total']<=6]
        same_three=min(options,key=lambda p:(p['workload_cv'],p['shortage_total'],p['resource_total']))
        dump_json(target/'same_q3_three_groups.json',same_three)
        dump_json(target/'same_q3_metrics.json',metrics(s,'same_q3_three_groups',path,plan,same_three))
    row=metrics(s,args.case,path,plan,chosen)
    dump_json(target/'metrics.json',row)
    rows=[]
    baseline_path=ROOT/'results_q3_energy_upgrade/selection/low_energy_26/q3_plan.json'
    baseline=accept(s,read(baseline_path))
    baseline_path=OUT/'baseline_recertified/q3_plan.json'
    dump_json(baseline_path,baseline)
    baseq=solve_q4(s,baseline,baseline_path.read_bytes())
    for k in (2,3):
        original=min((p for p in baseq['partitions'] if p['group_count']==k),
                     key=lambda p:(p['shortage_total'],p['resource_total'],p['workload_cv']))
        rows.append(metrics(s,f'baseline_{k}',baseline_path,baseline,original))
    rows.extend(read(p) for p in sorted((OUT/'selection').glob('*/metrics.json')))
    same=OUT/'selection/two_zero_shortage/same_q3_metrics.json'
    if same.exists():rows.append(read(same))
    write_csv(OUT/'comparison.csv',rows,list(rows[0]))
    dump_json(OUT/'comparison.json',rows)
    print(json.dumps(row,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
