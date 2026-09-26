"""Generate explicit box-conserving within-region candidates, not valid schedules."""
from copy import deepcopy
from itertools import permutations
from search_q34_compromise import ROOT, DATA, BASE, read, dump_json, Scenario

def validate_and_write(s, original, rows, name, changes):
    expected=sorted(s.boxes)
    actual=sorted(b for r in rows for _,bs in r['visits'] for b in bs)
    if expected!=actual:raise AssertionError('Boxes missing, duplicated or invented')
    for row in rows:
        options=[]
        for typ in s.transport:
            try:options.append(s.route(typ,row['visits']))
            except ValueError:pass
        if not options:raise ValueError('No physically feasible model: '+row['id'])
        # This model and previous starts are hints only. CP rebuilds every field.
        route=min(options,key=lambda r:r['energy_kwh'])
        row['model']=route['model'];row['route']=route
    result=deepcopy(original);result['transport_sorties']=rows
    result['seed_only']=True;result['seed_changes']=changes
    dump_json(ROOT/'results_q34_compromise/seeds'/(name+'.json'),result)

def main():
    s=Scenario(DATA);original=read(BASE)
    for name,pairs in [
        ('compact24',[('T019_1','T023'),('T004','T014_2')]),
        ('compact23',[('T019_1','T023'),('T014_2','T019_2'),('T002_1','T010_1')]),
        ('compact22',[('T019_1','T023'),('T014_2','T019_2'),('T002_1','T010_1'),('T009','T010_2')])]:
        rows={r['id']:deepcopy(r) for r in original['transport_sorties']}
        for a,b in pairs:
            visits={}
            for z,bs in rows[a]['visits']+rows[b]['visits']:visits.setdefault(z,[]).extend(bs)
            rows[a]['visits']=list(visits.items());rows.pop(b)
        for z,bs in rows['T017']['visits']:
            target='T002_2' if z=='S013' else 'T011'
            for zz,boxes in rows[target]['visits']:
                if zz==z:boxes.extend(bs)
        rows.pop('T017')
        validate_and_write(s,original,list(rows.values()),name,
            dict(merges=pairs,redistributed_food_task='T017',source=str(BASE.relative_to(ROOT))))
    q2=read(ROOT/'results_q2_time/selection/balanced.json');rows=[]
    for old in q2['sorties']:
        if old['id']!='T014':rows.append(deepcopy(old));continue
        for i,visit in enumerate(old['visits']):
            row=deepcopy(old);row['id']+='_'+str(i);row['visits']=[visit];rows.append(row)
    validate_and_write(s,original,rows,'q2regional25',dict(split='T014',source='Q2 balanced'))
    out=ROOT/'results_q34_compromise/seeds'
    dump_json(out/'q2groups2.json',[
        ['S001','S002','S004','S008','S009','S011'],
        ['S003','S005','S006','S007','S010','S012','S013','S014','S015']])
    dump_json(out/'q2groups3.json',[
        ['S001','S010','S011','S012','S013','S014'],
        ['S002','S004','S008','S009'],['S003','S005','S006','S007','S015']])
    # Second-stage improvement discovered by examining the three delayed
    # S001 water boxes. Free an early C aircraft by repacking S007; retain
    # its low-priority hygiene box in the western regional C sortie.
    incumbent=ROOT/'results_q34_compromise/compact23_deep/q3_plan.json'
    if incumbent.exists():
        base=read(incumbent);rows=[]
        for old in deepcopy(base['transport_sorties']):
            if old['id']=='T018':
                visits=old['visits']+[['S007',['S007-HYG-01']]]
                feasible=[]
                for sequence in permutations(visits):
                    try:feasible.append(s.route('C',list(sequence)))
                    except ValueError:pass
                old['visits']=min(feasible,key=lambda t:t['energy_kwh'])['visits']
            if old['id']!='T002_1':rows.append(old);continue
            for tid,boxes in [('T007_HARD',['S007-MED-01','S007-WAT-01']),
                              ('T007_SOFT',['S007-FOD-01','S007-WAT-02'])]:
                row=deepcopy(old);row.update(id=tid,model='A',visits=[['S007',boxes]])
                rows.append(row)
        validate_and_write(s,base,rows,'repack007_A24',dict(
            split='T002_1',transfer={'box':'S007-HYG-01','target':'T018'},
            objective='free early C aircraft for time-critical water deliveries'))

if __name__=='__main__':main()
