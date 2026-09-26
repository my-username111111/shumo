"""Independent continuous and physical Q3 acceptance gate.

Does not import the optimizer's link/terrain functions. Triangle/rectangle
intersection is evaluated by feasible vertices (not polygon clipping).
Unproved intervals are UNKNOWN, never silently accepted or labelled outages.
"""
from math import floor, sqrt, sin, cos, radians, atan2, log10, isfinite
import numpy as np
from model import phase_position
from transport_check import TransportReplay, close
from verify import verify_q2
from q4 import coupled_components


def independent_clear(s, fixed, p0, p1):
    dem=s.dem
    vertices=np.array([((p[0]-dem.x0)/dem.dx,(dem.y0-p[1])/dem.dy,p[2])
                       for p in (fixed,p0,p1)],dtype=float)
    if not np.isfinite(vertices).all(): return False
    if any(not (0<=x<dem.width and 0<=y<dem.nrows) for x,y,z in vertices): return False
    xmin=max(0,floor(vertices[:,0].min()-1e-9)); xmax=min(dem.width-1,floor(vertices[:,0].max()+1e-9))
    ymin=max(0,floor(vertices[:,1].min()-1e-9)); ymax=min(dem.nrows-1,floor(vertices[:,1].max()+1e-9))
    xx,yy=np.meshgrid(np.arange(xmin,xmax+1),np.arange(ymin,ymax+1))
    heights=dem.values[yy,xx]
    # Radial cruise legs can produce almost collinear XY vertices. Testing a
    # determinant against a fixed absolute epsilon can send this ill-conditioned
    # case into a plane inversion and falsely certify terrain clearance.
    edges=vertices[1:,:2]-vertices[0,:2]
    area=edges[0,0]*edges[1,1]-edges[0,1]*edges[1,0]
    area_tolerance=1e-10*max(1.,np.linalg.norm(edges[0])*np.linalg.norm(edges[1]))
    minimum=np.full(xx.shape,np.inf)
    if abs(area)<=area_tolerance:
        # A collapsed XY triangle has a piecewise-linear lower altitude
        # envelope on its three edges. Clip all edges against slightly padded
        # cells, taking their minimum; never invert the near-singular plane.
        padding=1e-8 + 1e-10*max(np.linalg.norm(edges[0]),np.linalg.norm(edges[1]))
        for first,last in zip(vertices,np.roll(vertices,-1,axis=0)):
            enter=np.zeros(xx.shape); leave=np.ones(xx.shape); valid=np.ones(xx.shape,dtype=bool)
            for origin,delta,lo in ((first[0],last[0]-first[0],xx),
                                    (first[1],last[1]-first[1],yy)):
                if abs(delta)<1e-12:
                    valid &= (origin>=lo-padding)&(origin<=lo+1+padding)
                else:
                    a,b=(lo-padding-origin)/delta,(lo+1+padding-origin)/delta
                    enter=np.maximum(enter,np.minimum(a,b));leave=np.minimum(leave,np.maximum(a,b))
            valid &= enter<=leave+1e-9
            lower=np.minimum(first[2]+enter*(last[2]-first[2]),
                             first[2]+leave*(last[2]-first[2]))-1e-4
            np.minimum(minimum,np.where(valid,lower,np.inf),out=minimum)
    else:
        # Local coordinates avoid cancellation from a large pixel origin.
        origin=vertices[0,:2].copy()
        matrix=np.column_stack((vertices[:,:2]-origin,np.ones(3)))
        coefficients=np.linalg.solve(matrix,vertices[:,2])
        inverse=np.linalg.inv(matrix.T)
        def offer(x,y,extra=True):
            local_x,local_y=x-origin[0],y-origin[1]
            bary=[inverse[k,0]*local_x+inverse[k,1]*local_y+inverse[k,2] for k in range(3)]
            valid=(x>=xx-1e-9)&(x<=xx+1+1e-9)&(y>=yy-1e-9)&(y<=yy+1+1e-9)&extra
            for value in bary: valid &= value>=-1e-9
            z=coefficients[0]*local_x+coefficients[1]*local_y+coefficients[2]
            np.minimum(minimum,np.where(valid,z,np.inf),out=minimum)
        for x,y,z in vertices: offer(x,y)
        for dx,dy in ((0,0),(0,1),(1,0),(1,1)): offer(xx+dx,yy+dy)
        for a,b in zip(vertices,np.roll(vertices,-1,axis=0)):
            dx,dy=b[0]-a[0],b[1]-a[1]
            if abs(dx)>1e-12:
                for x in (xx,xx+1):
                    t=(x-a[0])/dx;offer(x,a[1]+t*dy,(t>=-1e-9)&(t<=1+1e-9))
            if abs(dy)>1e-12:
                for y in (yy,yy+1):
                    t=(y-a[1])/dy;offer(a[0]+t*dx,y,(t>=-1e-9)&(t<=1+1e-9))
    touched=np.isfinite(minimum)
    return bool(np.any(touched) and np.all(np.isfinite(heights[touched])) and
                np.all(heights[touched]!=dem.nodata) and
                np.all(heights[touched]<minimum[touched]-1e-7))


