"""Trim unused relay hover windows using continuously certified service intervals."""
from copy import deepcopy
import json
from pathlib import Path

from model import Scenario
from q3_certificate import accept
from q4_audit import audit, verify_fixed_q3
from q4_exact import solve as solve_q4
from q3_upgrade import dump_json

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'results_q3_q4_frontier/polished'


def trim(s,source):
    verify_fixed_q3(s,source)
    plan=deepcopy(source);changes=[]
    for r in plan['relay_sorties']:
        intervals=[x for x in source['communication_intervals'] if x.get('relay_sortie')==r['id']]
        if not intervals:raise AssertionError('Unused relay cannot be polished as an active task')
        first=min(x['start'] for x in intervals);last=max(x['end'] for x in intervals)
        if first < r['established']-1e-7 or last > r['service_end']+1e-7:
            raise AssertionError('Certificate exceeds original service window')
        previous=deepcopy(r)
        service=last-first
        trip=s.relay_sortie(r['lon'],r['lat'],r['agl'],service)
        launch=first-trip['prepare']-trip['out_time']-trip['establish']
        finish=launch+trip['duration']
        r.update(start=launch,established=first,service_end=last,return_time=finish,
                 relay_ready=finish+s.relay.turnaround,
                 component_ready=finish+s.charge_time(trip['return_soc'],s.relay_charge),
                 return_soc=trip['return_soc'],energy_kwh=trip['energy_kwh'])
        if (launch<previous['start']-1e-7 or
                any(r[key]>previous[key]+1e-7 for key in ('return_time','relay_ready','component_ready','energy_kwh'))):
            raise AssertionError('Window trimming expanded a resource interval or energy')
        changes.append(dict(relay=r['id'],launch_shift_seconds=launch-previous['start'],
                            service_reduction_seconds=previous['service_end']-previous['established']-service,
                            energy_saved_kwh=previous['energy_kwh']-r['energy_kwh']))
    obj=plan['objective']
    obj['relay_energy_kwh']=sum(r['energy_kwh'] for r in plan['relay_sorties'])
    obj['energy_kwh']=obj['transport_energy_kwh']+obj['relay_energy_kwh']
    obj['makespan']=max(r['return_time'] for r in plan['transport_sorties']+plan['relay_sorties'])
    plan['search']['continuous_window_polish']=dict(method='trim to saved continuously certified actual service endpoints',
        original_objective=source['objective'],changes=changes,
        bound_warning='Original finite-model stage values describe the unpolished integer plan, not this continuous-time candidate.')
    # First preserve and verify the exact saved relations, then issue a fresh certificate.
    verify_fixed_q3(s,plan)
    plan=accept(s,plan)
    for key,value in source['objective'].items():
        if plan['objective'][key]>value+1e-7:raise AssertionError('Polishing worsened an objective')
    return plan,changes


def main():
    s=Scenario();selection=json.loads((ROOT/'results_q3_q4_frontier/selection.json').read_text(encoding='utf8'))
    # Refuse recursive input so reruns compare the same pre-polish selections.
    sources=OUT/'configuration.json'
    if sources.exists():selected=json.loads(sources.read_text(encoding='utf8'))['sources']
    else:
        selected={name:row['label'] for name,row in selection.items() if name in ('primary','fastest','energy')}
        dump_json(sources,dict(sources=selected,method='continuous relay window trimming; no task or box changes'))
    for name,path in selected.items():
        source=json.loads((ROOT/path).read_text(encoding='utf8'))
        plan,changes=trim(s,source)
        label='polished_'+name;plan['q2_seed']=label
        q4=solve_q4(s,plan,json.dumps(plan,ensure_ascii=False).encode('utf8'));checked=audit(s,plan,q4)
        dump_json(OUT/'plans'/f'{label}.json',plan)
        dump_json(OUT/'q4'/f'{label}.json',q4)
        report=dict(label=label,status='PASS',source=path,objective=plan['objective'],changes=changes,
                    q4_validation=checked['status'])
        dump_json(OUT/'runs'/f'{label}.json',report)
        print(json.dumps(report,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
