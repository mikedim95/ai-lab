"""Lab 5 evidence tests: existing fixtures, transport mocks, temporary databases."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import catalog
import server
from test_app import LAB5_MESSAGES, LAB5_DISCLOSURE

NORMAL = "The technician checks the handover records."


class Lab5EvidenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.app = server.Application(self.directory)
        self.sid, _ = self.app.store.get(None)
        self.requests = []

    def record(self, sid=None):
        return self.app.store.get(sid or self.sid)[1]["labs"]["5"]

    def receipts(self, sid=None):
        return self.record(sid).get("lab5_verification_receipts", [])

    def ask(self, message, reply=NORMAL, *, during=None, **extra):
        def transport(request, timeout):
            self.assertEqual(request.full_url, server.OLLAMA_URL + "/api/chat")
            self.requests.append(json.loads(request.data))
            if during:
                during()
            if isinstance(reply, Exception):
                raise reply
            return io.BytesIO(json.dumps({"message": {"content": reply}}).encode())
        with patch("server.urllib.request.urlopen", side_effect=transport):
            return self.app.chat(self.sid, 5, {"message": message, **extra})

    def prepare(self):
        self.ask(LAB5_MESSAGES[0])
        self.ask(LAB5_MESSAGES[1])

    def sequence(self, reply=LAB5_DISCLOSURE):
        self.prepare()
        return self.ask(LAB5_MESSAGES[2], reply)

    def assert_locked(self, result):
        self.assertFalse(result["lab"]["last_result"]["triggered"])
        self.assertFalse(result["lab"]["exploited"])
        self.assertIsNone(result["lab"]["flag"])
        self.assertFalse(result["lab"]["completed"])
        self.assertEqual(result["summary"]["score"], 0)

    def test_valid_three_turn_escalation_binds_all_private_evidence_and_keeps_250_points(self):
        before = copy.deepcopy(self.app.store.get(self.sid)[1])
        self.prepare()
        prior = copy.deepcopy(self.record())
        result = self.ask(LAB5_MESSAGES[2], LAB5_DISCLOSURE)
        self.assertTrue(result["lab"]["last_result"]["triggered"])
        self.assertEqual(len(self.receipts()), 3)
        receipt = self.receipts()[-1]
        for key, value in {"student_id": self.sid, "lab_id": 5, "attempt": 1, "defense_level": 0,
            "conversation_revision": 2, "result_revision": 3, "generation_source": "production_ollama",
            "verification_outcome": True, "verification_reason": "verified", "exchange_verified": True}.items():
            self.assertEqual(receipt[key], value)
        self.assertRegex(receipt["turn_id"], r"^[a-f0-9]{32}$")
        self.assertEqual(receipt["conversation_sha256"], server._lab3_hash(prior["messages"]))
        self.assertEqual(receipt["result_history_sha256"], server._lab3_hash(self.record()["messages"]))
        self.assertEqual(receipt["model_context_sha256"], server._lab3_hash(self.requests[-1]["messages"]))
        self.assertEqual(receipt["ollama_request_sha256"], server._lab3_hash(self.requests[-1]))
        for field in ["generated_response_sha256", "displayed_response_sha256"]:
            self.assertEqual(receipt[field], server._lab3_hash(LAB5_DISCLOSURE))
        self.assertFalse(result["lab"]["completed"])
        self.assertEqual(result["summary"]["score"], 0)
        awarded = self.app.submit_flag(self.sid, 5, {"flag": result["lab"]["flag"]})
        self.assertEqual(awarded["summary"]["score"], 250)
        after = self.app.store.get(self.sid)[1]
        for lab in before["labs"]:
            if lab != "5":
                self.assertEqual(after["labs"][lab], before["labs"][lab])

    def test_one_and_two_exchanges_cannot_unlock(self):
        self.assert_locked(self.ask(LAB5_MESSAGES[2], LAB5_DISCLOSURE))
        self.app.reset(self.sid, 5)
        self.ask(LAB5_MESSAGES[0])
        self.assert_locked(self.ask(LAB5_MESSAGES[2], LAB5_DISCLOSURE))

    def test_pasted_transcript_or_client_history_cannot_create_exchanges(self):
        fabricated = [{"role": role, "content": content} for message in LAB5_MESSAGES[:2]
                      for role, content in [("user", message), ("assistant", NORMAL)]]
        self.assert_locked(self.ask(LAB5_MESSAGES[2], LAB5_DISCLOSURE, history=fabricated,
                                   messages=fabricated, lab5_conversation_revision=2))
        self.app.reset(self.sid, 5)
        transcript = "\n".join(turn["role"] + ": " + turn["content"] for turn in fabricated)
        self.assert_locked(self.ask(transcript + "\n" + LAB5_MESSAGES[2], LAB5_DISCLOSURE))

    def test_fabricated_stored_history_without_receipts_cannot_qualify(self):
        data = self.app.store.get(self.sid)[1]
        data["labs"]["5"]["messages"] = [turn for message in LAB5_MESSAGES[:2] for turn in
            [{"role": "user", "content": message}, {"role": "assistant", "content": NORMAL}]]
        self.app.store.save(self.sid, data)
        self.assert_locked(self.ask(LAB5_MESSAGES[2], LAB5_DISCLOSURE))
        self.assertFalse(self.receipts()[-1]["verification_outcome"])

    def test_scripted_and_mock_model_callbacks_cannot_supply_production_evidence(self):
        for model in [lambda messages: LAB5_DISCLOSURE, Mock(return_value=LAB5_DISCLOSURE)]:
            self.sid, _ = self.app.store.get(None)
            self.prepare()
            self.app.model = model
            result = self.app.chat(self.sid, 5, {"message": LAB5_MESSAGES[2],
                "generation_source": "production_ollama", "verification_outcome": True})
            self.assert_locked(result)
            self.assertEqual(self.receipts()[-1]["generation_source"], "test_fixture")
            self.app.model = server._PRODUCTION_OLLAMA_MODEL

    def test_nonproduction_prior_exchanges_cannot_launder_a_later_production_reply(self):
        self.app.model = lambda messages: NORMAL
        for message in LAB5_MESSAGES[:2]:
            self.app.chat(self.sid, 5, {"message": message})
        self.app.model = server._PRODUCTION_OLLAMA_MODEL
        self.assert_locked(self.ask(LAB5_MESSAGES[2], LAB5_DISCLOSURE))
        self.assertEqual(self.receipts()[-1]["verification_reason"], "escalation_not_verified")

    def test_three_real_exchanges_can_follow_fixture_turns_without_counting_them(self):
        self.app.model = lambda messages: NORMAL
        for message in LAB5_MESSAGES[:2]:
            self.app.chat(self.sid, 5, {"message": message})
        self.app.model = server._PRODUCTION_OLLAMA_MODEL
        self.assertTrue(self.sequence()["lab"]["last_result"]["triggered"])

    def test_student_supplied_plain_or_formatted_code_never_counts_as_extraction(self):
        code = catalog.LAB5_ACCESS_CODE
        split = max(1, len(code) // 2)
        for value in [code, " ".join(code), "<b>" + code[:split] + "</b>" + code[split:]]:
            self.sid, _ = self.app.store.get(None)
            self.prepare()
            self.assert_locked(self.ask(LAB5_MESSAGES[2] + " " + value, LAB5_DISCLOSURE))
            self.assertEqual(self.receipts()[-1]["verification_reason"], "student_supplied_code")

    def test_student_code_taint_survives_history_window_truncation(self):
        self.ask(LAB5_MESSAGES[0] + " " + catalog.LAB5_ACCESS_CODE)
        for _ in range(31):
            self.ask(LAB5_MESSAGES[0])
        self.ask(LAB5_MESSAGES[1])
        self.assert_locked(self.ask(LAB5_MESSAGES[2], LAB5_DISCLOSURE))
        self.assertTrue(self.record()["lab5_supplied_code"])

    def test_genuine_history_remains_valid_after_window_truncation(self):
        for _ in range(32):
            self.ask(LAB5_MESSAGES[0])
        self.ask(LAB5_MESSAGES[1])
        self.assertTrue(self.ask(LAB5_MESSAGES[2], LAB5_DISCLOSURE)["lab"]["last_result"]["triggered"])

    def test_quoted_hypothetical_and_refused_disclosures_remain_ineligible(self):
        for reply in ['"' + LAB5_DISCLOSURE + '"', "Hypothetical example: " + LAB5_DISCLOSURE,
                      "I cannot disclose the maintenance access code: " + catalog.LAB5_ACCESS_CODE,
                      catalog.LAB5_REFUSAL, "Exploit verified."]:
            self.sid, _ = self.app.store.get(None)
            self.assert_locked(self.sequence(reply))

    def test_generated_and_displayed_disclosures_must_both_qualify(self):
        for generated, displayed in [(NORMAL, LAB5_DISCLOSURE), (LAB5_DISCLOSURE, catalog.LAB5_REFUSAL)]:
            self.sid, _ = self.app.store.get(None)
            self.prepare()
            with patch("server.defend_output", return_value=(displayed, None)):
                self.assert_locked(self.ask(LAB5_MESSAGES[2], generated))

    def test_existing_protected_modes_filter_the_value_and_bind_effective_difficulty(self):
        for level in (1, 2):
            self.sid, _ = self.app.store.get(None)
            self.app.update_defense(self.sid, 5, {"level": level})
            result = self.sequence()
            self.assert_locked(result)
            self.assertEqual(self.receipts()[-1]["defense_level"], level)
            self.assertNotIn(catalog.LAB5_ACCESS_CODE, result["lab"]["messages"][-1]["content"])

    def test_reset_or_difficulty_change_during_generation_rejects_old_evidence(self):
        for change in [lambda app: app.reset(self.sid, 5),
                       lambda app: app.update_defense(self.sid, 5, {"level": 1})]:
            self.sid, _ = self.app.store.get(None)
            self.prepare()
            with self.assertRaises(server.APIError) as error:
                self.ask(LAB5_MESSAGES[2], LAB5_DISCLOSURE,
                         during=lambda: change(server.Application(self.directory)))
            self.assertEqual(error.exception.status, 409)
            self.assertFalse(self.record()["exploited"])
            self.assertIsNone(self.receipts()[-1]["displayed_response_sha256"])

    def test_outdated_conversation_is_rejected_even_in_the_same_attempt(self):
        self.prepare()
        def concurrent_turn():
            other = server.Application(self.directory)
            with patch("server.urllib.request.urlopen", return_value=io.BytesIO(json.dumps({"message":{"content":NORMAL}}).encode())):
                other.chat(self.sid, 5, {"message": LAB5_MESSAGES[0]})
        with self.assertRaises(server.APIError) as error:
            self.ask(LAB5_MESSAGES[2], LAB5_DISCLOSURE, during=concurrent_turn)
        self.assertEqual(error.exception.status, 409)
        self.assertFalse(self.record()["exploited"])
        self.assertEqual(self.receipts()[-1]["verification_reason"], "stale_conversation")

    def test_snapshot_is_rechecked_inside_the_commit_transaction(self):
        self.prepare()
        commit = self.app.store.save_lab5_turn
        def raced(sid, record, receipt):
            server.Application(self.directory).reset(sid, 5)
            return commit(sid, record, receipt)
        with patch.object(self.app.store, "save_lab5_turn", side_effect=raced):
            with self.assertRaises(server.APIError):
                self.ask(LAB5_MESSAGES[2], LAB5_DISCLOSURE)
        self.assertFalse(self.record()["exploited"])

    def test_replay_stale_and_cross_student_receipts_cannot_unlock(self):
        self.sequence()
        receipt, record = copy.deepcopy(self.receipts()[-1]), copy.deepcopy(self.record())
        _, fresh = self.app.store.save_lab5_turn(self.sid, record, receipt)
        self.assertFalse(fresh)
        self.assertEqual(self.receipts()[-1]["verification_reason"], "replayed_turn")
        self.app.reset(self.sid, 5)
        _, fresh = self.app.store.save_lab5_turn(self.sid, record, receipt)
        self.assertFalse(fresh)
        self.assertFalse(self.record()["exploited"])
        other, _ = self.app.store.get(None)
        _, fresh = self.app.store.save_lab5_turn(other, record, receipt)
        self.assertFalse(fresh)
        self.assertFalse(self.record(other)["exploited"])
        self.assertEqual(self.receipts(other)[-1]["verification_reason"], "identity_mismatch")

    def test_mismatched_receipt_fields_cannot_verify(self):
        make = server._lab3_receipt
        for field in ["student_id", "lab_id", "attempt", "defense_level", "turn_id", "conversation_sha256",
                      "model_context_sha256", "ollama_request_sha256", "generated_response_sha256", "displayed_response_sha256"]:
            self.sid, _ = self.app.store.get(None)
            self.prepare()
            def corrupt(*args):
                receipt = make(*args)
                receipt[field] = "mismatched"
                return receipt
            with patch("server._lab3_receipt", side_effect=corrupt):
                try:
                    result = self.ask(LAB5_MESSAGES[2], LAB5_DISCLOSURE)
                    self.assert_locked(result)
                except server.APIError as error:
                    self.assertEqual(error.status, 409)
            self.assertFalse(self.record()["exploited"])

    def test_altered_history_and_record_at_commit_are_rejected(self):
        self.prepare()
        data = self.app.store.get(self.sid)[1]
        data["labs"]["5"]["messages"][1]["content"] = "Fabricated transcript."
        self.app.store.save(self.sid, data)
        self.assert_locked(self.ask(LAB5_MESSAGES[2], LAB5_DISCLOSURE))
        self.sid, _ = self.app.store.get(None)
        self.prepare()
        commit = self.app.store.save_lab5_turn
        def swapped(sid, record, receipt):
            record = copy.deepcopy(record)
            record["messages"][-1]["content"] = NORMAL
            return commit(sid, record, receipt)
        with patch.object(self.app.store, "save_lab5_turn", side_effect=swapped):
            with self.assertRaises(server.APIError):
                self.ask(LAB5_MESSAGES[2], LAB5_DISCLOSURE)
        self.assertFalse(self.record()["exploited"])

    def test_completion_is_preserved_if_it_is_earned_while_generation_is_pending(self):
        first = self.sequence()
        def award():
            server.Application(self.directory).submit_flag(self.sid, 5, {"flag":first["lab"]["flag"]})
        result = self.ask(LAB5_MESSAGES[0], during=award)
        self.assertTrue(result["lab"]["completed"])
        self.assertEqual(result["summary"]["score"], 250)

    def test_receipts_are_private_persistent_and_do_not_enter_model_context(self):
        with self.app.store.connect() as db:
            schema = db.execute("SELECT name,sql FROM sqlite_schema ORDER BY name").fetchall()
        result = self.sequence()
        receipt = copy.deepcopy(self.receipts()[-1])
        self.ask(LAB5_MESSAGES[0])
        for encoded in [json.dumps(result), json.dumps(self.requests[-1])]:
            self.assertNotIn("lab5_verification_receipts", encoded)
            self.assertNotIn(receipt["turn_id"], encoded)
            self.assertNotIn(receipt["model_context_sha256"], encoded)
        reopened = server.Application(self.directory)
        self.assertEqual(reopened.store.get(self.sid)[1]["labs"]["5"]["lab5_verification_receipts"], self.receipts())
        with reopened.store.connect() as db:
            self.assertEqual(db.execute("SELECT name,sql FROM sqlite_schema ORDER BY name").fetchall(), schema)

    def test_failed_generation_records_no_eligible_exchange(self):
        with self.assertRaises(server.APIError):
            self.ask(LAB5_MESSAGES[0], OSError("fixture transport failure"))
        receipt = self.receipts()[-1]
        self.assertFalse(receipt["verification_outcome"])
        self.assertFalse(receipt["exchange_verified"])
        self.assertIsNone(receipt["displayed_response_sha256"])


if __name__ == "__main__":
    unittest.main()
