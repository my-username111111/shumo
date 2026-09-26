"""Explicit within-zone repacking seeds; each must still be fully scheduled."""
from copy import deepcopy
from pathlib import Path
import json
from model import Scenario
from q3_upgrade import dump_json
from optimize_q3_time_guarded import DEFAULT


def main():
    s=Scenario()
    baseline=json.loads(DEFAULT.read_text(encoding='utf8'))
    out=Path(__file__).resolve().parent/'results_q3_time_guarded/seeds'
    cases={
        'merge_s008':[('T013','T021')],
        'merge_s005':[('T016','T019_1')],
        'merge_both':[('T013','T021'),('T016','T019_1')],
    }
    for name,pairs in cases.items():
        plan=deepcopy(baseline)
        rows={r['id']:r for r in plan['transport_sorties']}
        for first,second in pairs:
            visits={}
            for zone,boxes in rows[first]['visits']+rows[second]['visits']:
                visits.setdefault(zone,[]).extend(boxes)
            route=s.route('C',list(visits.items()))
            rows[first].update(visits=route['visits'],route=route,model='C')
            del rows[second]
        plan['transport_sorties']=list(rows.values())
        assert sorted(b for r in rows.values() for _,bs in r['visits'] for b in bs)==sorted(s.boxes)
        plan.update(seed_only=True,seed_changes=dict(merges=pairs,source=str(DEFAULT)))
        dump_json(out/(name+'.json'),plan)
        print(name,len(rows),flush=True)
    for name,bid,donor,target in [
        ('move_s008_food','S008-FOD-01','T013','T003'),
        ('move_s008_hygiene','S008-HYG-01','T021','T003'),
        ('move_s005_hygiene','S005-HYG-01','T019_1','T010_2'),
        ('move_s013_food','S013-FOD-01','T002_2','T011'),
    ]:
        plan=deepcopy(baseline)
        rows={r['id']:r for r in plan['transport_sorties']}
        for _,boxes in rows[donor]['visits']:
            if bid in boxes: boxes.remove(bid)
        rows[target]['visits'].append([s.boxes[bid].zone,[bid]])
        for tid,typ in [(donor,'A'),(target,'C')]:
            route=s.route(typ,rows[tid]['visits'])
            rows[tid].update(route=route,model=typ)
        assert sorted(b for r in rows.values() for _,bs in r['visits'] for b in bs)==sorted(s.boxes)
        plan.update(seed_only=True,seed_changes=dict(box=bid,donor=donor,target=target,source=str(DEFAULT)))
        dump_json(out/(name+'.json'),plan)
        print(name,len(rows),flush=True)


if __name__=='__main__': main()