def margin(s,fixed,p0,p1,first,second,extra=0.):
    lo=min(fixed[1],p0[1],p1[1]);hi=max(fixed[1],p0[1],p1[1])
    c=1. if lo<=0<=hi else max(cos(radians(lo)),cos(radians(hi)))
    distance=max(sqrt((6371000*radians(p[1]-fixed[1]))**2+
        (6371000*c*radians(p[0]-fixed[0]))**2+(p[2]-fixed[2])**2) for p in (p0,p1))
    pa,ga=s.radio[first];pb,gb=s.radio[second]
    budget=min(pa,pb)+ga+gb-s.system_loss_db-s.sensitivity_dbm-s.fade_db
    clear=budget-32.45-20*log10(s.frequency_mhz)-20*log10(max(.001,distance/1000))-extra
    lower=clear-s.obstruction_db
    if lower>=0: return lower,'obstruction_upper_bound'
    if independent_clear(s,fixed,p0,p1): return clear,'independent_swept_cells'
    return lower,'unproved_clearance'


def point_margin(s,a,b,first,second,extra=0.):
    """Exact point witness; None means missing/near-tangent terrain data."""
    h=sin(radians(b[1]-a[1])/2)**2+cos(radians(a[1]))*cos(radians(b[1]))*sin(radians(b[0]-a[0])/2)**2
    distance=sqrt((12742000*atan2(sqrt(max(0,h)),sqrt(max(0,1-h))))**2+(b[2]-a[2])**2)
    pa,ga=s.radio[first];pb,gb=s.radio[second]
    clear=min(pa,pb)+ga+gb-s.system_loss_db-s.sensitivity_dbm-s.fade_db-32.45-20*log10(s.frequency_mhz)-20*log10(max(.001,distance/1000))-extra
    if clear<0:return clear  # Even unobstructed propagation cannot work.
    d=s.dem;x0,y0=(a[0]-d.x0)/d.dx,(d.y0-a[1])/d.dy
    x1,y1=(b[0]-d.x0)/d.dx,(d.y0-b[1])/d.dy
    try: cells=TransportReplay(s).cells(a[:2],b[:2])
    except ValueError:return None
    uncertain=False;blocked=False
    for x,y in cells:
        z=float(d.values[y,x])
        if not isfinite(z) or z==d.nodata:return None
        enter,leave=0.,1.
        for origin,delta,low in ((x0,x1-x0,x),(y0,y1-y0,y)):
            if abs(delta)>1e-12:
                u,v=(low-origin)/delta,(low+1-origin)/delta
                enter=max(enter,min(u,v));leave=min(leave,max(u,v))
        if enter>leave+1e-9:continue
        ray=min(a[2]+enter*(b[2]-a[2]),a[2]+leave*(b[2]-a[2]))
        if z>ray+1e-6:blocked=True
        elif abs(z-ray)<=1e-6:uncertain=True
    if blocked:return clear-s.obstruction_db
    if uncertain:return None
    return clear


