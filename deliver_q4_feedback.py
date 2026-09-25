"""Rebuild Q3 plans selected for lower Q4 resource demand and audit Q4."""
from __future__ import annotations

import csv
from hashlib import sha256
import json
from pathlib import Path

from model import Scenario
from optimize_q3_timing import optimize
from q3_joint import data_signature
from q3_upgrade import dump_json
from q4_audit import verify_fixed_q3
from q4_delivery import export as export_q4


ROOT=Path(__file__).resolve().parent
OUTPUT=ROOT/'results_q4_feedback'
CASES=(
    dict(name='resource_first_26', delivery_cap=2700000., makespan_cap=6600.,
         preserve=[('P3','G1','B_batteries'),('P4','G2','B_batteries')]),
    dict(name='lower_energy_26', delivery_cap=2750000., makespan_cap=6600.,
         preserve=[('P3','G1','B_batteries'),('P4','G2','B_batteries')]),
    dict(name='three_group_27', delivery_cap=None, makespan_cap=7000.,
         preserve=[('P4','G2','A_batteries')]),
)


def read(path):
    return json.loads(path.read_text(encoding='utf8'))


def record(name,plan,q4,path,physical=None):
    p3=next(p for p in q4['partitions'] if p['id']=='P3')
    p4=next(p for p in q4['partitions'] if p['id']=='P4')
    obj=plan['objective']
    gaps=[] if physical is None else [v for v in physical['minimum_handoff_seconds'].values()
                                      if v is not None]
    return dict(plan=name,total_sorties=obj['total_sorties'],
        energy_kwh=obj['energy_kwh'],weighted_delivery_seconds=obj['weighted_delivery_seconds'],
        makespan_seconds=obj['makespan'],weighted_soft_delay=obj['weighted_soft_delay'],
        extra_loss_db=plan['extra_loss_db'],
        min_handoff_seconds=min(gaps) if gaps else None,
        q4_two_group_recommended=q4['recommended_partition'],
        p3_aircraft=sum(p3['resource_need'][f'{g}_aircraft'] for g in 'ABC'),
        p3_batteries=sum(p3['resource_need'][f'{g}_batteries'] for g in 'ABC'),
        p3_shortage=p3['shortage_total'],p3_workload_cv=p3['workload_cv'],
        p4_aircraft=sum(p4['resource_need'][f'{g}_aircraft'] for g in 'ABC'),
        p4_batteries=sum(p4['resource_need'][f'{g}_batteries'] for g in 'ABC'),
        p4_shortage=p4['shortage_total'],p4_workload_cv=p4['workload_cv'],
        q3_certificate=plan['certificate']['status'],
        sha256=sha256(path.read_bytes()).hexdigest())


def main():
    s=Scenario()
    old=[]
    for name in ('low_energy_26','minimum_energy_27'):
        root=ROOT/'results_q3_energy_upgrade/selection'/name
        path=root/'q3_plan.json'
        old.append(record('previous_'+name,read(path),read(root/'q4/q4_results.json'),path))
    rows=list(old)
    for case in CASES:
        name=case['name']
        source=read(OUTPUT/'seeds'/f'{name}.json')
        plan,_,report=optimize(s,source,buffer=5.,policy='energy',
            delivery_cap=case['delivery_cap'],makespan_cap=case['makespan_cap'],
            preserve_q4_resources=case['preserve'])
        target=OUTPUT/'selection'/name
        plan_path=target/'q3_plan.json'
        dump_json(plan_path,plan)
        dump_json(target/'timing_lp_report.json',report)
        q4,audit=export_q4(s,plan_path,target/'q4',figures=False)
        physical=verify_fixed_q3(s,plan)['physical']
        p3=next(p for p in q4['partitions'] if p['id']=='P3')
        p4=next(p for p in q4['partitions'] if p['id']=='P4')
        assert audit['status']=='PASS' and plan['certificate']['status']=='PASS'
        assert len(plan['deliveries'])==80 and plan['extra_loss_db']==1.
        assert plan['objective']['weighted_soft_delay']==0
        assert plan['communication']['outage_seconds']==0
        assert all(v is None or v>=5.-1e-6 for v in physical['minimum_handoff_seconds'].values())
        assert p4['shortage_total']<=7
        assert sum(p4['resource_need'][f'{g}_batteries'] for g in 'ABC')<=17
        if name.endswith('_26'):
            assert p3['shortage_total']<=1 and plan['objective']['total_sorties']==26
        else:
            assert plan['objective']['total_sorties']==27
        rows.append(record(name,plan,q4,plan_path,physical))
    with (OUTPUT/'comparison.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]))
        writer.writeheader();writer.writerows(rows)
    dump_json(OUTPUT/'manifest.json',dict(status='PASS',
        input_fingerprint=data_signature(s),
        scope='finite Q3 search, Q4 resource-chain preserving LP, independent Q3/Q4 audit',
        rows=rows))
    print(json.dumps({r['plan']:dict(energy_kwh=r['energy_kwh'],
                                    p3_shortage=r['p3_shortage'],
                                    p4_batteries=r['p4_batteries'],
                                    p4_shortage=r['p4_shortage']) for r in rows},
                     ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
