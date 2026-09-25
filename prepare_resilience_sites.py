"""Adaptive finite relay-site expansion for a declared extra-loss scenario."""
import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path

from model import Scenario
from q3_joint import Geometry, site_pool
from q3_upgrade import _relay_site, interval_margin, dump_json


def expand(s, plan, extra=1., interval=6., limit=12):
    sites=site_pool(s,plan)
    geometry=Geometry(s,sites,extra=extra,interval=interval)
    missing={}
    for t in plan['transport_sorties']:
        for d in geometry.template(s.route(t['model'],t['visits']))['demands']:
            if not d['sites']:
                missing[tuple(d['p0'])+tuple(d['p1'])]=d
    home=s.nodes['O01'];gateway=(home.lon,home.lat,home.ground+s.gateway_agl)
    report=dict(extra_loss_db=extra,interval_seconds=interval,initial_sites=len(sites),
                initial_uncovered_unique_intervals=len(missing),steps=[],
                scope='Adaptive finite candidate heuristic; failure is not a global impossibility proof')
    while missing and len(report['steps'])<limit:
        d=next(iter(missing.values()));p0,p1=d['p0'],d['p1']
        x=(p0[0]+p1[0])/2;y=(p0[1]+p1[1])/2
        coords=set()
        # Include the access endpoint, gateway-to-endpoint corridor, and nearby high sites.
        for fraction in (.35,.55,.75,1.):
            cx=home.lon+(x-home.lon)*fraction;cy=home.lat+(y-home.lat)*fraction
            for dx,dy in ((0,0),(-.005,0),(.005,0),(0,-.005),(0,.005),
                          (-.005,-.005),(.005,.005),(-.005,.005),(.005,-.005)):
                coords.add((cx+dx,cy+dy,300.))
        choices=[]
        for lon,lat,agl in sorted(coords):
            site=_relay_site(s,lon,lat,agl,gateway)
            if site is None or site.gateway_margin<extra:continue
            access,_=interval_margin(s,site.point,p0,p1,'relay_access','transport',extra)
            value=min(site.gateway_margin-extra,access)
            if value>=0:choices.append((value,-site.travel_energy_kwh,site))
        if not choices:
            report['uncovered_witness']=d
            break
        chosen=replace(max(choices,key=lambda v:v[:2])[2],id=f'X{len(sites):04d}')
        sites.append(chosen)
        covered=[key for key,demand in missing.items() if interval_margin(
            s,chosen.point,demand['p0'],demand['p1'],'relay_access','transport',extra)[0]>=0]
        for key in covered:del missing[key]
        report['steps'].append(dict(site=asdict(chosen),covered=len(covered),remaining=len(missing)))
        print(json.dumps(report['steps'][-1]),flush=True)
    report.update(final_sites=len(sites),remaining_uncovered_unique_intervals=len(missing))
    return sites,report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',type=Path,default=Path('results_q3_q4_frontier/selection/primary/q3_plan.json'))
    p.add_argument('--output',type=Path,default=Path('results_q3_q4_resilience/expanded_sites'))
    p.add_argument('--extra-loss',type=float,default=1.)
    args=p.parse_args()
    plan=json.loads(args.input.read_text(encoding='utf8'))
    sites,report=expand(Scenario(),plan,args.extra_loss)
    report['source']=str(args.input)
    dump_json(args.output/'sites.json',[asdict(x) for x in sites])
    dump_json(args.output/'screening.json',report)
    print(json.dumps(report),flush=True)


if __name__=='__main__':main()
