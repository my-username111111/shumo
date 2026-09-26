"""End-to-end acceptance checks for fixed-structure Q3 retiming."""

import json
from pathlib import Path
import unittest

from model import Scenario
from optimize_q3_timing import optimize


ROOT = Path(__file__).resolve().parents[1]
PRIMARY = ROOT / 'results_q3_q4_resilience/selection/primary/q3_plan.json'


class Q3TimingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from q3_certificate import accept
        cls.scenario = Scenario()
        # Historical margins must be recomputed after the collinearity fix.
        cls.primary = accept(cls.scenario,json.loads(PRIMARY.read_text(encoding='utf8')))

    def test_buffer_five_replays_q3_and_q4(self):
        plan, q4, report = optimize(self.scenario, self.primary, 5.)
        self.assertEqual(report['q3_independent_validation'],'PASS')
        self.assertEqual(report['q4_independent_validation'],'PASS')
        self.assertEqual(plan['certificate']['extra_loss_db'],1.)
        self.assertEqual(plan['objective']['weighted_soft_delay'],0)
        self.assertEqual(len(plan['deliveries']),80)
        self.assertEqual(plan['objective']['total_sorties'],26)
        self.assertLess(plan['objective']['weighted_delivery_seconds'],2_620_000.)
        self.assertGreaterEqual(min(x for x in
            plan['independent_replay']['minimum_handoff_seconds'].values() if x is not None),5.)
        self.assertEqual(len(q4['components']['components']),3)
        self.assertEqual(next(p for p in q4['partitions'] if p['id']=='P3')['shortage_total'],2)

    def test_invalid_cap_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'caps'):
            optimize(self.scenario,self.primary,5.,delivery_cap=float('nan'))

    def test_makespan_policy_is_certified_and_optimizes_completion_first(self):
        plan, _, report = optimize(self.scenario,self.primary,5.,policy='makespan')
        self.assertEqual(report['lp_stages'][0]['objective'],'makespan')
        self.assertEqual(report['q3_independent_validation'],'PASS')
        self.assertEqual(report['q4_independent_validation'],'PASS')
        self.assertEqual(plan['objective']['weighted_soft_delay'],0)
        self.assertAlmostEqual(report['lp_stages'][0]['value'],plan['objective']['makespan'],places=3)

    def test_protected_partition_does_not_increase_any_resource_type(self):
        from q4_exact import solve
        before=solve(self.scenario,self.primary,json.dumps(self.primary).encode('utf8'))
        old=next(p for p in before['partitions'] if p['id']=='P3')
        _,after,report=optimize(self.scenario,self.primary,5.,preserve_partition='P3')
        new=next(p for p in after['partitions'] if p['id']=='P3')
        self.assertEqual(report['preserved_partition'],'P3')
        for key,value in old['resource_need'].items():
            self.assertLessEqual(new['resource_need'][key],value)


if __name__ == '__main__':
    unittest.main()
