"""Freeze one Q3; independently replay and deliver both balanced Q4 variants."""
import argparse,csv,json,hashlib,shutil
from pathlib import Path
from search_q34_compromise import ROOT,DATA,read,save_candidate,Scenario,dump_json
from q4_delivery import export

def choose(q4,k,cv=.30):
    options=[p for p in q4['partitions'] if p['group_count']==k and p['workload_cv']<=cv]
    if not options:raise ValueError(f'No {k}-group partition with CV <= {cv}')
    return min(options,key=lambda p:(p['shortage_total'],p['workload_cv'],p['resource_total']))

def csv_out(path,rows):
    rows=list(rows)
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--input',type=Path,required=True)
    ap.add_argument('--name',default='primary');ap.add_argument('--data-dir',type=Path,default=DATA)
    args=ap.parse_args();s=Scenario(args.data_dir);out=ROOT/'results_q34_compromise/selection'/args.name
    plan,q4,summary=save_candidate(s,read(args.input),out)
    chosen={str(k):choose(q4,k) for k in (2,3)}
    dump_json(out/'balanced_partitions.json',dict(
        policy='For each K, require CV <= 0.30, then minimize typed shortage, CV and resource count.',
        interpretation='A transparent preference, not an added problem constraint. Both K use this exact Q3.',
        partitions=chosen))
    # Use the repository's complete delivery path too: all eight resource types,
    # all partitions, concrete inventory IDs, bottleneck chains and proof files.
    export(s,out/'q3_plan.json',out/'q4',figures=False)
    csv_out(out/'transport_sorties.csv',[
        {**{k:r[k] for k in ['id','model','drone','battery','start','return_time','battery_ready','energy_kwh','return_soc']},
         'visits':json.dumps(r['visits'],ensure_ascii=False)} for r in plan['transport_sorties']])
    csv_out(out/'relay_sorties.csv',[
        {k:r[k] for k in ['id','lon','lat','agl','relay','energy_component','start','established','service_end','return_time','relay_ready','component_ready','energy_kwh','return_soc']} for r in plan['relay_sorties']])
    csv_out(out/'box_deliveries.csv',[
        dict(box=b,zone=s.boxes[b].zone,time=d['time'],hard_deadline=s.boxes[b].hard_deadline,
             desired=s.boxes[b].desired,priority=s.boxes[b].priority) for b,d in sorted(plan['deliveries'].items())])
    csv_out(out/'communication_intervals.csv',[
        {k:r.get(k) for k in ['sortie','phase_index','start','end','mode','relay_sortie','margin_lower_db','status']} for r in plan['communication_intervals']])
    hard=min(s.boxes[b].hard_deadline-d['time'] for b,d in plan['deliveries'].items() if s.boxes[b].hard_deadline is not None)
    total=q4['total_work_seconds']
    compact={str(k):dict(partition=p['id'],cv=p['workload_cv'],shortage=p['shortage_total'],
        resource_total=p['resource_total'],need=p['resource_need'],deficit=p['shortage'],
        groups=[dict(id=g['id'],zones=g['zones'],boxes=g['boxes'],share=g['work_seconds']/total) for g in p['groups']])
        for k,p in [(k,chosen[str(k)]) for k in (2,3)]}
    result=dict(objective=plan['objective'],communication=plan['communication'],minimum_hard_margin_seconds=hard,
        minimum_handoff_seconds=plan['independent_replay']['minimum_handoff_seconds'],q4=compact,
        legal_partition_count=len(q4['partitions']),source_commit=read(ROOT/'source_commit.json')['sha'],
        source_candidate=str(args.input),source_sha256=hashlib.sha256(args.input.read_bytes()).hexdigest(),
        plan_sha256=hashlib.sha256((out/'q3_plan.json').read_bytes()).hexdigest(),
        code_sha256={f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in
            ['model.py','q3_joint.py','q3_certificate.py','q4_exact.py','q4_audit.py','search_q34_compromise.py']},
        optimality_scope='Q3: feasible finite-candidate search, not global optimum. Q4: exhaustive partitions and minimum interval resources for this frozen Q3.')
    dump_json(out/'delivery_summary.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)

if __name__=='__main__':main()
