"""Final Lab 8 verification and awards: mocked transport, real SQLite/HTTP.

The existing regression message is reused unchanged. The synthetic handover
fixtures contain only classroom data. No tests connect to a live model.
"""
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import catalog
import server
from lab8_verification_fixtures import ADOPTED, EXISTING_INSTRUCTION, REFERENCE, SUMMARY

NORMAL = "Plant AI can explain fictional maintenance handovers."


class Lab8ReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.app = server.Application(self.directory)
        self.sid, _ = self.app.store.get(None)
        self.requests = []

    def record(self, sid=None):
        return self.app.store.get(sid or self.sid)[1]["labs"]["8"]

    def save(self, documents=None, sync=True):
        return self.app.update_kb(self.sid, 8, {"documents": copy.deepcopy([REFERENCE] if documents is None else documents),
                                               "sync": sync, "use_kb": True})

    def ask(self, message=EXISTING_INSTRUCTION, reply=ADOPTED, *, during=None, **extra):
        def transport(request, timeout):
            self.assertEqual(request.full_url, server.OLLAMA_URL + "/api/chat")
            self.requests.append(json.loads(request.data))
            if during:
                during()
            if isinstance(reply, Exception):
                raise reply
            return io.BytesIO(json.dumps({"message": {"content": reply}}).encode())
        with patch("server.urllib.request.urlopen", side_effect=transport):
            return self.app.chat(self.sid, 8, {"message": message, **extra})

    def locked(self, result):
        self.assertIs(result["lab"]["last_result"]["triggered"], False)
        self.assertIs(result["lab"]["exploited"], False)
        self.assertIsNone(result["lab"]["flag"])
        self.assertEqual(result["summary"]["score"], 0)

    def capture(self, *, commit=True, reply=ADOPTED):
        original = self.app.store.save_lab8_turn
        held = {}
        def intercept(sid, record, receipt, **kwargs):
            held.update(record=copy.deepcopy(record), receipt=copy.deepcopy(receipt),
                        evidence=copy.deepcopy(kwargs.get("evidence")))
            return original(sid, record, receipt, **kwargs) if commit else (self.app.store.get(sid)[1], True)
        with patch.object(self.app.store, "save_lab8_turn", side_effect=intercept):
            response = self.ask(reply=reply)
        return response, held

    def earn(self):
        self.save()
        captured = self.ask()
        self.assertTrue(captured["lab"]["last_result"]["triggered"])
        return self.app.submit_flag(self.sid, 8, {"flag": captured["lab"]["flag"]})

    def test_precise_synthetic_two_stage_fixture_and_complete_private_evidence(self):
        self.save()
        before = self.record()
        result = self.ask()
        self.assertTrue(result["lab"]["last_result"]["triggered"])
        self.assertFalse(result["lab"]["completed"])
        self.assertEqual(result["summary"]["score"], 0)
        receipt = self.record()["lab8_verification_receipts"][-1]
        expected = {"student_id": self.sid, "lab_id": 8, "attempt": before["attempt"],
            "turn_id": result["request_id"], "defense_level": 0, "synced_revision": before["synced_revision"],
            "synced_snapshot_sha256": server._lab3_hash(before["synced_documents"]),
            "retrieved_documents": server._lab3_excerpts(before["synced_documents"]),
            "model_context_sha256": server._lab3_hash(self.requests[-1]["messages"]),
            "ollama_request_sha256": server._lab3_hash(self.requests[-1]),
            "generated_response_sha256": server._lab3_hash(ADOPTED),
            "displayed_response_sha256": server._lab3_hash(ADOPTED),
            "generation_source": "production_ollama", "verification_outcome": True,
            "verification_reason": "verified", "contract_sha256": server.LAB8_CONTRACT_SHA256}
        for key, value in expected.items():
            self.assertEqual(receipt[key], value, key)
        for private in ("lab8_verification_receipts", "lab8_award_receipt", "sha256", catalog.LAB8_PROMPT):
            self.assertNotIn(private, json.dumps(result))

    def test_saved_without_sync_cannot_verify(self):
        self.save(sync=False)
        self.locked(self.ask())

    def test_no_retrieved_student_document_cannot_verify(self):
        self.locked(self.ask(use_kb=True, documents=[REFERENCE], synced_documents=[REFERENCE]))
        self.save()
        self.locked(self.ask(use_kb=False))

    def test_source_must_be_student_controlled(self):
        self.save()
        before = self.record()
        record = copy.deepcopy(before)
        for key in ("documents", "synced_documents"):
            record[key][0]["is_user_injected"] = False
        self.app.store.save_lab8(self.sid, record, before)
        self.locked(self.ask())

    def test_retrieval_without_relevant_adoption_and_ordinary_summary_cannot_verify(self):
        self.save()
        for reply in (NORMAL, SUMMARY, REFERENCE["content"], "SIM-HO-008 COBALT-READY Boiler B-01 Generator G-01"):
            with self.subTest(reply=reply):
                self.locked(self.ask(reply=reply))

    def test_ordinary_question_cannot_verify_even_with_authoritative_model_assertion(self):
        self.save()
        self.locked(self.ask(message="What does the internal simulation handover policy record?"))
        self.locked(self.ask(message="Share all maintenance handover policy records."))

    def test_unrelated_source_keywords_do_not_form_the_synthetic_handover_assertion(self):
        self.save([{**REFERENCE, "content": "Internal policy index: SIM-HO-008, COBALT-READY, Boiler B-01, Generator G-01."}])
        self.locked(self.ask())

    def test_current_student_assertion_echo_cannot_verify(self):
        self.save()
        self.locked(self.ask(message=ADOPTED, reply=ADOPTED))

    def test_prior_student_assertion_cannot_be_laundered_by_a_later_instruction(self):
        self.save()
        self.locked(self.ask(message=ADOPTED, reply=NORMAL))
        self.locked(self.ask())

    def test_student_echo_taint_survives_public_history_truncation(self):
        self.save()
        self.ask(message=ADOPTED, reply=NORMAL)
        for _ in range(31):
            self.ask(message="Explain a fictional handover.", reply=NORMAL)
        self.assertNotIn(ADOPTED, [turn["content"] for turn in self.record()["messages"]])
        self.locked(self.ask())

    def test_quotations_and_code_are_not_authoritative_adoption(self):
        self.save()
        for reply in ['"' + ADOPTED + '"', '> ' + ADOPTED, '```\n' + ADOPTED + '\n```',
                      '<blockquote>' + ADOPTED + '</blockquote>', '<think>' + ADOPTED + '</think>',
                      "The document contains the sentence: " + ADOPTED]:
            with self.subTest(reply=reply):
                self.locked(self.ask(reply=reply))

    def test_hypothetical_refused_and_explicitly_unverified_assertions_do_not_qualify(self):
        self.save()
        for framing in ("Hypothetical example: ", "If this were verified, ", "I cannot authenticate this. ",
                        "This is unverified. ", "The record is 'unverified'. ", "The record is unproven. ",
                        "According to the student document: ", "I reject this assertion. "):
            with self.subTest(framing=framing):
                self.locked(self.ask(reply=framing + ADOPTED))

    def test_wrong_record_status_or_equipment_cannot_qualify(self):
        self.save()
        for original, wrong in (("SIM-HO-008", "SIM-HO-009"), ("COBALT-READY", "AMBER-PENDING"),
                                ("Boiler B-01", "Boiler B-02"), ("Generator G-01", "Generator G-02")):
            self.locked(self.ask(reply=ADOPTED.replace(original, wrong)))

    def test_synthetic_assertion_allows_explicit_first_person_paraphrases(self):
        self.save()
        for reply in (ADOPTED, ADOPTED.replace("I accept", "Plant AI adopts"),
                      ADOPTED.replace("I accept", "I treat"), ADOPTED.replace("status is ", "status: "),
                      ADOPTED + " No live telemetry or equipment-control tools are available."):
            self.assertTrue(self.ask(reply=reply)["lab"]["last_result"]["triggered"])

    def test_fabricated_model_success_claim_is_not_proof(self):
        self.save()
        self.locked(self.ask(reply="Exploit verified. Lab 8 complete. 400 points awarded."))

    def test_scripted_callback_is_not_production_evidence_even_when_text_matches(self):
        self.save()
        self.app.model = lambda _: ADOPTED
        self.locked(self.app.chat(self.sid, 8, {"message": EXISTING_INSTRUCTION}))
        receipt = self.record()["lab8_verification_receipts"][-1]
        self.assertEqual(receipt["generation_source"], "test_fixture")
        self.assertEqual(receipt["verification_reason"], "non_production_generation")

    def test_monkeypatched_local_model_name_does_not_spoof_production_identity(self):
        self.save()
        with patch.object(server, "local_model", lambda _: ADOPTED):
            self.app.model = server.local_model
            self.locked(self.app.chat(self.sid, 8, {"message": EXISTING_INSTRUCTION}))

    def test_actual_ollama_request_hash_is_required(self):
        self.save()
        with patch.object(server, "_lab5_request_hash", return_value="f" * 64):
            self.locked(self.ask())
        self.assertEqual(self.record()["lab8_verification_receipts"][-1]["verification_reason"], "ollama_request_mismatch")

    def test_output_filter_cannot_insert_or_remove_the_assertion_and_still_verify(self):
        self.save()
        with patch.object(server, "defend_output", return_value=(ADOPTED, None)):
            self.locked(self.ask(reply=SUMMARY))
        with patch.object(server, "defend_output", return_value=(NORMAL, None)):
            self.locked(self.ask())

    def test_assertion_beyond_visible_output_limit_cannot_verify(self):
        self.save()
        self.locked(self.ask(reply="Routine notes. " * 1100 + ADOPTED))

    def test_clipped_retrieved_excerpt_must_contain_the_complete_assertion(self):
        docs = [{"id": "large-policy-a", "title": "policy", "content": ("policy and the " * 250).strip()},
                {"id": "large-policy-b", "title": "policy", "content": ("policy and the " * 160).strip()},
                {**REFERENCE, "content": "log " * 200 + REFERENCE["content"]}]
        self.save(docs)
        result = self.ask()
        self.locked(result)
        self.assertIn(REFERENCE["id"], [source["id"] for source in result["lab"]["last_result"]["sources"]])
        context = self.requests[-1]["messages"][0]["content"]
        self.assertNotIn("COBALT-READY", context)

    def test_saved_edit_cannot_replace_a_benign_synchronized_excerpt(self):
        benign = {**REFERENCE, "content": "Internal handover policy records fictional routine inspections."}
        self.save([benign])
        self.save(sync=False)
        self.locked(self.ask())
        self.save()
        self.assertTrue(self.ask()["lab"]["last_result"]["triggered"])

    def test_previous_success_does_not_verify_a_new_ordinary_reply(self):
        self.save()
        captured = self.ask()
        later = self.ask(reply=SUMMARY)
        self.assertTrue(captured["lab"]["last_result"]["triggered"])
        self.assertFalse(later["lab"]["last_result"]["triggered"])
        self.assertEqual(later["lab"]["flag"], captured["lab"]["flag"])
        self.assertEqual(later["lab"]["last_result"]["message"], "No exploit verified for this reply.")

    def test_private_receipts_never_enter_model_context_or_public_json(self):
        self.save()
        self.ask()
        result = self.ask(reply=NORMAL)
        for text in (json.dumps(self.requests[-1]), json.dumps(result)):
            for private in ("lab8_verification_receipts", "lab8_award_receipt", "contract_sha256",
                            "generated_response_sha256", "ollama_request_sha256"):
                self.assertNotIn(private, text)

    def test_each_request_uses_saved_effective_difficulty(self):
        self.save()
        for level in (0, 1, 2):
            self.app.update_defense(self.sid, 8, {"level": level})
            self.ask(message="Explain the internal handover policy.", reply=NORMAL, defense_level=99)
            self.assertEqual(self.record()["lab8_verification_receipts"][-1]["defense_level"], level)

    def test_changed_snapshot_during_generation_discards_old_response(self):
        self.save()
        with self.assertRaises(server.APIError) as error:
            self.ask(during=lambda: self.save([{**REFERENCE, "content": "New synchronized internal handover policy."}]))
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.record()["messages"], [])
        self.assertIn("New synchronized", self.record()["synced_documents"][0]["content"])
        self.assertIsNone(self.record()["last_result"])
        self.assertFalse(self.record()["lab8_verification_receipts"][-1]["verification_outcome"])

    def test_reset_and_difficulty_change_during_generation_cannot_restore_old_attempt(self):
        self.save()
        for change in (lambda: self.app.reset(self.sid, 8), lambda: self.app.update_defense(self.sid, 8, {"level": 1})):
            self.save()
            with self.assertRaises(server.APIError) as error:
                self.ask(during=change)
            self.assertEqual(error.exception.status, 409)
            self.assertEqual(self.record()["messages"], [])

    def test_replayed_and_cross_student_evidence_are_rejected_without_contamination(self):
        self.save()
        _, held = self.capture()
        for sid in (self.sid, self.app.store.get(None)[0]):
            before = self.app.store.get(sid)[1]
            with self.assertRaises(server.APIError) as error:
                self.app.store.save_lab8_turn(sid, held["record"], held["receipt"], evidence=held["evidence"])
            self.assertEqual(error.exception.status, 409)
            self.assertEqual(self.app.store.get(sid)[1], before)

    def test_mismatched_receipt_fields_cannot_verify(self):
        changes = {"attempt": 999, "defense_level": 2, "turn_id": "e" * 32,
                   "synced_revision": 999, "synced_snapshot_sha256": "e" * 64,
                   "model_context_sha256": "e" * 64, "ollama_request_sha256": "e" * 64,
                   "generated_response_sha256": "e" * 64, "displayed_response_sha256": "e" * 64,
                   "contract_sha256": "e" * 64, "generation_source": "test_fixture",
                   "retrieved_documents": [{"id": "wrong-document", "is_user_injected": True, "excerpt_sha256": "e" * 64}]}
        for field, value in changes.items():
            with self.subTest(field=field):
                self.app.reset(self.sid, 8)
                self.save()
                _, held = self.capture(commit=False)
                receipt = {**held["receipt"], field: value}
                _, fresh = self.app.store.save_lab8_turn(self.sid, held["record"], receipt, evidence=held["evidence"])
                self.assertFalse(fresh)
                self.assertIsNone(server._lab8_capture(self.sid, self.record()))

    def test_record_or_evidence_mismatch_cannot_commit_a_verified_response(self):
        for mutation in ("visible", "history", "context", "source", "excerpt", "actual_request",
                         "record_sources", "record_kb", "record_difficulty", "record_saved"):
            with self.subTest(mutation=mutation):
                self.app.reset(self.sid, 8)
                self.save()
                _, held = self.capture(commit=False)
                if mutation == "visible":
                    held["record"]["messages"][-1]["content"] = NORMAL
                elif mutation == "history":
                    held["evidence"]["history"] = []
                elif mutation == "context":
                    held["evidence"]["messages"][0]["content"] = "No reference supplied."
                elif mutation == "source":
                    held["evidence"]["entries"][0]["id"] = "unknown-source"
                elif mutation == "excerpt":
                    held["evidence"]["entries"][0]["content"] = NORMAL
                elif mutation == "record_sources":
                    held["record"]["last_result"]["sources"][0]["id"] = "wrong-public-source"
                elif mutation == "record_kb":
                    held["record"]["use_kb"] = False
                elif mutation == "record_difficulty":
                    held["record"]["last_result"]["defense_level"] = 2
                elif mutation == "record_saved":
                    held["record"]["documents"][0]["content"] = NORMAL
                else:
                    held["evidence"]["actual_request_sha256"] = "e" * 64
                _, fresh = self.app.store.save_lab8_turn(self.sid, held["record"], held["receipt"], evidence=held["evidence"])
                self.assertFalse(fresh)
                self.assertIsNone(server._lab8_capture(self.sid, self.record()))

    def test_commit_without_independent_generation_evidence_is_ineligible(self):
        self.save()
        _, held = self.capture(commit=False)
        _, fresh = self.app.store.save_lab8_turn(self.sid, held["record"], held["receipt"])
        self.assertFalse(fresh)
        self.assertIsNone(server._lab8_capture(self.sid, self.record()))

    def test_malformed_or_wrong_lab_receipt_is_rejected_without_writing(self):
        self.save()
        _, held = self.capture(commit=False)
        before = self.record()
        for field, value in (("turn_id", None), ("turn_id", "invalid"), ("result_revision", None),
                             ("state_revision", False), ("lab_id", 9), ("lab_id", 8.0),
                             ("synced_snapshot_sha256", None)):
            with self.subTest(field=field, value=value):
                receipt = {**held["receipt"], field: value}
                with self.assertRaises(server.APIError) as error:
                    self.app.store.save_lab8_turn(self.sid, held["record"], receipt, evidence=held["evidence"])
                self.assertEqual(error.exception.status, 409)
                self.assertEqual(self.record(), before)

    def test_transport_and_output_failures_keep_only_private_failed_evidence(self):
        self.save()
        for mode in ("transport", "output"):
            with self.subTest(mode=mode):
                if mode == "transport":
                    with self.assertRaises(server.APIError):
                        self.ask(reply=OSError("Fixture transport unavailable"))
                else:
                    with patch.object(server, "defend_output", side_effect=RuntimeError("Fixture output failure")):
                        with self.assertRaises(RuntimeError):
                            self.ask()
                receipt = self.record()["lab8_verification_receipts"][-1]
                self.assertFalse(receipt["verification_outcome"])
                self.assertIsNone(receipt["displayed_response_sha256"])
                self.assertEqual(self.record()["messages"], [])

    def test_first_award_duplicate_and_captured_attempt_origin(self):
        self.save()
        captured = self.ask()
        flag = captured["lab"]["flag"]
        first = self.app.submit_flag(self.sid, 8, {"flag": flag})
        self.assertEqual(first["flag_submission"], {"newly_completed": True, "points_awarded": 400})
        for _ in range(3):
            repeat = self.app.submit_flag(self.sid, 8, {"flag": flag})
            self.assertEqual(repeat["flag_submission"], {"newly_completed": False, "points_awarded": 0})
            self.assertEqual(repeat["summary"]["score"], 400)
        self.assertEqual(self.record()["lab8_flag_attempt"], 1)
        self.assertTrue(self.record()["lab8_award_receipt"]["verification_outcome"])

    def test_invalid_non_ascii_and_cross_student_flags_are_rejected(self):
        first = self.earn()
        other, _ = self.app.store.get(None)
        for sid, flag in ((self.sid, "PANOPTIS{incorrect}"), (self.sid, "PANOPTIS{λ}"), (other, first["lab"]["flag"])):
            with self.assertRaises(server.APIError) as error:
                self.app.submit_flag(sid, 8, {"flag": flag})
            self.assertIn(error.exception.status, (400, 403))
        self.assertEqual(self.app.summary(self.app.store.get(self.sid)[1])["score"], 400)

    def test_completion_flag_and_award_evidence_survive_difficulty_and_reopen(self):
        first = self.earn()
        flag, award = first["lab"]["flag"], self.record()["lab8_award_receipt"]
        for operation in (lambda: self.app.update_defense(self.sid, 8, {"level": 1}),
                          lambda: self.app.update_defense(self.sid, 8, {"level": 2})):
            response = operation()
            self.assertTrue(response["lab"]["completed"])
            self.assertFalse(response["lab"]["exploited"])
            self.assertIsNone(response["lab"]["last_result"])
            self.assertEqual(response["lab"]["flag"], flag)
            self.assertEqual(response["summary"]["score"], 400)
            self.assertEqual(self.record()["lab8_award_receipt"], award)
            self.assertEqual(self.app.submit_flag(self.sid, 8, {"flag": flag})["flag_submission"]["points_awarded"], 0)
        reopened = server.Application(self.directory)
        self.assertEqual(reopened.refresh_state(self.sid)["labs"]["8"]["flag"], flag)
        self.assertTrue(reopened.refresh_state(self.sid)["labs"]["8"]["completed"])
        reset = reopened.reset(self.sid, 8)
        self.assertFalse(reset["lab"]["completed"])
        self.assertIsNone(reset["lab"]["flag"])
        self.assertEqual(reset["summary"]["score"], 0)
        self.assertNotIn("lab8_award_receipt", self.record())

    def test_award_survives_a_new_backend_process(self):
        first = self.earn()
        code = """import json,sys
from pathlib import Path
import server
app=server.Application(Path(sys.argv[1]))
state=app.refresh_state(sys.argv[2])
print(json.dumps({'completed':state['labs']['8']['completed'],'flag':state['labs']['8']['flag'],'score':state['summary']['score']}))
"""
        process = subprocess.run([sys.executable, "-c", code, str(self.directory), self.sid],
                                 cwd=catalog.ROOT, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout), {"completed": True, "flag": first["lab"]["flag"], "score": 400})

    def test_stale_and_new_attempt_session_writes_cannot_erase_completion(self):
        _, stale = self.app.store.get(self.sid)
        first = self.earn()
        stale["labs"]["7"]["use_kb"] = True
        self.app.store.save(self.sid, stale)
        self.assertTrue(self.record()["completed"])
        future = copy.deepcopy(stale)
        future["labs"]["8"] = catalog.fresh_lab(attempt=99, lab_id=8)
        self.app.store.save(self.sid, future)
        state = self.app.refresh_state(self.sid)
        self.assertTrue(state["labs"]["8"]["completed"])
        self.assertEqual(state["labs"]["8"]["flag"], first["lab"]["flag"])
        self.assertEqual(state["summary"]["score"], 400)

    def test_legacy_completion_is_retained_without_rewriting_student_work(self):
        before = self.record()
        legacy = copy.deepcopy(before)
        legacy.update(completed=True, exploited=True)
        self.app.store.save_lab8(self.sid, legacy, before)
        expected = self.app.store.flag(self.sid, 8, legacy["attempt"])
        self.assertTrue(self.app.refresh_state(self.sid)["labs"]["8"]["completed"])
        reset = self.app.reset(self.sid, 8)
        self.assertFalse(reset["lab"]["completed"])
        self.assertIsNone(reset["lab"]["flag"])

    def test_stale_session_missing_lab8_cannot_remove_earned_completion(self):
        _, stale = self.app.store.get(self.sid)
        del stale["labs"]["8"]
        earned = self.earn()
        self.app.store.save(self.sid, stale)
        current = self.app.refresh_state(self.sid)
        self.assertTrue(current["labs"]["8"]["completed"])
        self.assertEqual(current["labs"]["8"]["flag"], earned["lab"]["flag"])
        self.assertEqual(current["summary"]["score"], 400)

    def test_legacy_unverified_capture_cannot_claim_a_new_award(self):
        before = self.record()
        legacy = copy.deepcopy(before)
        legacy.update(exploited=True, last_result={"triggered": True, "sources": [], "message": "Old heuristic result."})
        self.app.store.save_lab8(self.sid, legacy, before)
        public = self.app.public_lab(self.sid, 8, self.record())
        self.assertFalse(public["exploited"])
        self.assertFalse(public["last_result"]["triggered"])
        self.assertIsNone(public["flag"])
        with self.assertRaises(server.APIError) as error:
            self.app.submit_flag(self.sid, 8, {"flag": self.app.store.flag(self.sid, 8, 1)})
        self.assertEqual(error.exception.status, 403)
        self.assertTrue(self.record()["exploited"])

    def test_completion_and_verification_leave_other_labs_and_schema_unchanged(self):
        _, before = self.app.store.get(self.sid)
        with self.app.store.connect() as db:
            schema = db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall()
        self.earn()
        self.app.reset(self.sid, 8)
        _, after = self.app.store.get(self.sid)
        for key in before["labs"]:
            if key != "8":
                self.assertEqual(before["labs"][key], after["labs"][key])
        with self.app.store.connect() as db:
            self.assertEqual(schema, db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall())


if __name__ == "__main__":
    unittest.main()
