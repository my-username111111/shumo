import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from model import Scenario
from q3_certificate import accept, certify, independent_clear, physical_audit, legacy_view
from q3_joint import Geometry, data_signature
from q3_upgrade import swept_clear
from run_q3_joint import nondominated, lower_bounds

ROOT=Path(__file__).resolve().parents[1]


class GeometryTests(unittest.TestCase):
    def setUp(self):
        dem=SimpleNamespace(x0=0.,y0=5.,dx=1.,dy=1.,width=5,nrows=5,
            nodata=-9999.,values=np.zeros((5,5)))
        dem.pixel=lambda x,y:(x,5-y)
        self.s=SimpleNamespace(dem=dem)

    def test_swept_interior_obstacle_between_clear_endpoint_rays(self):
        fixed=(.2,4.8,10.);a=(4.8,4.8,10.);b=(4.8,.2,10.)
        self.assertTrue(independent_clear(self.s,fixed,a,b))
        self.s.dem.values[1,3]=20.
        self.assertFalse(independent_clear(self.s,fixed,a,b))
        self.assertFalse(swept_clear(self.s,fixed,a,b))

    def test_invalid_interval_is_rejected_before_subdivision(self):
        with self.assertRaises(ValueError):certify(None,{},minimum_interval=0)
        with self.assertRaises(ValueError):Geometry(None,[],interval=-1)

    def test_closed_cell_boundary_is_not_omitted(self):
        fixed=(1.,4.,10.);a=(4.,4.,10.);b=(4.,1.,10.)
        self.s.dem.values[0,2]=20.  # Adjacent cell touching the y=1 ray.
        self.assertFalse(independent_clear(self.s,fixed,a,b))
        self.assertFalse(swept_clear(self.s,fixed,a,b))

    def test_nodata_and_vertical_low_endpoint(self):
        fixed=(.2,4.8,10.);a=(4.8,.2,10.);b=(4.8,.2,30.)
        self.s.dem.values[2,2]=-9999.
        self.assertFalse(independent_clear(self.s,fixed,a,b))
        self.s.dem.values[2,2]=15.
        self.assertFalse(independent_clear(self.s,fixed,a,b))

    def test_subsecond_relay_gap_is_unknown(self):
        node=SimpleNamespace(lon=0,lat=0,ground=0)
        s=SimpleNamespace(nodes={'O01':node},gateway_agl=10)
        ph=dict(kind='cruise',t0=0.,t1=2.,a=(0,0,10),b=(1,1,10))
        plan=dict(transport_sorties=[dict(id='T1',start=0.,route={'phases':[ph]},visits=[('S1',[])])],
            relay_sorties=[dict(id='R1',lon=0,lat=0,altitude=10,established=0.,service_end=.999),
                          dict(id='R2',lon=0,lat=0,altitude=10,established=1.001,service_end=2.)])
        def kernel(s,f,a,b,first,second,extra):
            return (-1 if first=='transport' else 1),'test'
        rows,comm,cert=certify(s,plan,kernel=kernel)
        self.assertEqual(cert['status'],'UNKNOWN')
        self.assertAlmostEqual(comm['unknown_seconds'],.002)
        self.assertAlmostEqual(sum(r['end']-r['start'] for r in rows),2.)
        self.assertIsNone(comm['outage_seconds'])

    def test_disconnected_backhaul_cannot_certify_access(self):
        node=SimpleNamespace(lon=0,lat=0,ground=0)
        s=SimpleNamespace(nodes={'O01':node},gateway_agl=10)
        plan=dict(transport_sorties=[dict(id='T1',start=0.,visits=[('S1',[])],route={'phases':[
            dict(kind='climb',t0=0.,t1=.1,a=(0,0,10),b=(0,0,20))]})],
            relay_sorties=[dict(id='R1',lon=0,lat=0,altitude=10,established=0.,service_end=1.)])
        def kernel(s,f,a,b,first,second,extra):return (1 if first=='relay_access' else -1),'test'
        self.assertEqual(certify(s,plan,kernel=kernel)[2]['status'],'UNKNOWN')


class PhysicalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s=Scenario()
        cls.plan=json.loads((ROOT/'results_q3_upgrade/q3_plan.json').read_text(encoding='utf-8'))

    def test_independent_replay_does_not_call_optimizer_links(self):
        with patch('q3_upgrade.interval_margin',side_effect=AssertionError('shared kernel')), \
             patch.object(self.s,'blocked',side_effect=AssertionError('sampled kernel')), \
             patch.object(self.s,'link_margin',side_effect=AssertionError('sampled kernel')):
            result=accept(self.s,copy.deepcopy(self.plan))
        self.assertEqual(result['certificate']['status'],'PASS')

    def test_unknown_resource_and_tampered_times_rejected(self):
        for field,value in [('relay','R_FAKE'),('energy_component','RE99'),('established',0.),
                             ('component_ready',0.),('return_soc',1.),('energy_kwh',0.)]:
            with self.subTest(field=field):
                plan=copy.deepcopy(self.plan);plan['relay_sorties'][0][field]=value
                with self.assertRaises((AssertionError,ValueError)):physical_audit(self.s,plan)

    def test_component_overlap_rejected(self):
        plan=copy.deepcopy(self.plan)
        rows=plan['relay_sorties']
        pair=next((a,b) for i,a in enumerate(rows) for b in rows[i+1:]
                  if max(a['start'],b['start'])<min(a['component_ready'],b['component_ready']))
        pair[1]['energy_component']=pair[0]['energy_component']
        with self.assertRaises(AssertionError):physical_audit(self.s,plan)

    def test_final_relay_return_in_objective(self):
        plan=copy.deepcopy(self.plan);plan['objective']['makespan']-=1
        with self.assertRaises(AssertionError):physical_audit(self.s,plan)

    def test_robust_failure_requires_a_point_counterexample(self):
        _,comm,cert=certify(self.s,self.plan,extra=100.,stop_on_failure=True)
        self.assertEqual(cert['status'],'FAIL')
        self.assertLess(cert['witness']['best_margin_db'],0)
        self.assertIsNone(comm['outage_seconds'])

    def test_cache_keys_include_radio_parameters(self):
        before=data_signature(self.s);old=self.s.fade_db
        try:
            self.s.fade_db+=1
            self.assertNotEqual(before,data_signature(self.s))
        finally:self.s.fade_db=old

    def test_relaxed_bounds_do_not_exceed_feasible_plan(self):
        for key,value in lower_bounds(self.s).items():
            if key!='scope':self.assertLessEqual(value,self.plan['objective'][key]+1e-8)

    def test_archive_filters_dominated_records(self):
        a=copy.deepcopy(self.plan);b=copy.deepcopy(a)
        b['objective']['energy_kwh']+=1
        self.assertEqual(len(nondominated([a,b])),1)

    def test_q4_adapter_preserves_actual_service_relations(self):
        from q4 import coupled_components, solve
        from verify import verify_q4
        certified=accept(self.s,copy.deepcopy(self.plan))
        legacy=legacy_view(certified)
        components,_=coupled_components(self.s,legacy)
        self.assertEqual(components,certified['q4_compatibility']['coupled_components'])
        verify_q4(self.s,legacy,solve(self.s,legacy))
        self.assertEqual(legacy['transport_sorties'],certified['transport_sorties'])


if __name__=='__main__':unittest.main()