def charge(soc,full):
    return full*(.65*(.9-soc)/.9+.35) if soc<.9 else full*.35*(1-soc)/.1


def physical_audit(s,plan):
    checked=verify_q2(s,dict(sorties=plan['transport_sorties'],deliveries=plan['deliveries']))
    buffer=plan.get('search',{}).get('resource_buffer_seconds',0.)
    if not isfinite(buffer) or buffer<0:
        raise AssertionError('Invalid resource buffer')
    handoffs={}
    for kind,rows,key,end in (
        ('aircraft',plan['transport_sorties'],'drone','return_time'),
        ('battery',plan['transport_sorties'],'battery','battery_ready'),
        ('relay',plan['relay_sorties'],'relay','relay_ready'),
        ('component',plan['relay_sorties'],'energy_component','component_ready')):
        gaps=[]
        for rid in {row[key] for row in rows}:
            chain=sorted((row for row in rows if row[key]==rid),key=lambda row:row['start'])
            gaps.extend(b['start']-a[end] for a,b in zip(chain,chain[1:]))
        handoffs[kind]=min(gaps) if gaps else None
        if gaps and min(gaps)<buffer-1e-7:
            raise AssertionError(f'{kind} handoff violates declared resource buffer')
    checked['resource_buffer_seconds']=buffer
    checked['minimum_handoff_seconds']=handoffs
    for row in plan['transport_sorties']:
        close(row['battery_ready'],row['return_time']+charge(row['return_soc'],
              s.battery_charge[row['model']]),'independent transport battery charging')
    replay=TransportReplay(s);r=s.relay;home=s.nodes['O01'];origin=(home.lon,home.lat,home.work_alt)
    ids=[];groups={}
    for row in plan['relay_sorties']:
        ids.append(row['id'])
        if row['relay'] not in s.relays or row['energy_component'] not in {
            f'RE{i:02d}' for i in range(1,s.relay_energy_count+1)}: raise AssertionError('Unknown relay resource')
        if not all(isfinite(float(row[k])) for k in ('start','established','service_end','return_time','agl','lon','lat')):
            raise AssertionError('Nonfinite relay data')
        if row['start']<0 or row['service_end']<row['established'] or not 0<=row['agl']<=r.max_agl:
            raise AssertionError('Invalid relay times/height')
        target=(row['lon'],row['lat'],s.dem.height(row['lon'],row['lat'])+row['agl'])
        close(row['altitude'],target[2],'relay altitude')
        heights=[float(s.dem.values[y,x]) for x,y in replay.cells(origin[:2],target[:2])]
        if not heights or any(not isfinite(z) or z==s.dem.nodata for z in heights): raise AssertionError('Missing relay terrain')
        peak=max(max(heights)+50,origin[2],target[2])
        h=sin(radians(target[1]-origin[1])/2)**2+cos(radians(origin[1]))*cos(radians(target[1]))*sin(radians(target[0]-origin[0])/2)**2
        distance=12742000*atan2(sqrt(h),sqrt(max(0,1-h)))
        outbound=(peak-origin[2])/r.climb_speed+distance/r.speed+(peak-target[2])/r.descend_speed
        back=(peak-target[2])/r.climb_speed+distance/r.speed+(peak-origin[2])/r.descend_speed
        service=row['service_end']-row['established']
        energy=r.cruise_kw*2*distance/r.speed/3600+r.mass*9.81*(2*peak-origin[2]-target[2])/(r.climb_efficiency*3600000)+(r.hover_kw+r.comm_kw)*(r.establish+service)/3600
        soc=1-energy/r.battery_kwh
        if soc<r.reserve-1e-8: raise AssertionError('Relay return reserve')
        for actual,expected,label in ((row['established'],row['start']+r.prepare+outbound+r.establish,'establishment'),
            (row['return_time'],row['service_end']+back,'relay return'),(row['energy_kwh'],energy,'relay energy'),
            (row['return_soc'],soc,'relay SOC'),(row['relay_ready'],row['return_time']+r.turnaround,'turnaround'),
            (row['component_ready'],row['return_time']+charge(soc,s.relay_charge),'component charge')):
            close(actual,expected,label)
        for kind,key,end in (('relay','relay','relay_ready'),('component','energy_component','component_ready')):
            groups.setdefault((kind,row[key]),[]).append((row['start'],row[end]))
    if len(ids)!=len(set(ids)): raise AssertionError('Duplicate relay task ID')
    for resource,spans in groups.items():
        spans.sort()
        if any(b[0]<a[1]-1e-7 for a,b in zip(spans,spans[1:])): raise AssertionError(f'Resource overlap {resource}')
    obj=plan['objective'];tr=plan['transport_sorties'];rr=plan['relay_sorties'];delivery=plan['deliveries']
    expected=dict(weighted_soft_delay=sum(s.boxes[b].priority*max(0,d['time']-s.boxes[b].desired) for b,d in delivery.items() if s.boxes[b].hard_deadline is None),
        weighted_delivery_seconds=sum(s.boxes[b].priority*d['time'] for b,d in delivery.items()),
        makespan=max(x['return_time'] for x in tr+rr),energy_kwh=sum(x['energy_kwh'] for x in tr+rr),
        transport_energy_kwh=sum(x['energy_kwh'] for x in tr),relay_energy_kwh=sum(x['energy_kwh'] for x in rr),
        transport_sorties=len(tr),relay_sorties=len(rr),total_sorties=len(tr)+len(rr))
    for key,value in expected.items(): close(obj[key],value,'objective '+key)
    return dict(status='PASS',**checked,relay_sorties=len(rr),four_resource_classes=True)


