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
