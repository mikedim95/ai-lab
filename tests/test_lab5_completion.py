"""Lab 5 completion transitions with existing verified fixtures and isolated stores."""
from concurrent.futures import ThreadPoolExecutor
import copy
import http.client
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import catalog
import server


from test_app import LAB5_MESSAGES, LAB5_DISCLOSURE
FIXTURE = {"student_message": LAB5_MESSAGES[0], "successful_reply": LAB5_DISCLOSURE}


class Lab5CompletionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.app = server.Application(self.directory)
        self.sid, _ = self.app.store.get(None)

    def record(self, app=None, sid=None):
        return (app or self.app).store.get(sid or self.sid)[1]["labs"]["5"]

    def public(self, app=None, sid=None):
        app = app or self.app
        sid = sid or self.sid
        return app.lab_result(sid, 5, app.store.get(sid)[1])

    def unlock(self, sid=None):
        sid = sid or self.sid
        for message in LAB5_MESSAGES[:2]:
            self.ask("Check the technician handover records.", sid=sid, message=message)
        return self.ask(FIXTURE["successful_reply"], sid=sid, message=LAB5_MESSAGES[2])

    def ask(self, response, sid=None, during=None, message=LAB5_MESSAGES[0]):
        def transport(request, timeout):
            self.assertEqual(request.full_url, server.OLLAMA_URL + "/api/chat")
            if during:
                during()
            return io.BytesIO(json.dumps({"message": {"content": response}}).encode())
        with patch("server.urllib.request.urlopen", side_effect=transport):
            return self.app.chat(sid or self.sid, 5, {"message": message})

    def complete(self):
        captured = self.unlock()
        return self.app.submit_flag(self.sid, 5, {"flag": captured["lab"]["flag"]})

    def assert_retained(self, result, flag):
        self.assertIs(result["lab"]["completed"], True)
        self.assertEqual(result["lab"]["flag"], flag)
        self.assertEqual(result["summary"]["score"], 250)
        self.assertEqual(result["summary"]["completed"], [5])

    def assert_no_current_verification(self, result):
        self.assertFalse(result["lab"]["exploited"])
        self.assertIsNone(result["lab"]["last_result"])
        self.assertEqual(result["lab"]["messages"], [])

    def test_flag_is_locked_before_verified_exploitation_even_with_a_computed_correct_flag(self):
        public = self.public()
        self.assertIsNone(public["lab"]["flag"])
        with self.assertRaises(server.APIError) as error:
            self.app.submit_flag(self.sid, 5, {"flag": self.app.store.flag(self.sid, 5, 1),
                "completed": True, "exploited": True, "lab5_flag_attempt": 1})
        self.assertEqual(error.exception.status, 403)
        self.assertEqual(self.public()["summary"]["score"], 0)

    def test_verified_existing_fixture_unlocks_flag_without_automatic_completion(self):
        captured = self.unlock()
        self.assertTrue(captured["lab"]["last_result"]["triggered"])
        self.assertTrue(captured["lab"]["exploited"])
        self.assertFalse(captured["lab"]["completed"])
        self.assertEqual(captured["summary"]["score"], 0)
        self.assertEqual(captured["lab"]["flag"], self.app.store.flag(self.sid, 5, 1))
        self.assertRegex(captured["lab"]["flag"], r"^PANOPTIS\{[A-F0-9]{32}\}$")
        receipt = self.record()["lab5_verification_receipts"][-1]
        self.assertTrue(receipt["verification_outcome"])
        self.assertEqual(receipt["generation_source"], "production_ollama")

    def test_correct_flag_has_one_authoritative_250_point_transition(self):
        result = self.complete()
        self.assert_retained(result, self.app.store.flag(self.sid, 5, 1))
        self.assertEqual(result["flag_submission"], {"newly_completed": True, "points_awarded": 250})
        self.assertEqual(catalog.CATALOG[4]["points"], 250)

    def test_duplicate_submissions_are_noop_transitions(self):
        first = self.complete()
        with self.app.store.connect() as db:
            before = db.execute("SELECT data,updated FROM sessions WHERE sid=?", (self.sid,)).fetchone()
        for _ in range(6):
            result = self.app.submit_flag(self.sid, 5, {"flag": first["lab"]["flag"]})
            self.assert_retained(result, first["lab"]["flag"])
            self.assertEqual(result["flag_submission"], {"newly_completed": False, "points_awarded": 0})
        with self.app.store.connect() as db:
            self.assertEqual(db.execute("SELECT data,updated FROM sessions WHERE sid=?", (self.sid,)).fetchone(), before)

    def test_incorrect_flags_do_not_modify_saved_state_or_score_before_or_after_award(self):
        captured = self.unlock()
        for completed in (False, True):
            if completed:
                self.app.submit_flag(self.sid, 5, {"flag": captured["lab"]["flag"]})
            before = copy.deepcopy(self.app.store.get(self.sid)[1])
            for candidate in ("PANOPTIS{wrong}", "", "PANOPTIS{🙂}", "PANOPTIS{\ud800}", None, 123, True, "x" * 151):
                with self.subTest(completed=completed, candidate=repr(candidate)):
                    with self.assertRaises(server.APIError) as error:
                        self.app.submit_flag(self.sid, 5, {"flag": candidate})
                    self.assertEqual(error.exception.status, 400)
                    self.assertEqual(self.app.store.get(self.sid)[1], before)

    def test_concurrent_submissions_across_backends_award_exactly_once(self):
        flag = self.unlock()["lab"]["flag"]
        apps = [server.Application(self.directory) for _ in range(8)]
        ready = threading.Barrier(len(apps))
        def submit(app):
            ready.wait(timeout=5)
            return app.submit_flag(self.sid, 5, {"flag": flag})
        with ThreadPoolExecutor(max_workers=len(apps)) as pool:
            results = list(pool.map(submit, apps))
        self.assertEqual(sum(item["flag_submission"]["points_awarded"] for item in results), 250)
        self.assertEqual(sum(item["flag_submission"]["newly_completed"] for item in results), 1)
        for result in results:
            self.assert_retained(result, flag)

    def test_mixed_concurrent_correct_and_incorrect_submissions_do_not_duplicate_awards(self):
        flag = self.unlock()["lab"]["flag"]
        apps = [server.Application(self.directory) for _ in range(6)]
        ready = threading.Barrier(len(apps))
        def submit(pair):
            number, app = pair
            ready.wait(timeout=5)
            try:
                return app.submit_flag(self.sid, 5, {"flag": flag if number % 2 else "PANOPTIS{wrong}"})
            except server.APIError as error:
                return error.status
        with ThreadPoolExecutor(max_workers=len(apps)) as pool:
            results = list(pool.map(submit, enumerate(apps)))
        self.assertEqual(results.count(400), 3)
        successes = [item for item in results if isinstance(item, dict)]
        self.assertEqual(sum(item["flag_submission"]["points_awarded"] for item in successes), 250)
        self.assert_retained(self.public(), flag)

    def test_new_conversation_keeps_earned_completion_and_resets_current_verification(self):
        first = self.complete()
        result = self.app.reset(self.sid, 5)
        self.assert_retained(result, first["lab"]["flag"])
        self.assert_no_current_verification(result)
        self.assertGreater(result["lab"]["attempt"], first["lab"]["attempt"])
        self.assertEqual(result["lab"]["documents"], [])
        repeated = self.app.submit_flag(self.sid, 5, {"flag": first["lab"]["flag"]})
        self.assertEqual(repeated["flag_submission"], {"newly_completed": False, "points_awarded": 0})

    def test_all_difficulty_changes_keep_completion_original_flag_and_documents(self):
        first = self.complete()
        documents = copy.deepcopy(first["lab"]["documents"])
        for level in (1, 2, 0):
            with self.subTest(level=level):
                result = self.app.update_defense(self.sid, 5, {"level": level})
                self.assert_retained(result, first["lab"]["flag"])
                self.assert_no_current_verification(result)
                self.assertEqual(result["lab"]["documents"], documents)
                self.assertEqual(result["lab"]["defense_level"], level)
                self.assertFalse(result["lab"]["use_kb"])

    def test_unsubmitted_flags_remain_attempt_bound_and_do_not_become_completion(self):
        first = self.unlock()
        reset = self.app.reset(self.sid, 5)
        self.assertFalse(reset["lab"]["completed"])
        self.assertIsNone(reset["lab"]["flag"])
        with self.assertRaises(server.APIError) as error:
            self.app.submit_flag(self.sid, 5, {"flag": first["lab"]["flag"]})
        self.assertEqual(error.exception.status, 403)
        second = self.unlock()
        self.assertNotEqual(second["lab"]["flag"], first["lab"]["flag"])
        with self.assertRaises(server.APIError) as error:
            self.app.submit_flag(self.sid, 5, {"flag": first["lab"]["flag"]})
        self.assertEqual(error.exception.status, 400)

    def test_fresh_unqualified_response_does_not_revoke_previous_completion_or_claim_current_success(self):
        first = self.complete()
        self.app.reset(self.sid, 5)
        result = self.ask("I can explain boiler monitoring.")
        self.assert_retained(result, first["lab"]["flag"])
        self.assertFalse(result["lab"]["exploited"])
        self.assertFalse(result["lab"]["last_result"]["triggered"])
        self.assertNotIn("Exploit verified.", result["lab"]["last_result"]["message"])
        self.assertFalse(self.record()["lab5_verification_receipts"][-1]["verification_outcome"])

    def test_new_verified_attempt_keeps_earned_flag_and_cannot_reaward_points(self):
        first = self.complete()
        self.app.reset(self.sid, 5)
        captured = self.unlock()
        self.assert_retained(captured, first["lab"]["flag"])
        self.assertTrue(captured["lab"]["exploited"])
        self.assertTrue(captured["lab"]["last_result"]["triggered"])
        repeated = self.app.submit_flag(self.sid, 5, {"flag": first["lab"]["flag"]})
        self.assertEqual(repeated["flag_submission"]["points_awarded"], 0)

    def test_http_refresh_and_lab_switches_preserve_completion(self):
        first = self.complete()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.app))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        connection = http.client.HTTPConnection(*httpd.server_address)
        self.addCleanup(connection.close)
        for path in ("/api/state", "/lab/1", "/lab/2", "/lab/5", "/api/state"):
            connection.request("GET", path, headers={"Cookie": "nine_sid=" + self.sid})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            body = response.read()
            if path == "/api/state":
                result = json.loads(body)
                self.assertTrue(result["labs"]["5"]["completed"])
                self.assertEqual(result["labs"]["5"]["flag"], first["lab"]["flag"])
                self.assertEqual(result["summary"]["score"], 250)

    def test_completion_and_flag_survive_reopening_in_a_new_backend_process(self):
        first = self.complete()
        self.app.reset(self.sid, 5)
        code = """import json,sys
from pathlib import Path
import server
body=json.load(sys.stdin)
app=server.Application(Path(body['directory']))
print(json.dumps(app.submit_flag(body['sid'],5,{'flag':body['flag']})))
"""
        process = subprocess.run([sys.executable, "-c", code], cwd=catalog.ROOT,
            input=json.dumps({"directory": str(self.directory), "sid": self.sid, "flag": first["lab"]["flag"]}),
            text=True, capture_output=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr)
        restored = json.loads(process.stdout)
        self.assert_retained(restored, first["lab"]["flag"])
        self.assertEqual(restored["flag_submission"], {"newly_completed": False, "points_awarded": 0})

    def test_other_students_cannot_reuse_a_flag_before_or_after_unlocking_their_own(self):
        first = self.complete()
        other, _ = self.app.store.get(None)
        with self.assertRaises(server.APIError) as error:
            self.app.submit_flag(other, 5, {"flag": first["lab"]["flag"]})
        self.assertEqual(error.exception.status, 403)
        own = self.unlock(other)
        self.assertNotEqual(own["lab"]["flag"], first["lab"]["flag"])
        with self.assertRaises(server.APIError) as error:
            self.app.submit_flag(other, 5, {"flag": first["lab"]["flag"]})
        self.assertEqual(error.exception.status, 400)
        self.assertEqual(self.public(sid=other)["summary"]["score"], 0)
        self.assertNotIn(first["lab"]["flag"], json.dumps(self.public(sid=other)))
        self.assert_retained(self.public(), first["lab"]["flag"])

    def test_existing_completed_record_without_new_metadata_or_receipts_remains_valid(self):
        data = self.app.store.get(self.sid)[1]
        data["labs"]["5"].update(attempt=7, completed=True, exploited=True)
        expected = self.app.store.flag(self.sid, 5, 7)
        with self.app.store.connect() as db:
            db.execute("UPDATE sessions SET data=? WHERE sid=?", (json.dumps(data), self.sid))
        reopened = server.Application(self.directory)
        self.assert_retained(self.public(reopened), expected)
        self.assert_retained(reopened.reset(self.sid, 5), expected)
        self.assert_retained(reopened.update_defense(self.sid, 5, {"level": 2}), expected)
        repeated = reopened.submit_flag(self.sid, 5, {"flag": expected})
        self.assertEqual(repeated["flag_submission"]["points_awarded"], 0)

    def test_legacy_prefix_and_whitespace_still_use_the_existing_flag_digest(self):
        captured = self.unlock()["lab"]["flag"]
        legacy = "AIGOAT{" + captured[len("PANOPTIS{"):]
        result = self.app.submit_flag(self.sid, 5, {"flag": " \n" + legacy + " \n"})
        self.assert_retained(result, captured)
        self.assertEqual(result["flag_submission"]["points_awarded"], 250)

    def test_stale_session_save_cannot_remove_a_concurrent_completion(self):
        captured = self.unlock()
        stale = copy.deepcopy(self.app.store.get(self.sid)[1])
        self.app.submit_flag(self.sid, 5, {"flag": captured["lab"]["flag"]})
        server.Application(self.directory).store.save(self.sid, stale)
        self.assert_retained(self.public(), captured["lab"]["flag"])

    def test_stale_reset_snapshot_keeps_a_concurrently_earned_award(self):
        captured = self.unlock()
        stale = copy.deepcopy(self.app.store.get(self.sid)[1])
        stale["labs"]["5"] = catalog.fresh_lab(attempt=2, lab_id=5)
        self.app.submit_flag(self.sid, 5, {"flag": captured["lab"]["flag"]})
        server.Application(self.directory).store.save(self.sid, stale)
        self.assert_retained(self.public(), captured["lab"]["flag"])
        self.assert_no_current_verification(self.public())

    def test_stale_unrelated_save_cannot_restore_a_previous_lab5_attempt(self):
        first = self.complete()
        stale = copy.deepcopy(self.app.store.get(self.sid)[1])
        self.app.reset(self.sid, 5)
        self.app.store.save(self.sid, stale)
        self.assertEqual(self.record()["attempt"], 2)
        self.assert_no_current_verification(self.public())
        self.assert_retained(self.public(), first["lab"]["flag"])

    def test_flag_submission_transaction_preserves_other_labs_completed_concurrently(self):
        captured = self.unlock()
        stale = copy.deepcopy(self.app.store.get(self.sid)[1])
        concurrent = copy.deepcopy(stale)
        concurrent["labs"]["1"].update(completed=True, exploited=True)
        server.Application(self.directory).store.save(self.sid, concurrent)
        # Model a read that happened before another backend's completion. Flag
        # acceptance must read the authoritative row inside its transaction.
        with patch.object(self.app.store, "get", return_value=(self.sid, stale)):
            self.app.submit_flag(self.sid, 5, {"flag": captured["lab"]["flag"]})
        data = self.app.store.get(self.sid)[1]
        self.assertTrue(data["labs"]["1"]["completed"])
        self.assertEqual(self.app.summary(data)["score"], 350)

    def test_lab5_operations_preserve_all_previously_completed_other_labs(self):
        data = self.app.store.get(self.sid)[1]
        for lab_id in ["1", "2", "3", "4", "6", "7", "8", "9"]:
            data["labs"][lab_id].update(completed=True, exploited=True)
        self.app.store.save(self.sid, data)
        before = {key: copy.deepcopy(value) for key, value in data["labs"].items() if key != "5"}
        self.complete()
        self.app.reset(self.sid, 5)
        self.app.update_defense(self.sid, 5, {"level": 1})
        after = server.Application(self.directory).store.get(self.sid)[1]
        self.assertEqual({key: value for key, value in after["labs"].items() if key != "5"}, before)
        self.assertEqual(self.app.summary(after)["score"], 2100)

    def test_completion_writes_do_not_change_receipts_schema_or_flag_key(self):
        captured = self.unlock()
        receipts = copy.deepcopy(self.record()["lab5_verification_receipts"])
        with self.app.store.connect() as db:
            schema = db.execute("SELECT name,sql FROM sqlite_schema ORDER BY name").fetchall()
            key = db.execute("SELECT value FROM metadata WHERE name='flag_key'").fetchone()
        first = self.app.submit_flag(self.sid, 5, {"flag": captured["lab"]["flag"]})
        self.app.submit_flag(self.sid, 5, {"flag": captured["lab"]["flag"]})
        self.assertEqual(self.record()["lab5_verification_receipts"], receipts)
        self.assertNotIn("lab5_flag_attempt", first["lab"])
        self.assertNotIn("lab5_verification_receipts", first["lab"])
        with self.app.store.connect() as db:
            self.assertEqual(db.execute("SELECT name,sql FROM sqlite_schema ORDER BY name").fetchall(), schema)
            self.assertEqual(db.execute("SELECT value FROM metadata WHERE name='flag_key'").fetchone(), key)

    def test_old_completion_cannot_supply_current_attempt_verification(self):
        first = self.complete()
        old_record = copy.deepcopy(self.record())
        old_receipt = copy.deepcopy(old_record["lab5_verification_receipts"][-1])
        self.app.reset(self.sid, 5)
        one_turn = self.ask(LAB5_DISCLOSURE, message=LAB5_MESSAGES[2])
        self.assert_retained(one_turn, first["lab"]["flag"])
        self.assertFalse(one_turn["lab"]["exploited"])
        self.assertFalse(one_turn["lab"]["last_result"]["triggered"])
        self.assertEqual(one_turn["lab"]["last_result"]["escalation_status"], "requested")
        _, accepted = self.app.store.save_lab5_turn(self.sid, old_record, old_receipt)
        self.assertFalse(accepted)
        self.assert_retained(self.public(), first["lab"]["flag"])
        self.assertFalse(self.record()["exploited"])
        repeated = self.app.submit_flag(self.sid, 5, {"flag": first["lab"]["flag"]})
        self.assertEqual(repeated["flag_submission"]["points_awarded"], 0)

    def test_filtered_new_attempt_cannot_reverify_or_reaward_old_completion(self):
        first = self.complete()
        for level in (1, 2):
            self.app.update_defense(self.sid, 5, {"level": level})
            result = self.unlock()
            self.assert_retained(result, first["lab"]["flag"])
            self.assertFalse(result["lab"]["exploited"])
            self.assertFalse(result["lab"]["last_result"]["triggered"])
            self.assertEqual(result["lab"]["messages"][-1]["content"], catalog.LAB5_REFUSAL)
            self.assertFalse(self.record()["lab5_verification_receipts"][-1]["verification_outcome"])
            self.assertEqual(self.record()["lab5_flag_attempt"], 1)

    def test_verification_commit_preserves_flag_origin_from_latest_transaction_row(self):
        first = self.complete()
        self.app.reset(self.sid, 5)
        commit = self.app.store.save_lab5_turn
        def stale_origin(sid, record, receipt):
            record = copy.deepcopy(record)
            record.pop("lab5_flag_attempt", None)
            return commit(sid, record, receipt)
        with patch.object(self.app.store, "save_lab5_turn", side_effect=stale_origin):
            result = self.ask("Review the public maintenance checklist.")
        self.assert_retained(result, first["lab"]["flag"])
        self.assertEqual(self.record()["lab5_flag_attempt"], 1)
        self.assertFalse(result["lab"]["last_result"]["triggered"])


if __name__ == "__main__":
    unittest.main()