def certify(s,plan,extra=0.,minimum_interval=.25,kernel=margin,stop_on_failure=False):
    if not isfinite(extra) or extra<0 or not isfinite(minimum_interval) or minimum_interval<=0:
        raise ValueError('Invalid communication loss or minimum interval')
    home=s.nodes['O01'];gateway=(home.lon,home.lat,home.ground+s.gateway_agl)
    relays=plan['relay_sorties'];intervals=[]
    # Saved boundaries are subdivision hints only. Every resulting interval
    # is still independently proved; stored modes/margins are never trusted.
    from collections import defaultdict
    cuts=defaultdict(set)
    for row in plan.get('communication_intervals', []):
        cuts[row['sortie'],row['phase_index']].update((row['start'],row['end']))
    backhaul={r['id']:kernel(s,gateway,(r['lon'],r['lat'],r['altitude']),
        (r['lon'],r['lat'],r['altitude']),'relay_backhaul','gateway',extra)[0] for r in relays}
    for row in plan['transport_sorties']:
        for pi,ph in enumerate(row['route']['phases']):
            left,right=row['start']+ph['t0'],row['start']+ph['t1']
            service_events={left,right,
                *[r[k] for r in relays for k in ('established','service_end') if left<r[k]<right]}
            # A translated saved cut can differ from a phase/service event by
            # floating-point noise. Do not create spurious near-zero intervals.
            hints=[v for v in cuts[row['id'],pi] if left<v<right
                   and all(abs(v-event)>1e-7 for event in service_events)]
            events=sorted(service_events | set(hints))
            stack=list(zip(events,events[1:])) if right>left else [(left,right)]
            while stack:
                a,b=stack.pop();p0=phase_position(ph,a-row['start']);p1=phase_position(ph,b-row['start'])
                dm,proof=kernel(s,gateway,p0,p1,'transport','gateway',extra)
                options=[(dm,'direct','',proof)]
                for r in relays:
                    if 'service_zones' in r and not {z for z,_ in row['visits']}<=set(r['service_zones']):
                        continue
                    if r['established']<=a+1e-8 and r['service_end']>=b-1e-8:
                        rm,rproof=kernel(s,(r['lon'],r['lat'],r['altitude']),p0,p1,'relay_access','transport',extra)
                        options.append((min(rm,backhaul[r['id']]),'relay',r['id'],rproof))
                best=max(options)
                if dm>=0: best=options[0]
                if best[0]<0 and stop_on_failure and kernel is margin:
                    when=(a+b)/2;position=phase_position(ph,when-row['start'])
                    point_paths=[point_margin(s,gateway,position,'transport','gateway',extra)]
                    for r in relays:
                        if 'service_zones' in r and not {z for z,_ in row['visits']}<=set(r['service_zones']):
                            continue
                        if r['established']<=when<=r['service_end']:
                            point=(r['lon'],r['lat'],r['altitude'])
                            access=point_margin(s,point,position,'relay_access','transport',extra)
                            back=point_margin(s,gateway,point,'relay_backhaul','gateway',extra)
                            point_paths.append(min(access,back) if access is not None and back is not None else None)
                    if all(value is not None and value<0 for value in point_paths):
                        return intervals,dict(unknown_seconds=None,outage_seconds=None),dict(
                            status='FAIL',extra_loss_db=extra,scope='point counterexample; outage duration not measured',
                            witness=dict(sortie=row['id'],time=when,best_margin_db=max(point_paths)))
                if best[0]<0 and b-a>minimum_interval:
                    mid=(a+b)/2;stack.extend([(a,mid),(mid,b)]);continue
                status='PASS' if best[0]>=0 else 'UNKNOWN'
                # A negative conservative bound is not an outage witness.
                intervals.append(dict(sortie=row['id'],phase=ph['kind'],phase_index=pi,start=a,end=b,
                    mode=best[1] if status=='PASS' else 'unknown',relay_sortie=best[2],
                    margin_lower_db=best[0],proof=best[3],status=status))
    intervals.sort(key=lambda x:(x['sortie'],x['start'],x['end']))
    unknown=sum(x['end']-x['start'] for x in intervals if x['status']!='PASS')
    bad=sum(x['status']!='PASS' for x in intervals)
    direct=sum(x['end']-x['start'] for x in intervals if x['mode']=='direct')
    relay=sum(x['end']-x['start'] for x in intervals if x['mode']=='relay')
    communication=dict(direct_seconds=direct,relay_seconds=relay,total_transport_seconds=direct+relay+unknown,
        relay_share=relay/(direct+relay+unknown) if direct+relay+unknown else 0.,
        unknown_seconds=unknown,outage_seconds=0. if not bad else None,
        minimum_certified_margin_db=min((x['margin_lower_db'] for x in intervals),default=0.))
    certificate=dict(status='PASS' if not bad else 'UNKNOWN',unknown_interval_count=bad,
        geometry_version=2,
        minimum_interval_seconds=minimum_interval,extra_loss_db=extra,
        dem_model='piecewise_constant_closed_cells',
        time_method='phase and service events; recursive interval lower bounds',
        independent_replay='independent vertex-enumeration geometry and relay physics')
    return intervals,communication,certificate


