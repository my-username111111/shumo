"""Rebuild and independently check the Q3 energy/time trade-off archive.

The CP plans in ``results_q3_energy_upgrade/seeds`` are finite-search
incumbents. This program solves the exact continuous timing subproblem for
each fixed plan, then independently replays Q3 and all fixed-input Q4
partitions. It does not claim a global optimum for the joint problem.
"""
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


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / 'results_q3_energy_upgrade'
CASES = (
    ('balanced_26', 6500.0),
    ('fast_27', 6257.0),
    ('low_energy_26', 6900.0),
    ('minimum_energy_27', 7000.0),
)


def read(path: Path):
    return json.loads(path.read_text(encoding='utf8'))


def row(label: str, plan: dict, q4: dict | None, check: dict | None, path: Path):
    obj = plan['objective']
    p3 = next((p for p in q4['partitions'] if p['id'] == 'P3'), None) if q4 else None
    handoff = check['physical']['minimum_handoff_seconds'] if check else {}
    gaps = [v for v in handoff.values() if v is not None]
    return dict(
        plan=label,
        total_sorties=obj['total_sorties'],
        transport_sorties=obj['transport_sorties'],
        relay_sorties=obj['relay_sorties'],
        model_A=sum(x['model'] == 'A' for x in plan['transport_sorties']),
        model_B=sum(x['model'] == 'B' for x in plan['transport_sorties']),
        model_C=sum(x['model'] == 'C' for x in plan['transport_sorties']),
        energy_kwh=obj['energy_kwh'],
        transport_energy_kwh=obj['transport_energy_kwh'],
        relay_energy_kwh=obj['relay_energy_kwh'],
        weighted_delivery_seconds=obj['weighted_delivery_seconds'],
        makespan_seconds=obj['makespan'],
        weighted_soft_delay=obj['weighted_soft_delay'],
        extra_loss_db=plan['extra_loss_db'],
        min_resource_handoff_seconds=min(gaps) if gaps else None,
        p3_shortage=p3['shortage_total'] if p3 else None,
        q3_certificate=plan['certificate']['status'],
        q4_audit='PASS' if q4 else None,
        sha256=sha256(path.read_bytes()).hexdigest(),
    )


def main() -> None:
    s = Scenario()
    baseline_path = ROOT / 'results_q3_q4_resilience/selection/primary/q3_plan.json'
    prior_path = ROOT / 'results_q3_q4_timing/selection/buffer5_energy/q3_plan.json'
    rows = [row('previous_primary', read(baseline_path), None, None, baseline_path),
            row('previous_buffer5_energy', read(prior_path), None, None, prior_path)]
    for name, cap in CASES:
        source_path = OUTPUT / 'seeds' / f'{name}.json'
        source = read(source_path)
        plan, _, report = optimize(s, source, buffer=5., policy='energy',
                                   delivery_cap=None, makespan_cap=cap,
                                   allow_soft_delay=False)
        target = OUTPUT / 'selection' / name
        plan_path = target / 'q3_plan.json'
        dump_json(plan_path, plan)
        dump_json(target / 'timing_lp_report.json', report)
        q4, audit = export_q4(s, plan_path, target / 'q4', figures=False)
        check = verify_fixed_q3(s, plan)
        obj = plan['objective']
        assert audit['status'] == 'PASS'
        assert plan['certificate']['status'] == 'PASS'
        assert plan['extra_loss_db'] == 1.0
        assert obj['weighted_soft_delay'] == 0
        assert obj['transport_sorties'] == 23
        assert len(plan['deliveries']) == 80
        assert all(v is None or v >= 5. - 1e-6
                   for v in check['physical']['minimum_handoff_seconds'].values())
        rows.append(row(name, plan, q4, check, plan_path))

    comparison = OUTPUT / 'comparison.csv'
    with comparison.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    dump_json(OUTPUT / 'manifest.json', dict(
        status='PASS', input_fingerprint=data_signature(s),
        scope='finite CP incumbents + fixed-structure timing LP + independent Q3/Q4 replay',
        cases=[x[0] for x in CASES], rows=rows))
    print(json.dumps({k: {'energy_kwh': r['energy_kwh'],
                          'makespan_seconds': r['makespan_seconds'],
                          'total_sorties': r['total_sorties']}
                      for k, r in ((r['plan'], r) for r in rows)},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
