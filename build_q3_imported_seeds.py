"""Box-conserving tail-route changes around the imported Q3 plan."""
from copy import deepcopy
from model import Scenario
from optimize_q3_imported import OUT,read
from q3_upgrade import dump_json


def main():
    s=Scenario();baseline=read(OUT/'baseline/q3_plan.json');cases=[]
    for bid in ('S007-HYG-01','S003-HYG-01','S003-FOD-01','S003-WAT-04'):
        plan=deepcopy(baseline);rows={r['id']:r for r in plan['transport_sorties']}
        for v in rows['T018']['visits']:
            if bid in v[1]:v[1].remove(bid)
        rows['T018']['visits']=[v for v in rows['T018']['visits'] if v[1]]
        rows['T018']['route']=s.route('C',rows['T018']['visits'])
        route=s.route('A',[[s.boxes[bid].zone,[bid]]])
        rows['TAIL_SPLIT']=dict(id='TAIL_SPLIT',model='A',route=route,visits=route['visits'],start=1700.)
        plan['transport_sorties']=list(rows.values())
        plan.update(seed_only=True,seed_changes=dict(split_from='T018',box=bid))
        assert sorted(b for r in rows.values() for _,bs in r['visits'] for b in bs)==sorted(s.boxes)
        name='split_'+bid.lower().replace('-','_')
        energy=sum(r['route']['energy_kwh'] for r in rows.values())
        cases.append(dict(name=name,transport_energy=energy,tail_duration=rows['T018']['route']['duration']))
        dump_json(OUT/'seeds'/(name+'.json'),plan)
    dump_json(OUT/'seeds/index.json',cases)
    for row in cases:print(row,flush=True)
    # Move S006's urgent medical box onto its early A sortie, exchanging the
    # nonurgent food box. Both routes retain their original box counts/types.
    plan=deepcopy(baseline);rows={r['id']:r for r in plan['transport_sorties']}
    first=rows['T009']['visits'][0][1];second=rows['T010_2']['visits'][0][1]
    first.remove('S006-FOD-01');first.append('S006-MED-01')
    second.remove('S006-MED-01');second.append('S006-FOD-01')
    for tid in ('T009','T010_2'):
        rows[tid]['route']=s.route(rows[tid]['model'],rows[tid]['visits'])
    assert sorted(b for r in rows.values() for _,bs in r['visits'] for b in bs)==sorted(s.boxes)
    plan.update(seed_only=True,seed_changes=dict(exchange=['S006-MED-01','S006-FOD-01'],tasks=['T009','T010_2']))
    dump_json(OUT/'seeds/early_s006_medical.json',plan)
    print('early_s006_medical',sum(r['route']['energy_kwh'] for r in rows.values()),flush=True)


if __name__=='__main__':main()
