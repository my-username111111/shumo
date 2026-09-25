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
        cls.scenario = Scenario()
        cls.primary = json.loads(PRIMARY.read_text(encoding='utf8'))

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


if __name__ == '__main__':
    unittest.main()
