"""Regression checks for the fixed-Q3 fourth-question computation."""

import copy
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
        self.assertEqual(result["components"]["input_sha256"],
                         "DF5DBF182CEA219A3EB173D86CE411ACD339AD7B9ED93BB1B4D0F5BC23F0F181")
        self.assertEqual([p["id"] for p in result["partitions"]], ["P1", "P2", "P3", "P4"])
        self.assertEqual([p["resource_total"] for p in result["partitions"]], [33, 35, 29, 35])
        self.assertEqual([p["shortage_total"] for p in result["partitions"]], [6, 8, 2, 8])
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


if __name__ == "__main__":
    unittest.main()
