"""Refine timing while protecting both selected Q4 resource vectors."""
import argparse,json
from search_q34_compromise import ROOT,DATA,Scenario,read,dump_json,save_candidate,solve_q4
from deliver_q34_compromise import choose
from optimize_q3_timing import optimize

def main():
    p=argparse.ArgumentParser();p.add_argument('--input',required=True)
    p.add_argument('--output',required=True);p.add_argument('--policy',choices=['timely','energy'],default='timely')
    p.add_argument('--makespan-cap',type=float,default=7451);p.add_argument('--delivery-cap',type=float,default=3000000)
    p.add_argument('--data-dir',default=str(DATA));a=p.parse_args()
    from pathlib import Path
    s=Scenario(Path(a.data_dir));source=read(a.input)
    before=solve_q4(s,source,json.dumps(source).encode());two=choose(before,2);three=choose(before,3)
    chains=[(two['id'],g['id'],k) for g in two['groups'] for k in g['resource_need']
            if k.endswith('_batteries') or (k.endswith('_aircraft') and k!='relay_aircraft')]
    plan,after,report=optimize(s,source,5.,policy=a.policy,makespan_cap=a.makespan_cap,
        delivery_cap=a.delivery_cap,preserve_partition=three['id'],preserve_q4_resources=chains)
    # Re-certification is allowed to change relay selection over overlaps;
    # accept only if the actual new Q4 still protects both vectors.
    for old in [two,three]:
        groups={frozenset(g['zones']) for g in old['groups']}
        now=next((p for p in after['partitions'] if {frozenset(g['zones']) for g in p['groups']}==groups),None)
        if now is None or any(now['resource_need'][k]>v for k,v in old['resource_need'].items()):
            raise ValueError('Selected two/three-group demand increased after timing refinement')
    target=ROOT/a.output;dump_json(target/'timing_report.json',report);save_candidate(s,plan,target)
    print(json.dumps(plan['objective'],ensure_ascii=False))

if __name__=='__main__':main()
