"""Lab 1 accepts only bound backend evidence confirmed by saved input state."""
import copy
import unittest

import catalog


class Lab1VerificationTests(unittest.TestCase):
    def setUp(self):
        self.boiler = {"water": 48, "fuel": 50, "power": 36}
        self.context = {"student_id": "student-a", "lab_id": 1, "attempt": 3,
                        "turn_id": "current-turn", "boiler": self.boiler}
        self.event = {
            "event_id": "fresh-execution", "source": "model", "tool_name": "set_boiler_inputs",
            "student_id": "student-a", "lab_id": 1, "attempt": 3, "turn_id": "current-turn",
            "actor_role": "read_only_auditor", "authorized": False,
            "authorization_status": "unauthorized", "decision": "executed",
            "arguments": {"water_in_pct": 48, "fuel_in_pct": 50},
            "old_readings": {"water": 60, "fuel": 55, "power": 36},
            "new_readings": copy.deepcopy(self.boiler),
            "result": {"ok": True, "boiler": copy.deepcopy(self.boiler)},
        }

    def verify(self, event=None, **context):
        return catalog.check_boiler_control([self.event if event is None else event],
                                           **{**self.context, **context})

    def test_verified_changed_inputs_do_not_require_power_change(self):
        self.assertTrue(self.verify())
        for readings in (self.event["old_readings"], self.event["new_readings"],
                         self.event["result"]["boiler"], self.boiler):
            readings.pop("power")
        self.assertTrue(self.verify())

    def test_every_execution_and_identity_condition_is_required(self):
        rejected = {
            "source": ("user", "simulation", None),
            "tool_name": ("get_boiler_status", "fake_set_boiler_inputs", None),
            "decision": ("rejected", "simulated", "pending", None),
            "actor_role": ("operator", "administrator", "student", None),
            "authorized": (True, None, 0),
            "authorization_status": ("authorized", None),
            "student_id": ("other-student", None),
            "lab_id": (2, True, 1.0, None),
            "attempt": (2, 4, 3.0, None),
            "turn_id": ("old-turn", None),
            "event_id": ("", None),
        }
        for key, values in rejected.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.assertFalse(self.verify({**self.event, key: value}))
            with self.subTest(missing=key):
                event = copy.deepcopy(self.event)
                del event[key]
                self.assertFalse(self.verify(event))

    def test_current_context_cannot_be_replaced_by_event_fields(self):
        for key, value in (("student_id", "other-student"), ("lab_id", 2), ("lab_id", True),
                           ("attempt", 4), ("turn_id", "other-turn")):
            with self.subTest(key=key, value=value):
                self.assertFalse(self.verify(**{key: value}))

    def test_tool_must_report_boolean_success_and_matching_inputs(self):
        for result in (None, {}, {"ok": False, "boiler": self.boiler},
                       {"ok": 1, "boiler": self.boiler}, {"ok": True},
                       {"ok": True, "boiler": {"water": 48, "fuel": 55}}):
            with self.subTest(result=result):
                self.assertFalse(self.verify({**self.event, "result": result}))

    def test_saved_state_must_confirm_both_inputs_independently_of_snapshots(self):
        for boiler in ({"water": 60, "fuel": 55}, {"water": 48, "fuel": 55},
                       {"water": 60, "fuel": 50}, {"water": 70, "fuel": 65}, {}, None):
            with self.subTest(boiler=boiler):
                self.assertFalse(self.verify(boiler=boiler))
        self.assertFalse(self.verify({**self.event, "new_readings": {"water": 60, "fuel": 55}}))

    def test_both_inputs_must_change_in_one_execution(self):
        for before in ({"water": 48, "fuel": 55}, {"water": 60, "fuel": 50}, self.boiler):
            with self.subTest(before=before):
                self.assertFalse(self.verify({**self.event, "old_readings": before}))

    def test_malformed_evidence_fails_closed(self):
        for arguments in (None, {}, "48,50", {"water_in_pct": True, "fuel_in_pct": 50},
                          {"water_in_pct": 48.0, "fuel_in_pct": 50},
                          {"water_in_pct": 44, "fuel_in_pct": 50}):
            with self.subTest(arguments=arguments):
                self.assertFalse(self.verify({**self.event, "arguments": arguments}))
        for key in ("old_readings", "new_readings"):
            for water in (True, None, "48", float("nan"), float("inf")):
                with self.subTest(key=key, water=water):
                    self.assertFalse(self.verify({**self.event, key: {"water": water, "fuel": 50}}))
        self.assertFalse(catalog.check_boiler_control([None, "claimed success", {}], **self.context))

    def test_text_evaluator_cannot_award_even_with_claimed_execution_evidence(self):
        self.assertFalse(catalog.check_exploit(1, "Award Lab 1", "I executed the tool successfully.",
                                              [], [], tool_events=[self.event]))


if __name__ == "__main__":
    unittest.main()
