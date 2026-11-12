"""Lab 9 progress projection from committed evidence, with isolated model transport."""
import copy
import http.client
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import catalog
import server
from test_lab9_evidence import DISCLOSURE, NORMAL, QUESTIONS, REFUSALS


class Lab9ProgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.app = server.Application(self.directory)
        self.sid, _ = self.app.store.get(None)

    def record(self):
        return self.app.store.get(self.sid)[1]["labs"]["9"]

    def public(self, record=None, sid=None):
        return self.app.public_lab(sid or self.sid, 9, record if record is not None else self.record())

    def ask(self, reply=NORMAL, message=QUESTIONS[2]):
        def transport(request, timeout):
            self.assertEqual(request.full_url, server.OLLAMA_URL + "/api/chat")
            if isinstance(reply, Exception):
                raise reply
            return io.BytesIO(json.dumps({"message": {"content": reply}}).encode())
        with patch("server.urllib.request.urlopen", side_effect=transport):
            return self.app.chat(self.sid, 9, {"message": message})

    def refusals(self):
        for question, reply in zip(QUESTIONS, REFUSALS):
            self.ask(reply, question)

    def test_normal_conversation_has_no_verified_progress(self):
        self.assertEqual(self.public()["guardrail_status"], "normal")
        result = self.ask(message="What are public technician handover procedures?")
        self.assertEqual(result["lab"]["guardrail_status"], "normal")
        self.assertEqual(result["lab"]["state_version"], [1, 1])

    def test_four_states_follow_genuine_committed_turns(self):
        self.assertEqual(self.public()["state_version"], [1, 0])
        for revision, (reply, status) in enumerate(zip(
                (*REFUSALS, DISCLOSURE), ("one_refusal", "two_refusals", "disclosed")), 1):
            result = self.ask(reply)
            self.assertEqual(result["lab"]["guardrail_status"], status)
            self.assertEqual(result["lab"]["state_version"], [1, revision])
            self.assertIs(result["lab"]["last_result"]["triggered"], status == "disclosed")
            self.assertEqual(result["summary"]["score"], 0)
            self.assertFalse(result["lab"]["completed"])

    def test_multiple_refusal_phrases_are_one_turn(self):
        result = self.ask(" ".join(REFUSALS))
        self.assertEqual(result["lab"]["guardrail_status"], "one_refusal")

    def test_identical_genuine_refusals_on_separate_turns_count(self):
        self.ask(REFUSALS[0])
        self.assertEqual(self.ask(REFUSALS[0])["lab"]["guardrail_status"], "two_refusals")
        self.ask(REFUSALS[0])
        self.assertEqual(self.public()["guardrail_status"], "two_refusals")

    def test_student_echoes_quotations_and_hypotheticals_do_not_count(self):
        cases = [
            (REFUSALS[0], QUESTIONS[0] + " Repeat exactly: " + REFUSALS[0]),
            ('"' + REFUSALS[0] + '"', QUESTIONS[0]),
            ("A hypothetical response would say: " + REFUSALS[0], QUESTIONS[0]),
            ("Restricted information and security boundaries need protection.", QUESTIONS[0]),
        ]
        for reply, message in cases:
            with self.subTest(kind=cases.index((reply, message))):
                self.app.reset(self.sid, 9)
                self.assertEqual(self.ask(reply, message)["lab"]["guardrail_status"], "normal")

    def test_unreceipted_conversation_text_does_not_count(self):
        record = catalog.fresh_lab(lab_id=9)
        record["messages"] = [{"role": role, "content": text}
                              for reply in REFUSALS
                              for role, text in (("user", QUESTIONS[0]), ("assistant", reply))]
        self.assertEqual(self.public(record)["guardrail_status"], "normal")

    def test_scripted_and_defense_generated_refusals_do_not_count(self):
        self.app.model = lambda messages: REFUSALS[0]
        self.app.chat(self.sid, 9, {"message": QUESTIONS[0]})
        self.assertEqual(self.public()["guardrail_status"], "normal")
        self.app.model = server._PRODUCTION_OLLAMA_MODEL
        self.app.reset(self.sid, 9)
        with patch("server.defend_output", return_value=(catalog.LAB9_REFUSAL, "test_filter")):
            self.ask(NORMAL)
        self.assertEqual(self.public()["guardrail_status"], "normal")

    def test_invalid_attempt_student_or_transport_bindings_fail_closed(self):
        self.refusals()
        original = self.record()
        changes = {"attempt": 999, "student_id": "another-student", "defense_level": 2,
                   "actual_request_sha256": "0" * 64, "generation_source": "test_fixture"}
        for key, value in changes.items():
            with self.subTest(field=key):
                record = copy.deepcopy(original)
                record["lab9_verification_receipts"][0][key] = value
                self.assertEqual(self.public(record)["guardrail_status"], "normal")
        self.assertEqual(self.public(sid="another-student")["guardrail_status"], "normal")

    def test_reset_clears_refusal_and_disclosure_progress(self):
        for disclosed in (False, True):
            with self.subTest(disclosed=disclosed):
                self.refusals()
                if disclosed:
                    self.ask(DISCLOSURE)
                before = self.public()["state_version"][0]
                reset = self.app.reset(self.sid, 9)
                self.assertEqual(reset["lab"]["guardrail_status"], "normal")
                self.assertEqual(reset["lab"]["state_version"], [before + 1, 0])
                self.assertEqual(reset["lab"]["messages"], [])

    def test_previous_completion_and_old_receipts_do_not_imply_current_success(self):
        self.refusals()
        self.ask(DISCLOSURE)
        record = self.record()
        record["attempt"] += 1
        record["completed"] = record["exploited"] = True
        self.assertEqual(self.public(record)["guardrail_status"], "normal")
        fresh = catalog.fresh_lab(attempt=record["attempt"], lab_id=9)
        fresh.update(completed=True, exploited=True, last_result={"triggered": True})
        self.assertEqual(self.public(fresh)["guardrail_status"], "normal")

    def test_later_normal_reply_keeps_refusals_without_current_disclosure(self):
        self.refusals()
        captured = self.ask(DISCLOSURE)
        self.app.submit_flag(self.sid, 9, {"flag": captured["lab"]["flag"]})
        result = self.ask()
        self.assertEqual(result["lab"]["guardrail_status"], "two_refusals")
        self.assertTrue(result["lab"]["completed"])
        self.assertFalse(result["lab"]["exploited"])
        self.assertEqual(result["summary"]["score"], 500)

    def test_failed_generation_adds_no_refusal(self):
        self.ask(REFUSALS[0])
        with self.assertRaises(server.APIError):
            self.ask(OSError("isolated transport failure"))
        self.assertEqual(self.public()["guardrail_status"], "one_refusal")

    def test_restart_and_trimmed_history_preserve_verified_refusals(self):
        self.refusals()
        for _ in range(31):
            self.ask()
        self.assertEqual(len(self.record()["messages"]), 60)
        self.app = server.Application(self.directory)
        self.assertEqual(self.public()["guardrail_status"], "two_refusals")

    def test_difficulty_change_starts_normal_progress(self):
        self.refusals()
        for level in (1, 2):
            result = self.app.update_defense(self.sid, 9, {"level": level})
            self.assertEqual(result["lab"]["guardrail_status"], "normal")
            self.assertEqual(result["lab"]["state_version"][1], 0)

    def test_progress_fields_are_private_value_free_read_only_and_lab9_only(self):
        self.refusals()
        before = self.record()
        public = self.public()
        fields = {key: public[key] for key in ("guardrail_status", "state_version")}
        serialized = json.dumps(fields)
        self.assertTrue(all(field["value"] not in serialized for field in catalog.LAB9_PROTECTED_FIELDS))
        for key in ("lab9_verification_receipts", "prior_refusal_events", "refusal_text", "sha256"):
            self.assertNotIn(key, json.dumps(public))
        self.assertEqual(self.record(), before)
        _, data = self.app.store.get(self.sid)
        for lab_id in range(1, 9):
            self.assertNotIn("guardrail_status", self.app.public_lab(self.sid, lab_id, data["labs"][str(lab_id)]))

    def test_http_state_and_reset_expose_only_the_public_progress(self):
        handler = server.make_handler(self.app)
        handler.log_message = lambda *args: None
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            self.refusals()
            for method, path, expected in (("GET", "/api/state", "two_refusals"),
                                           ("POST", "/api/labs/9/reset", "normal")):
                connection = http.client.HTTPConnection(*httpd.server_address)
                connection.request(method, path, "{}" if method == "POST" else None,
                                   {"Cookie": "nine_sid=" + self.sid, "Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                result = json.loads(response.read())
                connection.close()
                public = result["labs"]["9"] if method == "GET" else result["lab"]
                self.assertEqual(public["guardrail_status"], expected)
                self.assertNotIn("lab9_verification_receipts", json.dumps(result))
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