def accept(s,plan,extra=None):
    # Preserve a saved robustness scenario unless the caller explicitly changes it.
    from math import isfinite
    extra = plan.get('extra_loss_db', 0.) if extra is None else extra
    if not isfinite(extra) or extra < 0:
        raise ValueError('Extra communication loss must be finite and nonnegative')
    physical=physical_audit(s,plan)
    rows,communication,certificate=certify(s,plan,extra)
    if certificate['status']!='PASS':
        raise ValueError(f"Continuous replay UNKNOWN: {certificate['unknown_interval_count']} intervals")
    plan.update(extra_loss_db=extra,communication_intervals=rows,communication=communication,certificate=certificate,
                independent_replay=physical)
    components,relations=coupled_components(s,{**plan,'communication':rows})
    plan['q4_compatibility']=dict(coupled_components=components,component_count=len(components),
        relay_service_zones=relations,constraint_imposed_during_q3=plan.get('search',{}).get('q4_constraint',False))
    return plan


def legacy_view(plan):
    """Adapt a certified plan to the existing Q4/export schema without rerouting."""
    from copy import deepcopy
    result=deepcopy(plan)
    result['communication_statistics']=result['communication']
    result['communication']=result['communication_intervals']
    for relay in result['relay_sorties']:
        relay['covered_transport']=sorted({row['sortie'] for row in result['communication']
            if row['mode']=='relay' and row['relay_sortie']==relay['id'] and row['end']>row['start']})
    return result
