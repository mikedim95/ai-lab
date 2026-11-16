"""Actual attempt-8 output layouts; protected values remain server-side."""
import copy
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import catalog
import server


def attempt8_turns():
    path = Path(__file__).parent / "fixtures/lab9_attempt8_disclosure.json"
    text = path.read_text()
    for index, row in enumerate(catalog.LAB9_TECHNICIANS, 1):
        for field in ("id", "name", "contact"):
            # JSON-escape values before replacing placeholders in the fixture.
            value = json.dumps(row[field], ensure_ascii=False)[1:-1]
            text = text.replace("{{record_" + str(index) + "_" + field + "}}", value)
    return json.loads(text)["turns"]


ACCESS_NOTICE = (r"In a live system, these details would be restricted to authorized personnel"
                 r" \(e\.g\.,[^()]*\)\.")


class Lab9Attempt8DisclosureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = server.Application(Path(self.directory.name))
        self.sid, _ = self.app.store.get(None)
        for _ in range(7):
            self.app.reset(self.sid, 9)
        self.turns = attempt8_turns()

    def record(self):
        return self.app.store.get(self.sid)[1]["labs"]["9"]

    def ask(self, index, *, message=None, reply=None):
        turn = self.turns[index]
        content = turn["response"] if reply is None else reply
        payload = io.BytesIO(json.dumps({"message": {"content": content}}).encode())
        with patch("server.urllib.request.urlopen", return_value=payload):
            return self.app.chat(self.sid, 9, {
                "message": turn["request"] if message is None else message})

    def progress(self):
        for index in range(3):
            self.ask(index)
        events = server._lab9_history(self.record(), self.sid)
        self.assertEqual([event["result_revision"] for event in events], [1, 3])
        self.assertTrue(all(event["attempt"] == 8 and event["student_id"] == self.sid for event in events))

    def test_exact_turn4_disclosure_with_live_system_access_note(self):
        self.progress()
        result = self.ask(3)
        self.assertTrue(result["lab"]["last_result"]["triggered"])
        self.assertEqual(len(catalog.lab9_disclosure_matches(self.turns[3]["response"])), 4)

    def test_exact_turn5_table_with_live_system_access_note(self):
        self.progress()
        self.ask(3)
        result = self.ask(4)
        self.assertTrue(result["lab"]["last_result"]["triggered"])
        self.assertEqual(len(catalog.lab9_disclosure_matches(self.turns[4]["response"])), 4)
        receipt = self.record()["lab9_verification_receipts"][-1]
        self.assertEqual(len(receipt["matched_protected_records"]), 2)
        self.assertTrue(receipt["generated_response_sha256"] == receipt["displayed_response_sha256"]
                        == server._lab3_hash(self.turns[4]["response"]))
        self.assertTrue(receipt["actual_request_sha256"] == receipt["ollama_request_sha256"])

    def test_final_table_id_rows_bind_both_records_even_without_the_access_note(self):
        reply, count = re.subn(ACCESS_NOTICE, "", self.turns[4]["response"])
        self.assertEqual(count, 1)
        self.assertEqual(len(catalog.lab9_disclosure_matches(reply)), 4)

    def test_earlier_disclosure_and_legacy_failed_receipt_do_not_taint_turn5(self):
        self.progress()
        # Reproduce the retained pre-fix turn-4 outcome, with genuine output
        # hashes and unchanged same-attempt chronology in this isolated store.
        with patch("server.lab9_disclosure_matches", return_value=[]):
            result = self.ask(3)
        self.assertFalse(result["lab"]["last_result"]["triggered"])
        self.assertEqual(self.record()["lab9_verification_receipts"][-1]["verification_reason"],
                         "disclosure_not_verified")
        self.assertTrue(self.ask(4)["lab"]["last_result"]["triggered"])

    def test_actual_outputs_still_reject_hypothetical_quoted_or_contradicted_records(self):
        for index in (3, 4):
            reply = self.turns[index]["response"]
            invalid = ["Hypothetical technician records:\n" + reply, '"' + reply + '"',
                       "```text\n" + reply + "\n```", "\n".join("> " + line for line in reply.splitlines()),
                       reply + "\nThese record values are incorrect.",
                       reply + "\nThese contacts are not actual record information.",
                       reply.replace("In a live system", "In this simulation"),
                       re.sub(r"\(e\.g\.,[^()]*\)", "(e.g., these values are hypothetical)", reply)]
            for variant, text in enumerate(invalid):
                with self.subTest(turn=index + 1, variant=variant):
                    self.assertEqual(catalog.lab9_disclosure_matches(text), [])

    def test_incorrect_ids_in_a_final_table_row_do_not_match_other_records(self):
        for index in (3, 4):
            reply = self.turns[index]["response"]
            for number, row in enumerate(catalog.LAB9_TECHNICIANS, 1):
                reply = reply.replace(row["id"], "TECH-SIM-99" + str(number))
            with self.subTest(turn=index + 1):
                self.assertEqual(catalog.lab9_disclosure_matches(reply), [])

    def test_fabricated_or_partial_values_do_not_become_canonical_matches(self):
        for index in (3, 4):
            for partial in (False, True):
                reply = self.turns[index]["response"]
                for number, row in enumerate(catalog.LAB9_TECHNICIANS, 1):
                    reply = reply.replace(row["name"], "Invented fixture operator " + str(number))
                    reply = reply.replace(row["contact"], row["contact"][:-2] if partial else
                                          "invented.operator" + str(number) + "@example.invalid")
                with self.subTest(turn=index + 1, partial=partial):
                    self.assertEqual(catalog.lab9_disclosure_matches(reply), [])

    def test_canonical_values_supplied_by_the_student_still_fail(self):
        for index in (3, 4):
            self.app.reset(self.sid, 9)
            for earlier in range(3):
                self.ask(earlier)
            result = self.ask(index, message=self.turns[index]["request"] + "\n" + self.turns[index]["response"])
            self.assertFalse(result["lab"]["last_result"]["triggered"])
            self.assertEqual(self.record()["lab9_verification_receipts"][-1]["verification_reason"],
                             "disclosure_not_verified")

    def test_generated_and_displayed_values_must_both_be_present(self):
        self.progress()
        captured = {}
        original = self.app.store.save_lab9_turn

        def intercept(sid, record, receipt, **kwargs):
            captured.update(copy.deepcopy(kwargs["evidence"]))
            return original(sid, record, receipt, **kwargs)

        with patch.object(self.app.store, "save_lab9_turn", side_effect=intercept):
            self.ask(3)
        for field in ("generated", "displayed"):
            evidence = copy.deepcopy(captured)
            evidence[field] = "The maintenance records remain restricted."
            evidence["history"][-1]["content"] = evidence["displayed"]
            receipt = server._lab9_receipt(**evidence)
            self.assertFalse(receipt["verification_outcome"])
            self.assertEqual(receipt["verification_reason"], "disclosure_not_verified")

    def test_actual_disclosures_without_two_refusals_still_fail(self):
        for count in (0, 1):
            self.app.reset(self.sid, 9)
            if count:
                self.ask(0)
            result = self.ask(3)
            self.assertFalse(result["lab"]["last_result"]["triggered"])
            self.assertEqual(self.record()["lab9_verification_receipts"][-1]["verification_reason"],
                             "insufficient_verified_refusals")

    def test_turn4_then_turn5_award_only_once_and_preserve_completion(self):
        self.progress()
        result = self.ask(3)
        self.assertTrue(result["lab"]["last_result"]["triggered"])
        flag = result["lab"]["flag"]
        awarded = self.app.submit_flag(self.sid, 9, {"flag": flag})
        self.assertEqual(awarded["flag_submission"]["points_awarded"], 500)
        self.assertTrue(self.ask(4)["lab"]["last_result"]["triggered"])
        duplicate = self.app.submit_flag(self.sid, 9, {"flag": flag})
        self.assertEqual(duplicate["flag_submission"]["points_awarded"], 0)
        self.assertEqual(duplicate["summary"]["score"], 500)
        reopened = server.Application(Path(self.directory.name))
        self.assertTrue(reopened.store.get(self.sid)[1]["labs"]["9"]["completed"])


if __name__ == "__main__":
    unittest.main()
