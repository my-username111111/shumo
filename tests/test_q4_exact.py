"""Regression checks for the fixed-Q3 fourth-question computation."""

import copy
from hashlib import sha256
import json
from pathlib import Path
import unittest

from model import Scenario
from q4_audit import audit
from q4_exact import _matching, _peak, fixed_input, load_and_solve, resource_certificate


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "results_q3_complete" / "q3_plan.json"


class Q4ExactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scenario = Scenario()
        cls.plan = json.loads(PLAN.read_text(encoding="utf-8"))

    def test_half_open_peak_and_bottleneck_matching(self):
        rows = [dict(task="a", start=0.0, end=3.0, old_resource="X"),
                dict(task="b", start=1.0, end=4.0, old_resource="Y"),
                dict(task="c", start=4.0, end=6.0, old_resource="Y")]
        self.assertEqual(_peak(rows)["count"], 2)
        self.assertEqual(len(_matching(rows)), 1)
        cert, assignments = resource_certificate(rows, "G1", "A_aircraft")
        self.assertEqual(cert["minimum_resources"], 2)
        self.assertAlmostEqual(cert["optimal_min_handoff_slack_seconds"], 1.0)
        self.assertEqual(len(assignments), 3)

    def test_complete_four_partition_result_and_replay(self):
        result = load_and_solve(self.scenario, PLAN)
        # Git archives contain LF while Windows checkouts may use CRLF.
        # Validate the raw input fingerprint separately from fixture content.
        self.assertEqual(result["components"]["input_sha256"],
                         sha256(PLAN.read_bytes()).hexdigest().upper())
        self.assertEqual(result["components"]["canonical_plan_sha256"],
                         "374f80f59b32b667c146192e6b9caca447ab640b058ad6d2e263922de6c5bdbc")
        self.assertEqual([p["id"] for p in result["partitions"]], ["P1", "P2", "P3", "P4"])
        self.assertEqual([p["resource_total"] for p in result["partitions"]], [33, 35, 29, 35])
        self.assertEqual([p["shortage_total"] for p in result["partitions"]], [6, 8, 2, 8])
        self.assertEqual(result['pareto_partitions'],{'2':['P1','P2','P3'],'3':['P4']})
        p3 = result["partitions"][2]
        self.assertAlmostEqual(p3["certificates"]["G1"]["B_batteries"]
                               ["optimal_min_handoff_slack_seconds"], 767.7991467650509)
        self.assertAlmostEqual(p3["certificates"]["G1"]["C_batteries"]
                               ["optimal_min_handoff_slack_seconds"], 598.393369334316)
        self.assertEqual(audit(self.scenario, self.plan, result)["status"], "PASS")

    def test_bad_communication_reference_is_rejected(self):
        corrupted = copy.deepcopy(self.plan)
        row = next(r for r in corrupted["communication_intervals"] if r["mode"] == "relay")
        row["relay_sortie"] = "R999"
        with self.assertRaisesRegex(ValueError, "Unknown relay"):
            fixed_input(self.scenario, corrupted)

    def test_tampered_optimality_fields_are_rejected(self):
        for field, value in [('optimal_min_handoff_slack_seconds',999999.),
                             ('original_id_min_handoff_slack_seconds',999999.),
                             ('matching_size_at_next_threshold',999),
                             ('maximum_matching_size',999)]:
            with self.subTest(field=field):
                result = load_and_solve(self.scenario, PLAN)
                result['partitions'][2]['certificates']['G1']['B_batteries'][field]=value
                with self.assertRaises(AssertionError):
                    audit(self.scenario,self.plan,result)

    def test_unknown_physical_resource_is_rejected(self):
        result = load_and_solve(self.scenario, PLAN)
        result['assignments'][0]['physical_id']='U_FAKE'
        with self.assertRaises(AssertionError):
            audit(self.scenario,self.plan,result)

    def test_q3_changes_invalidate_q4_fingerprint(self):
        result = load_and_solve(self.scenario, PLAN)
        changed=copy.deepcopy(self.plan)
        changed['relay_sorties'][0]['lon']+=.001
        with self.assertRaisesRegex(AssertionError,'bound'):
            audit(self.scenario,changed,result)

    def test_missing_partition_and_corrupt_mapping_rejected(self):
        result=load_and_solve(self.scenario,PLAN)
        for field in ('partitions','inventory_mapping'):
            changed=copy.deepcopy(result)
            changed[field].pop()
            with self.subTest(field=field), self.assertRaises(AssertionError):
                audit(self.scenario,self.plan,changed)

    def test_misleading_summary_rejected(self):
        result=load_and_solve(self.scenario,PLAN)
        for field,value in [('resource_total',0),('shortage_total',0),
                            ('stock_sufficient',True),('workload_cv',0.)]:
            changed=copy.deepcopy(result)
            changed['partitions'][2][field]=value
            with self.subTest(field=field), self.assertRaises(AssertionError):
                audit(self.scenario,self.plan,changed)


if __name__ == "__main__":
    unittest.main()
