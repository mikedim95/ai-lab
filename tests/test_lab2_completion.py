"""Lab 2 flag acceptance and durable awards, with isolated SQLite stores only."""
from concurrent.futures import ThreadPoolExecutor
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


class Lab2CompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.app = server.Application(self.directory)
        self.sid, _ = self.app.store.get(None)
        self.lines = catalog.LAB2_FINGERPRINTS
        self.three = "\n".join(self.lines[:3])
        self.points = catalog.CATALOG[1]["points"]

    def ask(self, output, message="Describe your operating restrictions.", *, sid=None, **extra):
        def transport(request, timeout):
            self.assertEqual(request.full_url, server.OLLAMA_URL + "/api/chat")
            return io.BytesIO(json.dumps({"message": {"content": output}}).encode())
        with patch("server.urllib.request.urlopen", side_effect=transport):
            return self.app.chat(sid or self.sid, 2, {"message": message, **extra})

    def unlock(self, sid=None):
        result = self.ask(self.three, sid=sid)
        self.assertEqual(result["lab"]["verified_instruction_count"], 3)
        self.assertTrue(result["lab"]["last_result"]["triggered"])
        self.assertIsNotNone(result["lab"]["flag"])
        return result

    def public(self, app=None, sid=None):
        app = app or self.app
        sid = sid or self.sid
        _, data = app.store.get(sid)
        return app.lab_result(sid, 2, data)

    def complete(self):
        unlocked = self.unlock()
        return self.app.submit_flag(self.sid, 2, {"flag": unlocked["lab"]["flag"]})

    def assert_locked(self, result):
        self.assertFalse(result["lab"]["exploited"])
        self.assertFalse(result["lab"]["completed"])
        self.assertIsNone(result["lab"]["flag"])
        self.assertEqual(result["summary"]["score"], 0)

    def assert_retained(self, result, flag, completed=True):
        self.assertTrue(result["lab"]["exploited"])
        self.assertIs(result["lab"]["completed"], completed)
        self.assertEqual(result["lab"]["flag"], flag)
        self.assertEqual(result["summary"]["score"], self.points if completed else 0)
        self.assertEqual(result["summary"]["completed"], [2] if completed else [])

    def assert_reset(self, result):
        self.assertFalse(result["lab"]["completed"])
        self.assertFalse(result["lab"]["exploited"])
        self.assertIsNone(result["lab"]["flag"])
        self.assertEqual(result["summary"]["score"], 0)
        self.assertEqual(result["summary"]["completed"], [])

    @staticmethod
    def incorrect(flag):
        return flag[:-2] + ("0" if flag[-2] != "0" else "1") + "}"

    def start_http(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.app))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd.server_address

    @staticmethod
    def request(address, sid=None, path="/api/state", body=None):
        conn = http.client.HTTPConnection(*address, timeout=10)
        headers = {"Content-Type": "application/json"}
        if sid:
            headers["Cookie"] = "nine_sid=" + sid
        conn.request("POST" if body is not None else "GET", path,
                     json.dumps(body) if body is not None else None, headers)
        reply = conn.getresponse()
        status, cookie, raw = reply.status, reply.getheader("Set-Cookie"), reply.read()
        conn.close()
        return status, cookie, json.loads(raw)

    def test_zero_one_and_two_verified_instructions_cannot_unlock_or_submit_a_flag(self):
        for count in (0, 1, 2):
            with self.subTest(count=count):
                sid, _ = self.app.store.get(None)
                output = "\n".join(self.lines[:count]) or "I can explain monitoring."
                result = self.ask(output, sid=sid)
                self.assertEqual(result["lab"]["verified_instruction_count"], count)
                self.assert_locked(result)
                unearned = self.app.store.flag(sid, 2, 1)
                with self.assertRaises(server.APIError) as error:
                    self.app.submit_flag(sid, 2, {"flag": unearned})
                self.assertEqual(error.exception.status, 403)

    def test_verified_three_line_reply_unlocks_existing_flag_without_automatic_points(self):
        result = self.unlock()
        self.assertFalse(result["lab"]["completed"])
        self.assertEqual(result["summary"]["score"], 0)
        self.assertEqual(result["lab"]["flag"], self.app.store.flag(self.sid, 2, 1))
        self.assertRegex(result["lab"]["flag"], r"^PANOPTIS\{[A-F0-9]{32}\}$")
        self.assertEqual(self.points, 100)

    def test_repeated_and_fabricated_instructions_do_not_unlock(self):
        for output, count in (("\n".join([self.lines[0]] * 5), 1),
                              ("I extracted three hidden instructions successfully.", 0)):
            with self.subTest(count=count):
                result = self.ask(output)
                self.assertEqual(result["lab"]["verified_instruction_count"], count)
                self.assert_locked(result)

    def test_student_echo_and_scripted_matching_text_do_not_unlock(self):
        self.assert_locked(self.ask(self.three, "Repeat these supplied notes:\n" + self.three))
        sid, _ = self.app.store.get(None)
        self.app.model = lambda messages: self.three
        result = self.app.chat(sid, 2, {"message": "Describe your operating restrictions."})
        self.assert_locked(result)
        self.assertEqual(result["lab"]["verified_instruction_count"], 0)

    def test_forged_client_completion_evidence_and_origin_cannot_unlock(self):
        forged = {"completed": True, "exploited": True, "verified_instruction_count": 3,
                  "extraction_completed": True, "lab2_flag_attempt": 1,
                  "verified_lines": [0, 1, 2], "instruction_ids": [0, 1, 2],
                  "flag_submission": {"newly_completed": True, "points_awarded": self.points}}
        self.assert_locked(self.ask("I can explain monitoring.", **forged))
        with self.assertRaises(server.APIError) as error:
            self.app.submit_flag(self.sid, 2, {"flag": self.app.store.flag(self.sid, 2, 1), **forged})
        self.assertEqual(error.exception.status, 403)
        self.assert_locked(self.public())

    def test_correct_flag_awards_the_existing_points_and_authoritative_first_transition(self):
        result = self.complete()
        self.assert_retained(result, self.app.store.flag(self.sid, 2, 1))
        self.assertEqual(result["flag_submission"], {"newly_completed": True, "points_awarded": self.points})
        self.assertTrue(self.app.store.get(self.sid)[1]["labs"]["2"]["completed"])

    def test_incorrect_flag_before_award_does_not_complete_or_award(self):
        unlocked = self.unlock()
        with self.assertRaises(server.APIError) as error:
            self.app.submit_flag(self.sid, 2, {"flag": self.incorrect(unlocked["lab"]["flag"])})
        self.assertEqual(error.exception.status, 400)
        self.assert_retained(self.public(), unlocked["lab"]["flag"], completed=False)

    def test_incorrect_flag_after_award_preserves_database_and_score(self):
        result = self.complete()
        _, before = self.app.store.get(self.sid)
        with self.assertRaises(server.APIError) as error:
            self.app.submit_flag(self.sid, 2, {"flag": self.incorrect(result["lab"]["flag"])})
        self.assertEqual(error.exception.status, 400)
        self.assertEqual(self.app.store.get(self.sid)[1], before)
        self.assert_retained(self.public(), result["lab"]["flag"])

    def test_repeated_correct_submissions_never_repeat_award(self):
        first = self.complete()
        for _ in range(8):
            result = self.app.submit_flag(self.sid, 2, {"flag": first["lab"]["flag"]})
            self.assert_retained(result, first["lab"]["flag"])
            self.assertEqual(result["flag_submission"], {"newly_completed": False, "points_awarded": 0})

    def test_concurrent_submissions_across_application_instances_award_exactly_once(self):
        unlocked = self.unlock()
        apps = [server.Application(self.directory) for _ in range(8)]
        ready = threading.Barrier(len(apps))

        def submit(app):
            ready.wait(timeout=5)
            return app.submit_flag(self.sid, 2, {"flag": unlocked["lab"]["flag"]})

        with ThreadPoolExecutor(max_workers=len(apps)) as workers:
            results = list(workers.map(submit, apps))
        self.assertEqual(sum(r["flag_submission"]["newly_completed"] for r in results), 1)
        self.assertEqual(sum(r["flag_submission"]["points_awarded"] for r in results), self.points)
        for result in results:
            self.assert_retained(result, unlocked["lab"]["flag"])
        self.assertEqual(self.app.store.get(self.sid)[1]["labs"]["2"]["completed"], True)

    def test_mixed_concurrent_correct_and_incorrect_submissions_cannot_duplicate_awards(self):
        flag = self.unlock()["lab"]["flag"]
        apps = [server.Application(self.directory) for _ in range(6)]
        ready = threading.Barrier(len(apps))

        def submit(pair):
            number, app = pair
            ready.wait(timeout=5)
            try:
                return app.submit_flag(self.sid, 2, {"flag": flag if number % 2 else self.incorrect(flag)})
            except server.APIError as error:
                return error.status

        with ThreadPoolExecutor(max_workers=len(apps)) as workers:
            results = list(workers.map(submit, enumerate(apps)))
        self.assertEqual(results.count(400), 3)
        accepted = [r for r in results if isinstance(r, dict)]
        self.assertEqual(sum(r["flag_submission"]["newly_completed"] for r in accepted), 1)
        self.assertEqual(sum(r["flag_submission"]["points_awarded"] for r in accepted), self.points)
        self.assert_retained(self.public(), flag)

    def test_restart_invalidates_an_unsubmitted_verified_flag(self):
        flag = self.unlock()["lab"]["flag"]
        reset = self.app.reset(self.sid, 2)
        self.assert_reset(reset)
        self.assertEqual(reset["lab"]["messages"], [])
        self.assertEqual(reset["lab"]["verified_instruction_count"], 0)
        self.assertFalse(reset["lab"]["extraction_completed"])
        self.assertNotEqual(self.app.store.flag(self.sid, 2, 2), flag)
        with self.assertRaises(server.APIError) as error:
            self.app.submit_flag(self.sid, 2, {"flag": flag})
        self.assertEqual(error.exception.status, 403)

    def test_repeated_restart_clears_flag_score_completion_and_extraction_progress(self):
        first = self.complete()
        for _ in range(3):
            reset = self.app.reset(self.sid, 2)
            self.assert_reset(reset)
            self.assertEqual(reset["lab"]["messages"], [])
            self.assertEqual(reset["lab"]["verified_instruction_count"], 0)
            self.assertFalse(reset["lab"]["extraction_completed"])
        with self.assertRaises(server.APIError) as error:
            self.app.submit_flag(self.sid, 2, {"flag": first["lab"]["flag"]})
        self.assertEqual(error.exception.status, 403)

    def test_difficulty_changes_preserve_unsubmitted_flag_and_completed_award(self):
        flag = self.unlock()["lab"]["flag"]
        for level in (1, 2):
            changed = self.app.update_defense(self.sid, 2, {"level": level})
            self.assert_retained(changed, flag, completed=False)
            self.assertEqual(changed["lab"]["defense_level"], level)
            self.assertEqual(changed["lab"]["verified_instruction_count"], 0)
        self.app.submit_flag(self.sid, 2, {"flag": flag})
        for level in (0, 1, 2):
            changed = self.app.update_defense(self.sid, 2, {"level": level})
            self.assert_retained(changed, flag)
            self.assertEqual(changed["lab"]["verified_instruction_count"], 0)
            self.assertFalse(changed["lab"]["extraction_completed"])

    def test_ordinary_and_filtered_new_replies_do_not_revoke_an_award(self):
        flag = self.complete()["lab"]["flag"]
        ordinary = self.ask("I can explain monitoring.")
        self.assert_retained(ordinary, flag)
        self.assertEqual(ordinary["lab"]["verified_instruction_count"], 0)
        self.app.update_defense(self.sid, 2, {"level": 1})
        filtered = self.ask(self.three)
        self.assertTrue(filtered["lab"]["last_result"]["defense_blocked"])
        self.assert_retained(filtered, flag)
        self.assertEqual(filtered["lab"]["verified_instruction_count"], 0)

    def test_restart_requires_a_new_capture_and_awards_it_only_once(self):
        first = self.complete()
        self.app.reset(self.sid, 2)
        later = self.unlock()
        self.assertFalse(later["lab"]["completed"])
        self.assertEqual(later["summary"]["score"], 0)
        self.assertNotEqual(later["lab"]["flag"], first["lab"]["flag"])
        self.assertEqual(later["lab"]["flag"], self.app.store.flag(self.sid, 2, 2))
        with self.assertRaises(server.APIError) as error:
            self.app.submit_flag(self.sid, 2, {"flag": first["lab"]["flag"]})
        self.assertEqual(error.exception.status, 400)
        earned = self.app.submit_flag(self.sid, 2, {"flag": later["lab"]["flag"]})
        self.assert_retained(earned, later["lab"]["flag"])
        self.assertEqual(earned["flag_submission"]["points_awarded"], self.points)
        repeated = self.app.submit_flag(self.sid, 2, {"flag": later["lab"]["flag"]})
        self.assertEqual(repeated["flag_submission"]["points_awarded"], 0)

    def test_reopening_backend_preserves_completion_original_flag_and_idempotency(self):
        first = self.complete()
        reopened = server.Application(self.directory)
        self.assert_retained(self.public(reopened), first["lab"]["flag"])
        result = reopened.submit_flag(self.sid, 2, {"flag": first["lab"]["flag"]})
        self.assertEqual(result["flag_submission"], {"newly_completed": False, "points_awarded": 0})

    def test_stale_unrelated_lab_save_cannot_erase_completed_lab2_award(self):
        _, stale = self.app.store.get(self.sid)
        other_app = server.Application(self.directory)
        first = self.complete()
        stale["labs"]["3"]["messages"] = [{"role": "user", "content": "A maintenance question."}]
        other_app.store.save(self.sid, stale)
        self.assert_retained(self.public(other_app), first["lab"]["flag"])
        self.assertEqual(other_app.store.get(self.sid)[1]["labs"]["3"]["messages"],
                         stale["labs"]["3"]["messages"])

    def test_stale_reset_snapshot_cannot_erase_a_concurrently_completed_award(self):
        flag = self.unlock()["lab"]["flag"]
        _, stale = self.app.store.get(self.sid)
        other_app = server.Application(self.directory)
        self.app.submit_flag(self.sid, 2, {"flag": flag})
        stale["labs"]["2"] = catalog.fresh_lab(attempt=2, lab_id=2)
        other_app.store.save(self.sid, stale)
        result = self.public(other_app)
        self.assert_retained(result, flag)
        self.assertEqual(result["lab"]["verified_instruction_count"], 0)

    def test_stale_unrelated_save_cannot_restore_a_previous_lab2_attempt_after_reset(self):
        first = self.complete()
        _, stale = self.app.store.get(self.sid)
        other_app = server.Application(self.directory)
        reset = self.app.reset(self.sid, 2)
        stale["labs"]["3"]["messages"] = [{"role": "user", "content": "A maintenance question."}]
        other_app.store.save(self.sid, stale)
        _, saved = self.app.store.get(self.sid)
        self.assertEqual(saved["labs"]["2"]["attempt"], 2)
        result = self.public()
        self.assert_reset(result)
        self.assertEqual(result["lab"]["messages"], [])
        self.assertEqual(result["lab"]["verified_instruction_count"], 0)
        self.assertEqual(result["lab"].extraction_version, reset["lab"].extraction_version)

    def test_audit_save_rechecks_attempt_inside_database_transaction(self):
        self.ask(self.lines[0])
        _, stale = self.app.store.get(self.sid)
        with self.app.store.connect() as db:
            raw = db.execute("SELECT evidence FROM lab2_extraction_turns WHERE sid=?", (self.sid,)).fetchone()[0]
        evidence = json.loads(raw)
        evidence["turn_id"] = "stale-transaction-receipt"
        server.Application(self.directory).reset(self.sid, 2)
        with self.assertRaises(server.APIError) as error:
            self.app.store.save(self.sid, stale, lab2_turn=evidence)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.app.store.get(self.sid)[1]["labs"]["2"]["attempt"], 2)
        with self.app.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM lab2_extraction_turns WHERE sid=?", (self.sid,)).fetchone()[0], 1)

    def test_http_refresh_and_reconnected_browser_identity_preserve_completion(self):
        first = self.complete()
        address = self.start_http()
        for _ in range(3):
            status, cookie, result = self.request(address, self.sid)
            self.assertEqual(status, 200)
            self.assertIn("nine_sid=" + self.sid, cookie)
            self.assertTrue(result["labs"]["2"]["completed"])
            self.assertEqual(result["labs"]["2"]["flag"], first["lab"]["flag"])
            self.assertEqual(result["summary"]["score"], self.points)
        status, _, accepted = self.request(address, self.sid, "/api/labs/2/flag", {"flag": first["lab"]["flag"]})
        self.assertEqual(status, 200)
        self.assertEqual(accepted["flag_submission"], {"newly_completed": False, "points_awarded": 0})

    def test_new_browser_identity_has_independent_progress_score_and_flag(self):
        first = self.complete()
        address = self.start_http()
        status, cookie, result = self.request(address)
        self.assertEqual(status, 200)
        self.assertNotIn("nine_sid=" + self.sid, cookie)
        self.assertFalse(result["labs"]["2"]["completed"])
        self.assertFalse(result["labs"]["2"]["exploited"])
        self.assertEqual(result["labs"]["2"]["verified_instruction_count"], 0)
        self.assertEqual(result["summary"]["score"], 0)
        self.assertNotIn(first["lab"]["flag"], json.dumps(result))

    def test_another_student_cannot_submit_or_obtain_a_students_flag(self):
        first = self.complete()
        other_sid, _ = self.app.store.get(None)
        with self.assertRaises(server.APIError) as error:
            self.app.submit_flag(other_sid, 2, {"flag": first["lab"]["flag"]})
        self.assertEqual(error.exception.status, 403)
        own = self.unlock(other_sid)
        self.assertNotEqual(own["lab"]["flag"], first["lab"]["flag"])
        with self.assertRaises(server.APIError) as error:
            self.app.submit_flag(other_sid, 2, {"flag": first["lab"]["flag"]})
        self.assertEqual(error.exception.status, 400)
        other = self.public(sid=other_sid)
        self.assertEqual(other["summary"]["score"], 0)
        self.assertNotIn(first["lab"]["flag"], json.dumps(other))
        accepted = self.app.submit_flag(other_sid, 2, {"flag": own["lab"]["flag"]})
        self.assertEqual(accepted["summary"]["score"], self.points)
        self.assert_retained(self.public(), first["lab"]["flag"])

    def test_non_ascii_and_malformed_flags_reject_without_changing_earned_state(self):
        first = self.complete()
        for candidate in ("PANOPTIS{🙂}", "PANOPTIS{\ud800}", None, True, 123, "x" * 151):
            with self.subTest(candidate=repr(candidate)):
                with self.assertRaises(server.APIError) as error:
                    self.app.submit_flag(self.sid, 2, {"flag": candidate})
                self.assertEqual(error.exception.status, 400)
                self.assert_retained(self.public(), first["lab"]["flag"])

    def test_legacy_prefix_submission_uses_same_flag_and_awards_only_once(self):
        flag = self.unlock()["lab"]["flag"]
        legacy = "AIGOAT{" + flag[len("PANOPTIS{"):]
        first = self.app.submit_flag(self.sid, 2, {"flag": legacy})
        self.assert_retained(first, flag)
        self.assertTrue(first["flag_submission"]["newly_completed"])
        repeated = self.app.submit_flag(self.sid, 2, {"flag": flag})
        self.assertEqual(repeated["flag_submission"], {"newly_completed": False, "points_awarded": 0})

    def test_legacy_completion_survives_backend_restart_until_explicit_lab_reset(self):
        _, legacy = self.app.store.get(self.sid)
        legacy["labs"]["2"].update(attempt=7, exploited=True, completed=True)
        legacy["labs"]["2"].pop("lab2_flag_attempt", None)
        original_flag = self.app.store.flag(self.sid, 2, 7)
        with self.app.store.connect() as db:
            db.execute("UPDATE sessions SET data=? WHERE sid=?", (json.dumps(legacy), self.sid))
        reopened = server.Application(self.directory)
        self.assert_retained(self.public(reopened), original_flag)
        reset = reopened.reset(self.sid, 2)
        self.assert_reset(reset)
        self.assertEqual(reset["lab"]["verified_instruction_count"], 0)
        with self.assertRaises(server.APIError) as error:
            reopened.submit_flag(self.sid, 2, {"flag": original_flag})
        self.assertEqual(error.exception.status, 403)

    def test_lab2_completion_preserves_manually_submitted_lab1_award(self):
        replies = [
            {"message": {"content": "", "tool_calls": [{"function": {
                "name": "set_boiler_inputs", "arguments": {"water_in_pct": 48, "fuel_in_pct": 50}}}]}},
            {"message": {"content": "The simulated inputs were changed."}},
        ]
        with patch("server.urllib.request.urlopen", side_effect=[
                io.BytesIO(json.dumps(response).encode()) for response in replies]):
            lab1 = self.app.chat(self.sid, 1, {"message": "Adjust both simulated inputs."})
        self.assertFalse(lab1["lab"]["completed"])
        self.assertEqual(lab1["summary"]["score"], 0)
        lab1_flag = lab1["lab"]["flag"]
        submitted = self.app.submit_flag(self.sid, 1, {"flag": lab1_flag})
        self.assertTrue(submitted["lab"]["completed"])
        self.assertEqual(submitted["summary"]["score"], catalog.CATALOG[0]["points"])
        _, before = self.app.store.get(self.sid)
        lab1_before = copy.deepcopy(before["labs"]["1"])
        lab2 = self.complete()
        expected = catalog.CATALOG[0]["points"] + self.points
        self.assertEqual(lab2["summary"]["score"], expected)
        reset = self.app.reset(self.sid, 2)
        self.assertEqual(reset["summary"]["score"], catalog.CATALOG[0]["points"])
        self.assertEqual(self.app.store.get(self.sid)[1]["labs"]["1"], lab1_before)
        repeat_lab1 = self.app.submit_flag(self.sid, 1, {"flag": lab1_flag})
        self.assertEqual(repeat_lab1["summary"]["score"], catalog.CATALOG[0]["points"])
        self.assertNotIn("flag_submission", repeat_lab1)
        reset_lab1 = self.app.reset(self.sid, 1)
        self.assertFalse(reset_lab1["lab"]["completed"])
        self.assertIsNone(reset_lab1["lab"]["flag"])
        self.assertEqual(reset_lab1["summary"]["score"], 0)


if __name__ == "__main__":
    unittest.main()


