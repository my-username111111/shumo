"""Rebuild the audited Q3 retiming / fixed-Q3 Q4 comparison archive."""

from __future__ import annotations

import csv
from hashlib import sha256
import json
from pathlib import Path
from shutil import copyfile

from model import Scenario
from optimize_q3_timing import optimize
from q4_audit import verify_fixed_q3
from q4_delivery import export as export_q4
from q4_exact import load_and_solve
from q3_upgrade import dump_json


ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'results_q3_q4_timing'
SOURCES = {
    'previous_primary': ROOT / 'results_q3_q4_resilience/selection/primary/q3_plan.json',
    'previous_buffer30': ROOT / 'results_q3_q4_resilience/selection/radio1_buffer30_coarse/q3_plan.json',
    'previous_stock': ROOT / 'results_q3_q4_refined/rebatch_stock/plans/independent_two_variable.json',
}
SEED = OUT / 'seeds/buffer5_cp.json'
RAW_SEED = ROOT / 'results_q3_q4_next/buffer5/plans/timely_2030.json'


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf8'))


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def metrics(s: Scenario, label: str, path: Path) -> dict:
    plan = read(path)
    physical = verify_fixed_q3(s, plan)['physical']
    q4 = load_and_solve(s, path)
    best = next(p for p in q4['partitions'] if p['id'] == q4['recommended_partition'])
    hard_margin = min(s.boxes[b].hard_deadline - row['time']
        for b, row in plan['deliveries'].items() if s.boxes[b].hard_deadline is not None)
    handoffs = physical['minimum_handoff_seconds']
    return dict(scheme=label, source=str(path.relative_to(ROOT)), sha256=digest(path),
                extra_loss_db=plan.get('extra_loss_db', 0.),
                required_buffer_seconds=plan['search']['resource_buffer_seconds'],
                **plan['objective'], hard_deadline_min_margin_seconds=hard_margin,
                communication_margin_lower_db=plan['communication']['minimum_certified_margin_db'],
                aircraft_min_handoff_seconds=handoffs['aircraft'],
                battery_min_handoff_seconds=handoffs['battery'],
                relay_min_handoff_seconds=handoffs['relay'],
                q4_recommended=best['id'],q4_two_group_shortage=best['shortage_total'],
                q4_two_group_resources=best['resource_total'],
                q4_two_group_cv=best['workload_cv'],
                q4_three_group_shortage=next(p['shortage_total'] for p in q4['partitions']
                                           if p['group_count'] == 3),
                status='PASS')


def main() -> None:
    s = Scenario()
    OUT.mkdir(parents=True, exist_ok=True)
    SEED.parent.mkdir(parents=True, exist_ok=True)
    if not SEED.exists():
        if not RAW_SEED.exists():
            raise FileNotFoundError('Run the documented 5-second CP search first')
        copyfile(RAW_SEED, SEED)
        for name, source in (
            ('configuration.json',ROOT/'results_q3_q4_next/buffer5/configuration.json'),
            ('run.json',ROOT/'results_q3_q4_next/buffer5/runs/timely_2030.json')):
            copyfile(source,SEED.parent/name)
    original = {key: read(path) for key,path in SOURCES.items()}
    seed = read(SEED)
    configurations = [
        ('nominal_0p2',original['previous_primary'],dict(buffer=.2)),
        ('buffer5_timely',original['previous_primary'],dict(buffer=5.)),
        ('buffer5_energy',seed,dict(buffer=5.,policy='energy',makespan_cap=6318.)),
        ('buffer30_retimed',original['previous_primary'],dict(buffer=30.1)),
        ('stock_zero_shortage',original['previous_stock'],dict(buffer=5.,allow_soft_delay=True)),
    ]
    rows = [metrics(s,label,path) for label,path in SOURCES.items()]
    for label,source,kwargs in configurations:
        plan,q4,report = optimize(s,source,**kwargs)
        directory = OUT/'selection'/label
        directory.mkdir(parents=True,exist_ok=True)
        plan_path = directory/'q3_plan.json'
        dump_json(plan_path,plan)
        dump_json(directory/'timing_lp_report.json',report)
        result,validation = export_q4(s,plan_path,directory/'q4',figures=False)
        if validation['status'] != 'PASS' or result['recommended_partition'] != q4['recommended_partition']:
            raise AssertionError('Exported Q4 result disagrees with independent timing replay')
        rows.append(metrics(s,label,plan_path))
    fields = list(rows[0])
    with (OUT/'comparison.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields)
        writer.writeheader();writer.writerows(rows)
    dump_json(OUT/'manifest.json',dict(status='PASS',
        policy='Q3 fixed-structure LP, full Q3/Q4 independent recertification; no global joint optimum claim',
        source_hashes={k:digest(p) for k,p in SOURCES.items()},
        seed_sha256=digest(SEED),
        code_hashes={name:digest(ROOT/name) for name in
                     ('optimize_q3_timing.py','deliver_q3_q4_timing.py','q3_certificate.py',
                      'q4_exact.py','q4_audit.py')},
        generated={row['scheme']:row['sha256'] for row in rows if row['scheme'] not in SOURCES},
        compared_schemes=[row['scheme'] for row in rows]))
    print(json.dumps(dict(status='PASS',plans=len(configurations),comparison=str(OUT/'comparison.csv')),
                     ensure_ascii=False),flush=True)


if __name__ == '__main__':
    main()
