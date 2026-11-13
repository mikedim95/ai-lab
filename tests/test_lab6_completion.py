"""Lab 6 completion transitions with existing verified fixtures and isolated stores."""
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


from test_app import LAB6_IDENTITY_REQUEST, LAB6_ADOPTED_REPLY
FIXTURE = {"student_message": LAB6_IDENTITY_REQUEST, "successful_reply": LAB6_ADOPTED_REPLY}


class Lab6CompletionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.app = server.Application(self.directory)
        self.sid, _ = self.app.store.get(None)

    def record(self, app=None, sid=None):
        return (app or self.app).store.get(sid or self.sid)[1]["labs"]["6"]

    def public(self, app=None, sid=None):
        app = app or self.app
        sid = sid or self.sid
        return app.lab_result(sid, 6, app.store.get(sid)[1])

    def unlock(self, sid=None):
        sid = sid or self.sid
        return self.ask(FIXTURE["successful_reply"], sid=sid, message=LAB6_IDENTITY_REQUEST)

    def ask(self, response, sid=None, during=None, message=LAB6_IDENTITY_REQUEST):
        def transport(request, timeout):
            self.assertEqual(request.full_url, server.OLLAMA_URL + "/api/chat")
            if during:
                during()
            return io.BytesIO(json.dumps({"message": {"content": response}}).encode())
        with patch("server.urllib.request.urlopen", side_effect=transport):
            return self.app.chat(sid or self.sid, 6, {"message": message})

    def complete(self):
        captured = self.unlock()
        return self.app.submit_flag(self.sid, 6, {"flag": captured["lab"]["flag"]})

    def assert_retained(self, result, flag):
        self.assertIs(result["lab"]["completed"], True)
        self.assertEqual(result["lab"]["flag"], flag)
        self.assertEqual(result["summary"]["score"], 200)
        self.assertEqual(result["summary"]["completed"], [6])

    def assert_reset(self, result):
        self.assertFalse(result["lab"]["completed"])
        self.assertFalse(result["lab"]["exploited"])
        self.assertIsNone(result["lab"]["flag"])
        self.assertEqual(result["summary"]["score"], 0)
        self.assertEqual(result["summary"]["completed"], [])

    def assert_no_current_verification(self, result):
        self.assertFalse(result["lab"]["exploited"])
        self.assertIsNone(result["lab"]["last_result"])
        self.assertEqual(result["lab"]["messages"], [])

    def test_flag_is_locked_before_verified_exploitation_even_with_a_computed_correct_flag(self):
        public = self.public()
        self.assertIsNone(public["lab"]["flag"])
        with self.assertRaises(server.APIError) as error:
            self.app.submit_flag(self.sid, 6, {"flag": self.app.store.flag(self.sid, 6, 1),
                "completed": True, "exploited": True, "lab6_flag_attempt": 1})
        self.assertEqual(error.exception.status, 403)
        self.assertEqual(self.public()["summary"]["score"], 0)

    def test_verified_existing_fixture_unlocks_flag_without_automatic_completion(self):
        captured = self.unlock()
        self.assertTrue(captured["lab"]["last_result"]["triggered"])
        self.assertTrue(captured["lab"]["exploited"])
        self.assertFalse(captured["lab"]["completed"])
        self.assertEqual(captured["summary"]["score"], 0)
        self.assertEqual(captured["lab"]["flag"], self.app.store.flag(self.sid, 6, 1))
        self.assertRegex(captured["lab"]["flag"], r"^PANOPTIS\{[A-F0-9]{32}\}$")
        receipt = self.record()["lab6_verification_receipts"][-1]
        self.assertTrue(receipt["verification_outcome"])
        self.assertEqual(receipt["generation_source"], "production_ollama")

    def test_correct_flag_has_one_authoritative_200_point_transition(self):
        result = self.complete()
        self.assert_retained(result, self.app.store.flag(self.sid, 6, 1))
        self.assertEqual(result["flag_submission"], {"newly_completed": True, "points_awarded": 200})
        self.assertEqual(catalog.CATALOG[5]["points"], 200)

    def test_duplicate_submissions_are_noop_transitions(self):
        first = self.complete()
        with self.app.store.connect() as db:
            before = db.execute("SELECT data,updated FROM sessions WHERE sid=?", (self.sid,)).fetchone()
        for _ in range(6):
            result = self.app.submit_flag(self.sid, 6, {"flag": first["lab"]["flag"]})
            self.assert_retained(result, first["lab"]["flag"])
            self.assertEqual(result["flag_submission"], {"newly_completed": False, "points_awarded": 0})
        with self.app.store.connect() as db:
            self.assertEqual(db.execute("SELECT data,updated FROM sessions WHERE sid=?", (self.sid,)).fetchone(), before)

    def test_incorrect_flags_do_not_modify_saved_state_or_score_before_or_after_award(self):
        captured = self.unlock()
        for completed in (False, True):
            if completed:
                self.app.submit_flag(self.sid, 6, {"flag": captured["lab"]["flag"]})
            before = copy.deepcopy(self.app.store.get(self.sid)[1])
            for candidate in ("PANOPTIS{wrong}", "", "PANOPTIS{🙂}", "PANOPTIS{\ud800}", None, 123, True, "x" * 151):
                with self.subTest(completed=completed, candidate=repr(candidate)):
                    with self.assertRaises(server.APIError) as error:
                        self.app.submit_flag(self.sid, 6, {"flag": candidate})
                    self.assertEqual(error.exception.status, 400)
                    self.assertEqual(self.app.store.get(self.sid)[1], before)

    def test_concurrent_submissions_across_backends_award_exactly_once(self):
        flag = self.unlock()["lab"]["flag"]
        apps = [server.Application(self.directory) for _ in range(8)]
        ready = threading.Barrier(len(apps))
        def submit(app):
            ready.wait(timeout=5)
            return app.submit_flag(self.sid, 6, {"flag": flag})
        with ThreadPoolExecutor(max_workers=len(apps)) as pool:
            results = list(pool.map(submit, apps))
        self.assertEqual(sum(item["flag_submission"]["points_awarded"] for item in results), 200)
        self.assertEqual(sum(item["flag_submission"]["newly_completed"] for item in results), 1)
        for result in results:
            self.assert_retained(result, flag)

    def test_eight_backend_processes_share_one_atomic_200_point_award(self):
        flag = self.unlock()["lab"]["flag"]
        barrier = self.directory / "process-barrier"
        barrier.mkdir()
        code = """import json,os,sys,time
from pathlib import Path
import server
body=json.load(sys.stdin)
app=server.Application(Path(body['directory']))
barrier=Path(body['barrier'])
(barrier / ('ready-' + str(os.getpid()))).touch()
deadline=time.monotonic()+10
while len(list(barrier.glob('ready-*'))) < 8:
    if time.monotonic() > deadline:
        raise RuntimeError('Concurrent submission barrier timed out')
    time.sleep(0.01)
print(json.dumps(app.submit_flag(body['sid'],6,{'flag':body['flag']})))
"""
        body = json.dumps({"directory": str(self.directory), "barrier": str(barrier),
                           "sid": self.sid, "flag": flag})
        def submit(_):
            process = subprocess.run([sys.executable, "-S", "-c", code], cwd=catalog.ROOT,
                input=body, text=True, capture_output=True, timeout=20)
            self.assertEqual(process.returncode, 0, process.stderr)
            return json.loads(process.stdout)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, range(8)))
        self.assertEqual(sum(item["flag_submission"]["points_awarded"] for item in results), 200)
        self.assertEqual(sum(item["flag_submission"]["newly_completed"] for item in results), 1)
        self.assert_retained(self.public(server.Application(self.directory)), flag)

    def test_mixed_concurrent_correct_and_incorrect_submissions_do_not_duplicate_awards(self):
        flag = self.unlock()["lab"]["flag"]
        apps = [server.Application(self.directory) for _ in range(6)]
        ready = threading.Barrier(len(apps))
        def submit(pair):
            number, app = pair
            ready.wait(timeout=5)
            try:
                return app.submit_flag(self.sid, 6, {"flag": flag if number % 2 else "PANOPTIS{wrong}"})
            except server.APIError as error:
                return error.status
        with ThreadPoolExecutor(max_workers=len(apps)) as pool:
            results = list(pool.map(submit, enumerate(apps)))
        self.assertEqual(results.count(400), 3)
        successes = [item for item in results if isinstance(item, dict)]
        self.assertEqual(sum(item["flag_submission"]["points_awarded"] for item in successes), 200)
        self.assert_retained(self.public(), flag)

    def test_restart_revokes_completion_flag_and_points(self):
        first = self.complete()
        result = self.app.reset(self.sid, 6)
        self.assert_reset(result)
        self.assert_no_current_verification(result)
        self.assertGreater(result["lab"]["attempt"], first["lab"]["attempt"])
        self.assertEqual(result["lab"]["documents"], [])
        with self.assertRaises(server.APIError) as error:
            self.app.submit_flag(self.sid, 6, {"flag": first["lab"]["flag"]})
        self.assertEqual(error.exception.status, 403)

    def test_all_difficulty_changes_keep_completion_original_flag_and_documents(self):
        first = self.complete()
        documents = copy.deepcopy(first["lab"]["documents"])
        for level in (1, 2, 0):
            with self.subTest(level=level):
                result = self.app.update_defense(self.sid, 6, {"level": level})
                self.assert_retained(result, first["lab"]["flag"])
                self.assert_no_current_verification(result)
                self.assertEqual(result["lab"]["documents"], documents)
                self.assertEqual(result["lab"]["defense_level"], level)
                self.assertFalse(result["lab"]["use_kb"])

    def test_unsubmitted_flags_remain_attempt_bound_and_do_not_become_completion(self):
        first = self.unlock()
        reset = self.app.reset(self.sid, 6)
        self.assertFalse(reset["lab"]["completed"])
        self.assertIsNone(reset["lab"]["flag"])
        with self.assertRaises(server.APIError) as error:
            self.app.submit_flag(self.sid, 6, {"flag": first["lab"]["flag"]})
        self.assertEqual(error.exception.status, 403)
        second = self.unlock()
        self.assertNotEqual(second["lab"]["flag"], first["lab"]["flag"])
        with self.assertRaises(server.APIError) as error:
            self.app.submit_flag(self.sid, 6, {"flag": first["lab"]["flag"]})
        self.assertEqual(error.exception.status, 400)

    def test_fresh_unqualified_response_does_not_revoke_previous_completion_or_claim_current_success(self):
        first = self.complete()
        self.app.update_defense(self.sid, 6, {"level": 1})
        self.app.update_defense(self.sid, 6, {"level": 0})
        result = self.ask("I can explain boiler monitoring.")
        self.assert_retained(result, first["lab"]["flag"])
        self.assertFalse(result["lab"]["exploited"])
        self.assertFalse(result["lab"]["last_result"]["triggered"])
        self.assertNotIn("Exploit verified.", result["lab"]["last_result"]["message"])
        self.assertFalse(self.record()["lab6_verification_receipts"][-1]["verification_outcome"])

    def test_restart_allows_a_new_flag_to_earn_points_once(self):
        first = self.complete()
        self.app.reset(self.sid, 6)
        captured = self.unlock()
        self.assertFalse(captured["lab"]["completed"])
        self.assertTrue(captured["lab"]["exploited"])
        self.assertEqual(captured["summary"]["score"], 0)
        self.assertNotEqual(captured["lab"]["flag"], first["lab"]["flag"])
        with self.assertRaises(server.APIError) as error:
            self.app.submit_flag(self.sid, 6, {"flag": first["lab"]["flag"]})
        self.assertEqual(error.exception.status, 400)
        earned = self.app.submit_flag(self.sid, 6, {"flag": captured["lab"]["flag"]})
        self.assert_retained(earned, captured["lab"]["flag"])
        self.assertEqual(earned["flag_submission"]["points_awarded"], catalog.CATALOG[5]["points"])
        repeated = self.app.submit_flag(self.sid, 6, {"flag": captured["lab"]["flag"]})
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
        for path in ("/api/state", "/lab/1", "/lab/2", "/lab/6", "/api/state"):
            connection.request("GET", path, headers={"Cookie": "nine_sid=" + self.sid})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            body = response.read()
            if path == "/api/state":
                result = json.loads(body)
                self.assertTrue(result["labs"]["6"]["completed"])
                self.assertEqual(result["labs"]["6"]["flag"], first["lab"]["flag"])
                self.assertEqual(result["summary"]["score"], 200)

    def test_http_first_duplicate_invalid_and_cross_student_submissions(self):
        flag = self.unlock()["lab"]["flag"]
        other, _ = self.app.store.get(None)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.app))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        connection = http.client.HTTPConnection(*httpd.server_address)
        self.addCleanup(connection.close)
        for sid, candidate, status, award in [
            (self.sid, flag, 200, {"newly_completed": True, "points_awarded": 200}),
            (self.sid, flag, 200, {"newly_completed": False, "points_awarded": 0}),
            (self.sid, "PANOPTIS{wrong}", 400, None),
            (other, flag, 403, None),
        ]:
            connection.request("POST", "/api/labs/6/flag", body=json.dumps({"flag": candidate}),
                headers={"Content-Type": "application/json", "Cookie": "nine_sid=" + sid})
            response = connection.getresponse()
            self.assertEqual(response.status, status)
            result = json.loads(response.read())
            if award is not None:
                self.assertEqual(result["flag_submission"], award)
                self.assertEqual(result["summary"]["score"], 200)
            else:
                self.assertTrue(result["error"])
        self.assert_retained(self.public(), flag)

    def test_completion_and_flag_survive_reopening_in_a_new_backend_process(self):
        first = self.complete()
        code = """import json,sys
from pathlib import Path
import server
body=json.load(sys.stdin)
app=server.Application(Path(body['directory']))
print(json.dumps(app.submit_flag(body['sid'],6,{'flag':body['flag']})))
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
            self.app.submit_flag(other, 6, {"flag": first["lab"]["flag"]})
        self.assertEqual(error.exception.status, 403)
        own = self.unlock(other)
        self.assertNotEqual(own["lab"]["flag"], first["lab"]["flag"])
        with self.assertRaises(server.APIError) as error:
            self.app.submit_flag(other, 6, {"flag": first["lab"]["flag"]})
        self.assertEqual(error.exception.status, 400)
        self.assertEqual(self.public(sid=other)["summary"]["score"], 0)
        self.assertNotIn(first["lab"]["flag"], json.dumps(self.public(sid=other)))
        self.assert_retained(self.public(), first["lab"]["flag"])

    def test_existing_completed_record_without_new_metadata_or_receipts_remains_valid(self):
        data = self.app.store.get(self.sid)[1]
        data["labs"]["6"].update(attempt=7, completed=True, exploited=True)
        expected = self.app.store.flag(self.sid, 6, 7)
        with self.app.store.connect() as db:
            db.execute("UPDATE sessions SET data=? WHERE sid=?", (json.dumps(data), self.sid))
        reopened = server.Application(self.directory)
        self.assert_retained(self.public(reopened), expected)
        self.assert_retained(reopened.update_defense(self.sid, 6, {"level": 2}), expected)
        repeated = reopened.submit_flag(self.sid, 6, {"flag": expected})
        self.assertEqual(repeated["flag_submission"]["points_awarded"], 0)

    def test_legacy_prefix_and_whitespace_still_use_the_existing_flag_digest(self):
        captured = self.unlock()["lab"]["flag"]
        legacy = "AIGOAT{" + captured[len("PANOPTIS{"):]
        result = self.app.submit_flag(self.sid, 6, {"flag": " \n" + legacy + " \n"})
        self.assert_retained(result, captured)
        self.assertEqual(result["flag_submission"]["points_awarded"], 200)

    def test_stale_session_save_cannot_remove_a_concurrent_completion(self):
        captured = self.unlock()
        stale = copy.deepcopy(self.app.store.get(self.sid)[1])
        self.app.submit_flag(self.sid, 6, {"flag": captured["lab"]["flag"]})
        server.Application(self.directory).store.save(self.sid, stale)
        self.assert_retained(self.public(), captured["lab"]["flag"])

    def test_stale_reset_snapshot_keeps_a_concurrently_earned_award(self):
        captured = self.unlock()
        stale = copy.deepcopy(self.app.store.get(self.sid)[1])
        stale["labs"]["6"] = catalog.fresh_lab(attempt=2, lab_id=6)
        self.app.submit_flag(self.sid, 6, {"flag": captured["lab"]["flag"]})
        server.Application(self.directory).store.save(self.sid, stale)
        self.assert_retained(self.public(), captured["lab"]["flag"])
        self.assert_no_current_verification(self.public())

    def test_stale_unrelated_save_cannot_restore_a_previous_lab6_attempt(self):
        first = self.complete()
        stale = copy.deepcopy(self.app.store.get(self.sid)[1])
        self.app.reset(self.sid, 6)
        self.app.store.save(self.sid, stale)
        self.assertEqual(self.record()["attempt"], 2)
        self.assert_no_current_verification(self.public())
        self.assert_reset(self.public())

    def test_flag_submission_transaction_preserves_other_labs_completed_concurrently(self):
        captured = self.unlock()
        stale = copy.deepcopy(self.app.store.get(self.sid)[1])
        concurrent = copy.deepcopy(stale)
        concurrent["labs"]["1"].update(completed=True, exploited=True)
        server.Application(self.directory).store.save(self.sid, concurrent)
        # Model a read that happened before another backend's completion. Flag
        # acceptance must read the authoritative row inside its transaction.
        with patch.object(self.app.store, "get", return_value=(self.sid, stale)):
            self.app.submit_flag(self.sid, 6, {"flag": captured["lab"]["flag"]})
        data = self.app.store.get(self.sid)[1]
        self.assertTrue(data["labs"]["1"]["completed"])
        self.assertEqual(self.app.summary(data)["score"], 300)

    def test_lab6_operations_preserve_all_previously_completed_other_labs(self):
        data = self.app.store.get(self.sid)[1]
        for lab_id in ["1", "2", "3", "4", "5", "7", "8", "9"]:
            data["labs"][lab_id].update(completed=True, exploited=True)
        self.app.store.save(self.sid, data)
        before = {key: copy.deepcopy(value) for key, value in data["labs"].items() if key != "6"}
        self.complete()
        self.app.reset(self.sid, 6)
        self.app.update_defense(self.sid, 6, {"level": 1})
        after = server.Application(self.directory).store.get(self.sid)[1]
        self.assertEqual({key: value for key, value in after["labs"].items() if key != "6"}, before)
        self.assertEqual(self.app.summary(after)["score"], 2100 - catalog.CATALOG[5]["points"])

    def test_completion_writes_do_not_change_receipts_schema_or_flag_key(self):
        captured = self.unlock()
        receipts = copy.deepcopy(self.record()["lab6_verification_receipts"])
        with self.app.store.connect() as db:
            schema = db.execute("SELECT name,sql FROM sqlite_schema ORDER BY name").fetchall()
            key = db.execute("SELECT value FROM metadata WHERE name='flag_key'").fetchone()
        first = self.app.submit_flag(self.sid, 6, {"flag": captured["lab"]["flag"]})
        self.app.submit_flag(self.sid, 6, {"flag": captured["lab"]["flag"]})
        self.assertEqual(self.record()["lab6_verification_receipts"], receipts)
        self.assertNotIn("lab6_flag_attempt", first["lab"])
        self.assertNotIn("lab6_verification_receipts", first["lab"])
        with self.app.store.connect() as db:
            self.assertEqual(db.execute("SELECT name,sql FROM sqlite_schema ORDER BY name").fetchall(), schema)
            self.assertEqual(db.execute("SELECT value FROM metadata WHERE name='flag_key'").fetchone(), key)

    def test_old_completion_cannot_supply_current_attempt_verification(self):
        first = self.complete()
        old_record = copy.deepcopy(self.record())
        old_receipt = copy.deepcopy(old_record["lab6_verification_receipts"][-1])
        self.app.update_defense(self.sid, 6, {"level": 1})
        self.app.update_defense(self.sid, 6, {"level": 0})
        one_turn = self.ask(catalog.LAB6_REFUSAL, message=LAB6_IDENTITY_REQUEST)
        self.assert_retained(one_turn, first["lab"]["flag"])
        self.assertFalse(one_turn["lab"]["exploited"])
        self.assertFalse(one_turn["lab"]["last_result"]["triggered"])
        self.assertEqual(one_turn["lab"]["last_result"]["identity_status"], "refused")
        _, accepted = self.app.store.save_lab6_turn(self.sid, old_record, old_receipt)
        self.assertFalse(accepted)
        self.assert_retained(self.public(), first["lab"]["flag"])
        self.assertFalse(self.record()["exploited"])
        repeated = self.app.submit_flag(self.sid, 6, {"flag": first["lab"]["flag"]})
        self.assertEqual(repeated["flag_submission"]["points_awarded"], 0)

    def test_filtered_new_attempt_cannot_reverify_or_reaward_old_completion(self):
        first = self.complete()
        for level in (1, 2):
            self.app.update_defense(self.sid, 6, {"level": level})
            result = self.unlock()
            self.assert_retained(result, first["lab"]["flag"])
            self.assertFalse(result["lab"]["exploited"])
            self.assertFalse(result["lab"]["last_result"]["triggered"])
            self.assertEqual(result["lab"]["messages"][-1]["content"], catalog.LAB6_REFUSAL)
            self.assertFalse(self.record()["lab6_verification_receipts"][-1]["verification_outcome"])
            self.assertEqual(self.record()["lab6_flag_attempt"], 1)

    def test_verification_commit_preserves_flag_origin_from_latest_transaction_row(self):
        first = self.complete()
        self.app.update_defense(self.sid, 6, {"level": 1})
        self.app.update_defense(self.sid, 6, {"level": 0})
        commit = self.app.store.save_lab6_turn
        def stale_origin(sid, record, receipt, **kwargs):
            record = copy.deepcopy(record)
            record.pop("lab6_flag_attempt", None)
            return commit(sid, record, receipt, **kwargs)
        with patch.object(self.app.store, "save_lab6_turn", side_effect=stale_origin):
            result = self.ask("Review the public maintenance checklist.")
        self.assert_retained(result, first["lab"]["flag"])
        self.assertEqual(self.record()["lab6_flag_attempt"], 1)
        self.assertFalse(result["lab"]["last_result"]["triggered"])


if __name__ == "__main__":
    unittest.main()
