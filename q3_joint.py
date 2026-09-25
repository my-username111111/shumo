"""Finite-candidate Q3 scheduling: route modes and four physical resources.

Every accepted route mode has continuous geometric coverage certificates.
CP-SAT chooses transport starts/types/aircraft/batteries and optional relay
sessions with variable service windows and independent energy components.
Bounds describe this finite, conservatively rounded model only.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from math import ceil, floor, isfinite
from time import perf_counter
import hashlib
import json

from ortools.sat.python import cp_model
from model import Scenario
from q3_upgrade import Site, build_demands, interval_margin, _relay_site
from q2_exact_schedule import assemble_plan
from verify import verify_q2


def up(x):
    return ceil(x - 1e-8)


def data_signature(s):
    h = hashlib.sha256(s.dem.values.tobytes())
    h.update(repr((s.radio, s.frequency_mhz, s.obstruction_db, s.fade_db,
                   s.system_loss_db, s.sensitivity_dbm, s.gateway_agl,
                   s.dem.x0, s.dem.y0, s.dem.dx, s.dem.dy)).encode())
    return h.hexdigest()


def site_pool(s, baseline, refine=False):
    """Keep used sites and explicitly evaluate 50/100 m and height neighbours."""
    home = s.nodes['O01']
    gateway = (home.lon, home.lat, home.ground + s.gateway_agl)
    coords = {(r['lon'], r['lat'], r['agl']) for r in baseline['relay_sorties']}
    if refine:
        for x, y, _ in list(coords):
            for dx, dy in ((0, 0), (-0.0005, 0), (0.0005, 0),
                           (0, -0.0005), (0, 0.0005), (-0.001, 0), (0.001, 0)):
                for z in (100., 200., 300.):
                    coords.add((x+dx, y+dy, z))
    sites = []
    for x, y, z in sorted(coords):
        candidate = _relay_site(s, x, y, z, gateway)
        if candidate is not None:
            sites.append(replace(candidate, id=f'J{len(sites):04d}'))
    return sites


def refine_pool(s, baseline, extra=0., keep_per_relay=2):
    """Fine positions selected by joint coverage, travel cost and margin.

    This is a budgeted heuristic shortlist, not a safe dominance deletion.
    All original selected sites remain available in addition to improvements.
    """
    candidates=site_pool(s,baseline,True)
    kept={(r['lon'],r['lat'],r['agl']) for r in baseline['relay_sorties']}
    transport={r['id']:r for r in baseline['transport_sorties']}
    from model import phase_position
    for relay in baseline['relay_sorties']:
        assigned=[c for c in baseline['communication_intervals']
                  if c.get('relay_sortie')==relay['id'] and c['end']>c['start']]
        choices=[]
        for site in candidates:
            if abs(site.lon-relay['lon'])>.00101 or abs(site.lat-relay['lat'])>.00101 or site.gateway_margin<extra:
                continue
            margins=[]
            for c in assigned:
                row=transport[c['sortie']];ph=row['route']['phases'][c['phase_index']]
                value,_=interval_margin(s,site.point,phase_position(ph,c['start']-row['start']),
                    phase_position(ph,c['end']-row['start']),'relay_access','transport',extra)
                margins.append(value)
                if value<0: break
            if margins and min(margins)>=0:
                choices.append((site.travel_energy_kwh,-min(site.gateway_margin-extra,min(margins)),site))
        choices.sort(key=lambda v:v[:2])
        for _,_,site in choices[:keep_per_relay]: kept.add((site.lon,site.lat,site.agl))
    selected=[x for x in candidates if (x.lon,x.lat,x.agl) in kept]
    return [replace(x,id=f'J{i:04d}') for i,x in enumerate(selected)],dict(
        screened_positions_and_heights=len(candidates),retained=len(selected),
        horizontal_steps_degrees=[.0005,.001],height_levels_m=[100,200,300],
        selection='heuristic shortlist preserving incumbent sites; not dominance proof')


class Geometry:
    def __init__(self, s, sites, extra=0., interval=90., cache=None):
        if not isfinite(extra) or extra<0 or not isfinite(interval) or interval<=0:
            raise ValueError('Extra loss must be nonnegative and interval must be positive')
        self.s, self.sites, self.extra, self.interval = s, sites, extra, interval
        self.cache = cache or Path(__file__).parent / '.q3_cache' / 'joint'
        self.cache.mkdir(parents=True, exist_ok=True)
        self.fingerprint = data_signature(s)
        self.memory = {}

    def template(self, route):
        key = hashlib.sha256(json.dumps(dict(version=3, data=self.fingerprint,
            route=route, sites=[asdict(x) for x in self.sites],
            extra=self.extra, interval=self.interval), sort_keys=True).encode()).hexdigest()
        if key in self.memory:
            return self.memory[key]
        path = self.cache / (key + '.json')
        if path.exists():
            result = json.loads(path.read_text(encoding='utf-8'))
        else:
            demands, direct = build_demands(self.s, {'sorties': [
                dict(id='template', start=0., route=route)]}, self.interval, self.extra)
            result = dict(demands=[], direct=direct)
            for d in demands:
                choices = []
                for i, site in enumerate(self.sites):
                    if site.gateway_margin < self.extra:
                        continue
                    margin, proof = interval_margin(self.s, site.point, d.p0, d.p1,
                        'relay_access', 'transport', self.extra)
                    if min(site.gateway_margin-self.extra, margin) >= 0:
                        choices.append(i)
                result['demands'].append(dict(**asdict(d), sites=choices))
            path.write_text(json.dumps(result), encoding='utf-8')
        self.memory[key] = result
        return result


def prepare_modes(s, source, geometry, variable_models=True):
    all_modes = []
    for row in source['sorties']:
        modes = []
        for g in (s.transport if variable_models else [row['model']]):
            try:
                route = s.route(g, row['visits'])
            except ValueError:
                continue
            template = geometry.template(route)
            if any(not d['sites'] for d in template['demands']):
                continue
            modes.append(dict(model=g, route=route, **template))
        if not modes:
            raise ValueError(f"No continuously coverable route mode for {row['id']}")
        all_modes.append(modes)
    return all_modes


def solve(s, source, geometry, seconds=20., seed=2026, slots=2,
          variable_models=True, policy='timely', q4_groups=None,
          independent_groups=False, resource_buffer=0., objective_caps=None,
          group_resource_caps=None):
    if not isfinite(seconds) or seconds<=0 or not isinstance(slots,int) or slots<1:
        raise ValueError('Positive solver time and positive integer relay slots required')
    if policy not in {'timely','energy','makespan'}:
        raise ValueError('Unknown Q3 objective policy')
    if not isfinite(resource_buffer) or resource_buffer < 0:
        raise ValueError('Nonnegative resource handoff buffer required')
    groups = q4_groups or [list(s.zone_boxes)]
    if (any(not g for g in groups) or set(z for g in groups for z in g) != set(s.zone_boxes)
            or sum(map(len, groups)) != len(s.zone_boxes)):
        raise ValueError('Q4 groups must partition all service zones')
    job_groups = []
    for row in source['sorties']:
        zones = {z for z, _ in row['visits']}
        choices = [i for i, group in enumerate(groups) if zones <= set(group)]
        if len(choices) != 1:
            raise ValueError('A fixed transport route crosses the requested Q4 groups')
        job_groups.append(choices[0])
    buffer = up(resource_buffer)
    start_clock = perf_counter()
    modes = prepare_modes(s, source, geometry, variable_models)
    sites = geometry.sites
    # Slot count bounds are explicit: no inference about the continuous problem.
    model = cp_model.CpModel()
    horizon = 24000
    intervals = {('drone', u): [] for u in s.aircraft}
    batteries = {f'{g}{i:02d}': g for g, n in s.battery_count.items() for i in range(1,n+1)}
    intervals.update({('battery', b): [] for b in batteries})
    intervals.update({('relay', r): [] for r in s.relays})
    components = [f'RE{i:02d}' for i in range(1,s.relay_energy_count+1)]
    intervals.update({('component', c): [] for c in components})
    owners = ({resource: model.NewIntVar(0, len(groups)-1, f'owner_{resource[0]}_{resource[1]}')
               for resource in intervals} if independent_groups else {})
    starts, selections, aircraft, bat_vars = [], [], [], []
    soft_terms, weighted_terms, energy_terms, endings = [], [], [], []
    for i, options in enumerate(modes):
        t = model.NewIntVar(0, horizon, f't{i}')
        starts.append(t)
        selected, av, bv = [], {}, {}
        for k, option in enumerate(options):
            g, route = option['model'], option['route']
            x = model.NewBoolVar(f'x{i}_{k}')
            selected.append(x)
            for kind, inventory, target, duration in (
                ('drone', s.aircraft, av, up(route['duration'])),
                ('battery', batteries, bv, up(route['duration'] +
                    s.charge_time(route['return_soc'], s.battery_charge[g])))):
                ids = [u for u, typ in inventory.items() if typ == g]
                choices = []
                for u in ids:
                    flag = model.NewBoolVar(f'{kind}{i}_{k}_{u}')
                    if independent_groups:
                        model.Add(owners[kind,u] == job_groups[i]).OnlyEnforceIf(flag)
                    choices.append(flag); target[(k,u)] = flag
                    interval = model.NewOptionalFixedSizeIntervalVar(t, duration+buffer, flag,
                                                                     f'{kind}I{i}_{k}_{u}')
                    intervals[(kind,u)].append(interval)
                model.Add(sum(choices) == x)
            end = model.NewIntVar(0, horizon+6000, f'end{i}_{k}')
            model.Add(end == t+up(route['duration'])).OnlyEnforceIf(x)
            model.Add(end == 0).OnlyEnforceIf(x.Not()); endings.append(end)
            for bid, relative in route['delivery'].items():
                box = s.boxes[bid]
                when = model.NewIntVar(0, horizon+6000, f'delivery{i}_{k}_{bid}')
                model.Add(when == t+up(relative)).OnlyEnforceIf(x)
                model.Add(when == 0).OnlyEnforceIf(x.Not())
                weighted_terms.append(box.priority*when)
                if box.hard_deadline is not None:
                    model.Add(when <= floor(box.hard_deadline))
                else:
                    late = model.NewIntVar(0,horizon+6000,f'late{i}_{k}_{bid}')
                    model.AddMaxEquality(late,[0,when-box.desired])
                    soft_terms.append(box.priority*late)
            energy_terms.append(up(route['energy_kwh']*1e6)*x)
        model.AddExactlyOne(selected)
        selections.append(selected); aircraft.append(av); bat_vars.append(bv)

    # A Q4 group must execute its own transport tasks. Bound its type-specific
    # interval peaks while Q3 still chooses the actual fleet and timetable.
    # The extra buffer is conservative relative to Q4's half-open intervals.
    group_caps = []
    for group_index, kind, typ, cap in group_resource_caps or []:
        if (not 0 <= group_index < len(groups) or kind not in ('aircraft','battery')
                or typ not in s.transport or not isinstance(cap,int) or cap < 0):
            raise ValueError('Invalid Q4 group resource cap')
        scoped = []
        for i, options in enumerate(modes):
            if job_groups[i] != group_index:
                continue
            for k, option in enumerate(options):
                if option['model'] != typ:
                    continue
                route = option['route']
                size = route['duration']
                if kind == 'battery':
                    size += s.charge_time(route['return_soc'], s.battery_charge[typ])
                scoped.append(model.NewOptionalFixedSizeIntervalVar(
                    starts[i], up(size)+buffer, selections[i][k],
                    f'q4cap_{group_index}_{kind}_{typ}_{i}_{k}'))
        if scoped:
            model.AddCumulative(scoped, [1]*len(scoped), cap)
        group_caps.append(dict(group_index=group_index, kind=kind, model=typ, cap=cap))

    # Variable-duration relay sessions. Charge time is a conservative piecewise
    # affine upper rounding of the original two-stage SOC curve, not a full
    # charge assigned to the aircraft itself.
    sessions = []
    power = (s.relay.hover_kw+s.relay.comm_kw)/3600
    for j, site in enumerate(sites):
        if site.gateway_margin < geometry.extra:
            continue
        for group_index, group in enumerate(groups):
            previous = None
            for slot in range(slots):
                name=f'r{j}_{group_index}_{slot}'
                active=model.NewBoolVar(name)
                launch=model.NewIntVar(0,horizon,name+'launch')
                service=model.NewIntVar(0,floor(site.max_service_seconds),name+'service')
                established=launch+up(site.lead)
                finish=model.NewIntVar(0,horizon+10000,name+'finish')
                model.Add(finish==established+service+up(site.back_time))
                model.Add(service==0).OnlyEnforceIf(active.Not())
                model.Add(launch==0).OnlyEnforceIf(active.Not())
                relay_duration=up(site.lead)+service+up(site.back_time+s.relay.turnaround)+buffer
                relay_release=model.NewIntVar(0,horizon+10000,name+'relay_release')
                model.Add(relay_release==launch+relay_duration)
                rflags={}
                for r in s.relays:
                    f=model.NewBoolVar(name+r); rflags[r]=f
                    if independent_groups:
                        model.Add(owners['relay',r] == group_index).OnlyEnforceIf(f)
                    intervals[('relay',r)].append(model.NewOptionalIntervalVar(
                        launch,relay_duration,relay_release,f,name+r+'I'))
                model.Add(sum(rflags.values())==active)
                energy=model.NewIntVar(0,up(s.relay.battery_kwh*1e6),name+'energy')
                model.Add(energy==up(site.travel_energy_kwh*1e6)+up(power*1e6)*service)
                model.Add(energy<=floor(s.relay.battery_kwh*(1-s.relay.reserve)*1e6)).OnlyEnforceIf(active)
                # charge(e) = min(3.5*F*e/B, F*(0.65*e/(.9B)+.35-.65*.1/.9))
                mult=1000000
                a1=ceil(s.relay_charge*3.5/s.relay.battery_kwh)
                a2=ceil(s.relay_charge*.65/(.9*s.relay.battery_kwh))
                c2=ceil(s.relay_charge*(.35-.65*.1/.9)*mult)
                numerator=model.NewIntVar(0,10**13,name+'charge_num')
                model.AddMinEquality(numerator,[a1*energy,a2*energy+c2])
                charge=model.NewIntVar(0,100000,name+'charge')
                model.AddDivisionEquality(charge,numerator+mult-1,mult)
                cflags={}
                for c in components:
                    f=model.NewBoolVar(name+c); cflags[c]=f
                    if independent_groups:
                        model.Add(owners['component',c] == group_index).OnlyEnforceIf(f)
                    size=model.NewIntVar(0,horizon+100000,name+c+'size')
                    model.Add(size==up(site.lead)+service+up(site.back_time)+charge+buffer)
                    release=model.NewIntVar(0,2*horizon+100000,name+c+'release')
                    model.Add(release==launch+size)
                    intervals[('component',c)].append(model.NewOptionalIntervalVar(
                        launch,size,release,f,name+c+'I'))
                model.Add(sum(cflags.values())==active)
                ee=model.NewIntVar(0,up(s.relay.battery_kwh*1e6),name+'selected_energy')
                model.Add(ee==energy).OnlyEnforceIf(active)
                model.Add(ee==0).OnlyEnforceIf(active.Not()); energy_terms.append(ee)
                ef=model.NewIntVar(0,horizon+10000,name+'selected_end')
                model.Add(ef==finish).OnlyEnforceIf(active)
                model.Add(ef==0).OnlyEnforceIf(active.Not()); endings.append(ef)
                current=dict(site=j,active=active,start=launch,established=established,
                    service=service,relay=rflags,component=cflags,assignments=[],group=set(group))
                if previous:
                    model.Add(active<=previous['active'])
                    model.Add(launch>=previous['start']).OnlyEnforceIf(active)
                previous=current; sessions.append(current)
    for i, options in enumerate(modes):
        for k, option in enumerate(options):
            zones={z for z,_ in option['route']['visits']}
            for d_index,d in enumerate(option['demands']):
                choices=[]
                for r,session in enumerate(sessions):
                    if session['site'] not in d['sites'] or not zones<=session['group']:
                        continue
                    f=model.NewBoolVar(f'cover{i}_{k}_{d_index}_{r}')
                    choices.append(f); session['assignments'].append((f,i,k,d_index))
                    model.Add(f<=session['active'])
                    model.Add(session['established']<=starts[i]+floor(d['start'])).OnlyEnforceIf(f)
                    model.Add(session['established']+session['service']>=starts[i]+up(d['end'])).OnlyEnforceIf(f)
                model.Add(sum(choices)==selections[i][k])
    for session in sessions:
        model.Add(session['active']<=sum(x[0] for x in session['assignments']))
    for rows in intervals.values():
        model.AddNoOverlap(rows)
    for i,row in enumerate(source['sorties']):
        model.AddHint(starts[i],up(row.get('start',0.)))
        for k,option in enumerate(modes[i]):
            model.AddHint(selections[i][k],int(option['model']==row['model']))
        for (k,u),flag in aircraft[i].items():
            model.AddHint(flag,int(modes[i][k]['model']==row['model'] and u==row.get('drone')))
        for (k,b),flag in bat_vars[i].items():
            model.AddHint(flag,int(modes[i][k]['model']==row['model'] and b==row.get('battery')))
    # A verified schedule is a partial hint only; all constraints remain active.
    used_hints=set()
    for session in sessions:
        site=sites[session['site']]
        options=[r for r in source.get('relay_sorties',[]) if r['id'] not in used_hints and
                 abs(r['lon']-site.lon)<1e-9 and abs(r['lat']-site.lat)<1e-9 and abs(r['agl']-site.agl)<1e-9]
        old=min(options,key=lambda r:r['start']) if options else None
        model.AddHint(session['active'],int(old is not None))
        if old:
            used_hints.add(old['id'])
            # Exported launch includes the fractional lead-rounding offset.
            # Recover the original integer launch; ceil(actual launch) shifts
            # the service window by one second and can invalidate a tight hint.
            model.AddHint(session['start'],max(0,up(old['established'])-up(site.lead)))
            model.AddHint(session['service'],min(floor(site.max_service_seconds),up(old['service_end']-old['established'])))
            for r,flag in session['relay'].items():model.AddHint(flag,int(r==old['relay']))
            for c,flag in session['component'].items():model.AddHint(flag,int(c==old['energy_component']))
    makespan=model.NewIntVar(0,horizon+10000,'Cmax'); model.AddMaxEquality(makespan,endings)
    objectives=dict(soft=sum(soft_terms),weighted=sum(weighted_terms),makespan=makespan,
                    energy=sum(energy_terms),sorties=sum(x['active'] for x in sessions))
    for name, cap in (objective_caps or {}).items():
        if name not in objectives or not isfinite(cap) or cap < 0:
            raise ValueError('Invalid objective cap')
        model.Add(objectives[name] <= floor(cap))
    order=['soft','weighted','makespan','energy','sorties'] if policy=='timely' else ['soft','energy','makespan','weighted','sorties']
    if policy == 'makespan':
        order=['soft','makespan','weighted','energy','sorties']
    reports=[]; solver=None; successful=None
    for target in order:
        model.Minimize(objectives[target])
        solver=cp_model.CpSolver(); solver.parameters.max_time_in_seconds=seconds
        solver.parameters.num_search_workers=8; solver.parameters.random_seed=seed
        status=solver.Solve(model)
        value=solver.ObjectiveValue() if status in (cp_model.FEASIBLE,cp_model.OPTIMAL) else None
        bound=solver.BestObjectiveBound()
        reports.append(dict(target=target,status=solver.StatusName(status),value=value,
            lower_bound=bound,relative_gap=(max(0.,value-bound)/max(1.,abs(value))) if value is not None else None))
        print(json.dumps(dict(stage='joint_cp',seed=seed,**reports[-1])),flush=True)
        if value is None:
            break
        successful=solver
        for previous_target in order[:order.index(target)+1]:
            model.Add(objectives[previous_target]<=solver.Value(objectives[previous_target]))
        # Full hints help later lexicographic phases retain feasibility.
        model.ClearHints()
        for index in range(len(model.Proto().variables)):
            variable=model.GetIntVarFromProtoIndex(index)
            model.AddHint(variable,solver.Value(variable))
    report=dict(stages=reports,policy=policy,seed=seed,scale_seconds=1,
        coverage_max_interval_seconds=geometry.interval,stage_budget_seconds=seconds,search_workers=8,
        candidate_sites=len(sites),route_modes=sum(map(len,modes)),session_slots=len(sessions),
        q4_constraint=bool(q4_groups),independent_groups=independent_groups,
        resource_buffer_seconds=resource_buffer,objective_caps=objective_caps or {},
        elapsed_seconds=perf_counter()-start_clock,
        bound_scope='Finite sites, fixed box groups/visit orders, optional type modes and bounded relay sessions; later bounds conditional on earlier attained caps.')
    if group_caps:
        report['q4_group_resource_caps']=group_caps
    if successful is None:
        return None,report
    solver=successful
    rows=[]
    for i,old in enumerate(source['sorties']):
        k=next(k for k,v in enumerate(selections[i]) if solver.Value(v))
        option=modes[i][k]; route=option['route']; g=option['model']; start=float(solver.Value(starts[i]))
        drone=next(u for (kk,u),v in aircraft[i].items() if kk==k and solver.Value(v))
        battery=next(b for (kk,b),v in bat_vars[i].items() if kk==k and solver.Value(v))
        rows.append(dict(id=old['id'],job=old.get('job',old['id']),model=g,drone=drone,battery=battery,
            start=start,return_time=start+route['duration'],battery_ready=start+route['duration']+
            s.charge_time(route['return_soc'],s.battery_charge[g]),route=route,
            visits=route['visits'],load_kg=route['load_kg'],load_m3=route['load_m3'],
            energy_kwh=route['energy_kwh'],return_soc=route['return_soc']))
    transport=assemble_plan(s,rows); verify_q2(s,transport)
    relays=[]
    for session in sessions:
        if not solver.Value(session['active']): continue
        site=sites[session['site']]; established=float(solver.Value(session['established']))
        service=float(solver.Value(session['service']))
        # Launch later by the lead-rounding slack; actual service stays exactly
        # at CP times, and return/charge remain within conservative intervals.
        start=established-site.lead
        trip=s.relay_sortie(site.lon,site.lat,site.agl,service)
        finish=start+trip['duration']
        relays.append(dict(id=f'R{len(relays)+1:03d}',site_id=site.id,lon=site.lon,lat=site.lat,
            service_zones=sorted(session['group']),
            agl=site.agl,altitude=site.altitude,start=start,established=established,
            service_end=established+service,return_time=finish,
            relay_ready=finish+s.relay.turnaround,component_ready=finish+s.charge_time(trip['return_soc'],s.relay_charge),
            energy_kwh=trip['energy_kwh'],return_soc=trip['return_soc'],
            relay=next(r for r,f in session['relay'].items() if solver.Value(f)),
            energy_component=next(c for c,f in session['component'].items() if solver.Value(f))))
    result=dict(schema='q3-joint-v2',transport_sorties=rows,relay_sorties=relays,
                deliveries=transport['deliveries'],search=report,extra_loss_db=geometry.extra)
    obj=transport['objective']
    result['objective']=dict(weighted_soft_delay=obj['weighted_soft_delay_seconds'],
        weighted_delivery_seconds=obj['weighted_delivery_seconds'],
        makespan=max([r['return_time'] for r in rows+relays]),
        energy_kwh=obj['energy_kwh']+sum(r['energy_kwh'] for r in relays),
        transport_energy_kwh=obj['energy_kwh'],relay_energy_kwh=sum(r['energy_kwh'] for r in relays),
        transport_sorties=len(rows),relay_sorties=len(relays),total_sorties=len(rows)+len(relays))
    return result,report
