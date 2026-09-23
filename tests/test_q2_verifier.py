"""Fault-injection checks for the Q2 answer verifier."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model import Scenario  # noqa: E402
from q2 import Dispatch, solve  # noqa: E402
from q2 import Job  # noqa: E402
from q2_alns import solve_two_stage_alns  # noqa: E402
from verify import verify_q2  # noqa: E402


class Q2VerifierFaults(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scenario = Scenario()
        cls.answer = solve(cls.scenario, improve=False)
        verify_q2(cls.scenario, cls.answer)

    def assert_rejected(self, edit) -> None:
        answer = deepcopy(self.answer)
        edit(answer)
        with self.assertRaises(AssertionError):
            verify_q2(self.scenario, answer)

    def test_soft_target_competes_with_later_hard_deadline(self) -> None:
        dispatcher = Dispatch(self.scenario)
        jobs = {job.id: job for job in dispatcher.initial_jobs()}
        self.assertLess(dispatcher._job_key(jobs["S001-R1"]),
                        dispatcher._job_key(jobs["S005-H"]))

    def test_baseline_can_be_exported_with_alternatives(self) -> None:
        restored = json.loads(json.dumps(self.answer, allow_nan=False))
        verify_q2(self.scenario, restored)

    def test_relocate_candidates_do_not_duplicate_boxes(self) -> None:
        dispatcher = Dispatch(self.scenario)
        jobs = dispatcher.initial_jobs()
        baseline = dispatcher.schedule(jobs)
        original = dispatcher.evaluate
        duplicated = []

        def checked_evaluate(job, model):
            if len(job.box_ids) != len(set(job.box_ids)):
                duplicated.append(job.visits)
            return original(job, model)

        dispatcher.evaluate = checked_evaluate
        dispatcher.improve_routes(jobs, baseline, rounds=1, max_trials=80)
        self.assertEqual(duplicated, [])

    def test_joint_resource_beam_is_fully_verifiable(self) -> None:
        dispatcher = Dispatch(self.scenario)
        jobs = dispatcher.initial_jobs()
        plan, search = dispatcher.schedule_beam(jobs, beam_width=12,
                                                job_branches=3, assignment_branches=3)
        self.assertIsNotNone(plan)
        self.assertGreater(search["complete"], 0)
        verify_q2(self.scenario, plan)

    def test_two_stage_alns_keeps_complete_verified_plans(self) -> None:
        dispatcher = Dispatch(self.scenario)
        stage1, stage2, search = solve_two_stage_alns(
            dispatcher, self.answer, self.answer, Job,
            stage1_iterations=10, stage2_iterations=15, seeds=(7,), delivery_ratio=1.20)
        verify_q2(self.scenario, stage1)
        verify_q2(self.scenario, stage2)
        self.assertEqual(search["policy"], "stage1_service_then_stage2_zero_delay_energy")

    def test_unknown_aircraft(self) -> None:
        self.assert_rejected(lambda q: q["sorties"][0].__setitem__("drone", "U_FAKE"))

    def test_unknown_battery(self) -> None:
        self.assert_rejected(lambda q: q["sorties"][0].__setitem__("battery", "A_FAKE"))

    def test_mismatched_battery_type(self) -> None:
        first = self.answer["sorties"][0]
        other = next(f"{model}01" for model in self.scenario.battery_count if model != first["model"])
        self.assert_rejected(lambda q: q["sorties"][0].__setitem__("battery", other))

    def test_mismatched_aircraft_type(self) -> None:
        first = self.answer["sorties"][0]
        other = next(uid for uid, model in self.scenario.aircraft.items() if model != first["model"])
        self.assert_rejected(lambda q: q["sorties"][0].__setitem__("drone", other))

    def test_duplicate_sortie_id(self) -> None:
        self.assert_rejected(lambda q: q["sorties"][1].__setitem__("id", q["sorties"][0]["id"]))

    def test_wrong_delivery_source(self) -> None:
        bid = next(iter(self.answer["deliveries"]))
        self.assert_rejected(lambda q: q["deliveries"][bid].__setitem__("sortie", "T_FAKE"))

    def test_tampered_route_phase(self) -> None:
        self.assert_rejected(lambda q: q["sorties"][0]["route"]["phases"][0].__setitem__(
            "t1", q["sorties"][0]["route"]["phases"][0]["t1"] + 1))

    def test_tampered_objective(self) -> None:
        self.assert_rejected(lambda q: q["objective"].__setitem__(
            "energy_kwh", q["objective"]["energy_kwh"] + 1))


if __name__ == "__main__":
    unittest.main()
