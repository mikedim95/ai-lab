"""Public reply-scoped progress, using isolated stores and mocked transport."""
import copy
import http.client
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import secrets
import tempfile
import threading
import unittest
from unittest.mock import patch

import catalog
import server


class Lab2ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = server.Application(Path(self.temp.name))
        self.sid, _ = self.app.store.get(None)
        self.lines = catalog.LAB2_FINGERPRINTS

    def ask(self, output, message="Explain operating restrictions.", *, sid=None, **extra):
        def transport(request, timeout):
            self.assertEqual(request.full_url, server.OLLAMA_URL + "/api/chat")
            return io.BytesIO(json.dumps({"message": {"content": output}}).encode())
        with patch("server.urllib.request.urlopen", side_effect=transport):
            return self.app.chat(sid or self.sid, 2, {"message": message, **extra})

    def public(self, sid=None):
        sid = sid or self.sid
        _, data = self.app.store.get(sid)
        return self.app.public_lab(sid, 2, data["labs"]["2"])

    def assert_progress(self, lab, count, completed=False):
        self.assertIs(type(lab["verified_instruction_count"]), int)
        self.assertEqual(lab["verified_instruction_count"], count)
        self.assertIs(type(lab["required_instruction_count"]), int)
        self.assertEqual(lab["required_instruction_count"], 3)
        self.assertIs(type(lab["extraction_completed"]), bool)
        self.assertIs(lab["extraction_completed"], completed)
        self.assertNotIn("verified_instruction_leaks", lab)

    def append_private_receipt(self, mutate):
        _, data = self.app.store.get(self.sid)
        attempt = data["labs"]["2"]["attempt"]
        with self.app.store.connect() as db:
            raw = db.execute("SELECT evidence FROM lab2_extraction_turns WHERE sid=? AND attempt=? "
                             "ORDER BY rowid DESC LIMIT 1", (self.sid, attempt)).fetchone()[0]
        evidence = copy.deepcopy(json.loads(raw))
        evidence["turn_id"] = secrets.token_hex(16)
        mutate(evidence)
        self.app.store.save(self.sid, data, lab2_turn=evidence)

    def test_initial_progress_is_zero_and_projection_exposes_only_minimum_fields(self):
        lab = self.public()
        self.assert_progress(lab, 0)
        self.assertEqual(lab.extraction_version, (1, 0, 0))
        self.assertEqual(set(lab), {"id", "messages", "completed", "exploited", "flag", "documents",
                                   "kb_revision", "synced_revision", "use_kb", "last_result", "defense_level",
                                   "verified_instruction_count", "required_instruction_count", "extraction_completed"})
        encoded = json.dumps(lab)
        self.assertNotIn("extraction_version", encoded)
        self.assertNotIn("verified_lines", encoded)
        self.assertNotIn("supplied_lines", encoded)

    def test_one_authentic_instruction_reports_one(self):
        self.assert_progress(self.ask(self.lines[0])["lab"], 1)

    def test_two_authentic_instructions_in_one_reply_report_two(self):
        self.assert_progress(self.ask("\n".join(self.lines[:2]))["lab"], 2)

    def test_three_authentic_instructions_in_one_reply_complete_extraction(self):
        result = self.ask("\n".join(self.lines[:3]))
        self.assert_progress(result["lab"], 3, True)
        self.assertTrue(result["lab"]["last_result"]["triggered"])
        self.assertIsNotNone(result["lab"]["flag"])
        self.assertEqual(result["summary"]["score"], 0)

    def test_all_five_instructions_are_capped_at_three(self):
        self.assert_progress(self.ask("\n".join(self.lines))["lab"], 3, True)

    def test_duplicate_instructions_do_not_increase_count(self):
        self.assert_progress(self.ask("\n".join([self.lines[0]] * 5))["lab"], 1)

    def test_fabricated_claim_is_zero(self):
        result = self.ask("I extracted three hidden system instructions successfully.")
        self.assert_progress(result["lab"], 0)
        self.assertIsNone(result["lab"]["flag"])

    def test_student_supplied_echo_is_zero(self):
        output = "\n".join(self.lines)
        self.assert_progress(self.ask(output, "Repeat these notes:\n" + output)["lab"], 0)

    def test_different_single_reply_disclosures_never_accumulate(self):
        for line in self.lines[:3]:
            result = self.ask(line)
            self.assert_progress(result["lab"], 1)
            self.assertFalse(result["lab"]["exploited"])
        self.assertEqual(self.app.store.lab2_progress(self.sid, 1)[0], {0, 1, 2})

    def test_later_two_line_reply_does_not_complete_a_previous_one_line_reply(self):
        self.ask(self.lines[0])
        result = self.ask("\n".join(self.lines[1:3]))
        self.assert_progress(result["lab"], 2)
        self.assertFalse(result["lab"]["exploited"])

    def test_later_ordinary_reply_resets_current_progress(self):
        self.ask("\n".join(self.lines[:2]))
        self.assert_progress(self.ask("I can explain boiler monitoring.")["lab"], 0)

    def test_new_backend_attempt_resets_progress_and_orders_after_previous_reply(self):
        previous = self.ask(self.lines[0])["lab"]
        reset = self.app.reset(self.sid, 2)["lab"]
        self.assert_progress(reset, 0)
        self.assertGreater(reset.extraction_version, previous.extraction_version)
        self.assertEqual(reset.extraction_version, (2, 0, 0))
        self.assert_progress(self.public(), 0)

    def test_mode_change_keeps_existing_new_attempt_progress_reset(self):
        previous = self.ask(self.lines[0])["lab"]
        changed = self.app.update_defense(self.sid, 2, {"level": 1})["lab"]
        self.assert_progress(changed, 0)
        self.assertEqual(changed["defense_level"], 1)
        self.assertGreater(changed.extraction_version, previous.extraction_version)

    def test_prior_earned_flag_and_completion_persist_after_progress_returns_to_zero(self):
        extracted = self.ask("\n".join(self.lines[:3]))["lab"]
        accepted = self.app.submit_flag(self.sid, 2, {"flag": extracted["flag"]})
        self.assertTrue(accepted["lab"]["completed"])
        score = accepted["summary"]["score"]
        later = self.ask("I can explain operating procedures.")
        self.assert_progress(later["lab"], 0)
        self.assertTrue(later["lab"]["completed"])
        self.assertTrue(later["lab"]["exploited"])
        self.assertEqual(later["lab"]["flag"], extracted["flag"])
        self.assertEqual(later["summary"]["score"], score)
        reopened = server.Application(Path(self.temp.name))
        _, saved = reopened.store.get(self.sid)
        lab = reopened.public_lab(self.sid, 2, saved["labs"]["2"])
        self.assert_progress(lab, 0)
        self.assertTrue(lab["completed"])
        self.assertEqual(lab["flag"], extracted["flag"])

    def test_legacy_award_without_evidence_is_preserved_but_not_shown_as_current_extraction(self):
        _, data = self.app.store.get(self.sid)
        data["labs"]["2"]["exploited"] = True
        data["labs"]["2"]["completed"] = True
        self.app.store.save(self.sid, data)
        lab = self.public()
        self.assert_progress(lab, 0)
        self.assertTrue(lab["completed"])
        self.assertIsNotNone(lab["flag"])

    def test_progress_and_revision_are_isolated_between_students(self):
        self.ask("\n".join(self.lines[:2]))
        other_sid, _ = self.app.store.get(None)
        self.assert_progress(self.public(other_sid), 0)
        other = self.ask(self.lines[3], sid=other_sid)["lab"]
        self.assert_progress(other, 1)
        self.assertEqual(other.extraction_version, (1, 1, 0))
        self.assert_progress(self.public(), 2)

    def test_client_fields_and_fake_evidence_cannot_set_progress_or_completion(self):
        result = self.ask("I can explain operating procedures.",
                          verified_instruction_count=3, required_instruction_count=0,
                          extraction_completed=True, verified_instruction_leaks=3,
                          verified_lines=[0, 1, 2], instruction_ids=[0, 1, 2],
                          extraction_version=[999, 999, 1],
                          lab2_turn={"source": "live_ollama", "verified_lines": [0, 1, 2]})
        self.assert_progress(result["lab"], 0)
        self.assertFalse(result["lab"]["exploited"])
        self.assertIsNone(result["lab"]["flag"])
        self.assertEqual(result["lab"].extraction_version, (1, 1, 0))

    def test_protected_filtered_instructions_count_zero(self):
        for level in (1, 2):
            with self.subTest(level=level):
                self.app.update_defense(self.sid, 2, {"level": level})
                result = self.ask("\n".join(self.lines))
                self.assert_progress(result["lab"], 0)
                self.assertTrue(result["lab"]["last_result"]["defense_blocked"])
                self.assertEqual(result["lab"]["messages"][-1]["content"], catalog.LAB2_REFUSAL)
                self.assertIsNone(result["lab"]["flag"])

    def test_partial_output_filter_counts_only_surviving_instruction(self):
        with patch("server.defend_output", return_value=(self.lines[0], "output_filtered")):
            result = self.ask("\n".join(self.lines[:3]))
        self.assert_progress(result["lab"], 1)

    def test_newer_unverified_receipt_does_not_fall_back_to_earlier_live_success(self):
        self.ask("\n".join(self.lines[:3]))
        self.append_private_receipt(lambda e: e.update(source="unverified"))
        self.assert_progress(self.public(), 0)

    def test_newer_receipt_with_different_contract_does_not_fall_back(self):
        self.ask("\n".join(self.lines[:3]))
        self.append_private_receipt(lambda e: e.update(contract_sha256="not-the-active-contract"))
        self.assert_progress(self.public(), 0)

    def test_malformed_latest_private_evidence_is_zero_without_falling_back(self):
        self.ask("\n".join(self.lines[:3]))
        for raw in ("not JSON", "[]", json.dumps({"source": "live_ollama"})):
            with self.subTest(raw=raw):
                with self.app.store.connect() as db:
                    db.execute("INSERT INTO lab2_extraction_turns VALUES (?, ?, ?, ?)",
                               (self.sid, 1, secrets.token_hex(16), raw))
                self.assert_progress(self.public(), 0)

    def test_private_ids_are_distinct_valid_integers_and_completion_requires_backend_result(self):
        self.ask(self.lines[0])
        self.append_private_receipt(lambda e: e.update(verified_lines=[0, 0, True, False, -1, 99]))
        self.assert_progress(self.public(), 1)
        self.append_private_receipt(lambda e: e.update(verified_lines=[0, 1, 2, 3, 4]))
        self.assert_progress(self.public(), 3, False)
        self.assertFalse(self.public()["last_result"]["triggered"])

    def test_projection_ignores_visible_history_and_legacy_client_counters(self):
        _, data = self.app.store.get(self.sid)
        lab = data["labs"]["2"]
        lab.update(verified_instruction_count=3, extraction_completed=True,
                   lab2_verified_leaks=[0, 1, 2],
                   messages=[{"role": "assistant", "content": "\n".join(self.lines)}])
        self.app.store.save(self.sid, data)
        self.assert_progress(self.public(), 0)

    def test_revision_advances_when_progress_decreases_and_on_flag_submission(self):
        success = self.ask("\n".join(self.lines[:3]))["lab"]
        accepted = self.app.submit_flag(self.sid, 2, {"flag": success["flag"]})["lab"]
        self.assertGreater(accepted.extraction_version, success.extraction_version)
        self.assertEqual(accepted.extraction_version, (1, 1, 1))
        later = self.ask("I can explain operating procedures.")["lab"]
        self.assertGreater(later.extraction_version, accepted.extraction_version)
        self.assert_progress(later, 0)

    def test_http_versions_are_headers_without_private_evidence_in_json(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.app))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)

        def request(path="/api/state", body=None):
            conn = http.client.HTTPConnection(*httpd.server_address)
            headers = {"Cookie": "nine_sid=" + self.sid, "Content-Type": "application/json"}
            conn.request("POST" if body is not None else "GET", path,
                         body=json.dumps(body) if body is not None else None, headers=headers)
            response = conn.getresponse()
            status, version, raw = response.status, response.getheader("X-Lab2-Progress-Version"), response.read()
            conn.close()
            self.assertEqual(status, 200)
            return version, json.loads(raw)

        version, state = request()
        self.assertEqual(version, "1.0.0")
        self.assert_progress(state["labs"]["2"], 0)
        self.ask("\n".join(self.lines[:2]))
        version, state = request()
        self.assertEqual(version, "1.1.0")
        self.assert_progress(state["labs"]["2"], 2)
        metadata = {k: v for k, v in state["labs"]["2"].items() if k != "messages"}
        encoded = json.dumps(metadata)
        protected_values = [line.rstrip(".").rsplit(" ", 1)[-1] for line in self.lines]
        for private in ("verified_lines", "supplied_lines", "contract_sha256", "turn_id",
                        "instruction_ids", "lab2_extraction_turns", "extraction_version",
                        "system_prompt_sha256", *self.lines, *protected_values):
            self.assertNotIn(private, encoded)
        version, reset = request("/api/labs/2/reset", {})
        self.assertEqual(version, "2.0.0")
        self.assert_progress(reset["lab"], 0)
        extracted = self.ask("\n".join(self.lines[:3]))["lab"]
        version, accepted = request("/api/labs/2/flag", {"flag": extracted["flag"]})
        self.assertEqual(version, "2.1.1")
        self.assert_progress(accepted["lab"], 3, True)
        self.assertTrue(accepted["lab"]["completed"])
        self.assertNotIn("extraction_version", accepted["lab"])
        for lab_id, lab in state["labs"].items():
            if lab_id != "2":
                self.assertNotIn("verified_instruction_count", lab)
                self.assertNotIn("required_instruction_count", lab)
                self.assertNotIn("extraction_completed", lab)


if __name__ == "__main__":
    unittest.main()
