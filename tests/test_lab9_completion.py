"""Lab 9 durable awards using existing verification fixtures and temporary SQLite."""
from concurrent.futures import ThreadPoolExecutor
import copy
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import catalog
import server
from test_lab9_evidence import DISCLOSURE, NORMAL, QUESTIONS, REFUSALS


class Lab9CompletionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.app = server.Application(self.directory)
        self.sid, _ = self.app.store.get(None)

    def record(self, app=None, sid=None):
        return (app or self.app).store.get(sid or self.sid)[1]["labs"]["9"]

    def public(self, app=None, sid=None):
        app, sid = app or self.app, sid or self.sid
        return app.lab_result(sid, 9, app.store.get(sid)[1])

    def ask(self, reply=NORMAL, message=QUESTIONS[2], sid=None, during=None):
        def transport(request, timeout):
            self.assertEqual(request.full_url, server.OLLAMA_URL + "/api/chat")
            if during:
                during()
            return io.BytesIO(json.dumps({"message": {"content": reply}}).encode())
        with patch("server.urllib.request.urlopen", side_effect=transport):
            return self.app.chat(sid or self.sid, 9, {"message": message})

    def unlock(self, sid=None):
        for message, reply in zip(QUESTIONS, REFUSALS):
            self.ask(reply, message, sid=sid)
        return self.ask(DISCLOSURE, sid=sid)

    def complete(self):
        captured = self.unlock()
        return self.app.submit_flag(self.sid, 9, {"flag": captured["lab"]["flag"]})

    def assert_retained(self, result, flag):
        self.assertIs(result["lab"]["completed"], True)
        self.assertEqual(result["lab"]["flag"], flag)
        self.assertEqual(result["summary"]["score"], 500)
        self.assertEqual(result["summary"]["completed"], [9])

    def assert_reset(self, result):
        self.assertFalse(result["lab"]["completed"])
        self.assertFalse(result["lab"]["exploited"])
        self.assertIsNone(result["lab"]["flag"])
        self.assertEqual(result["summary"]["score"], 0)
        self.assertEqual(result["summary"]["completed"], [])

    def assert_new_attempt(self, result):
        self.assertFalse(result["lab"]["exploited"])
        self.assertIsNone(result["lab"]["last_result"])
        self.assertEqual(result["lab"]["messages"], [])
        self.assertEqual(result["lab"]["guardrail_status"], "normal")
        self.assertEqual(result["lab"]["state_version"][1], 0)

    def test_first_correct_flag_awards_exactly_500(self):
        captured = self.unlock()
        self.assertFalse(captured["lab"]["completed"])
        self.assertEqual(captured["summary"]["score"], 0)
        result = self.app.submit_flag(self.sid, 9, {"flag": captured["lab"]["flag"]})
        self.assert_retained(result, captured["lab"]["flag"])
        self.assertEqual(result["flag_submission"], {"newly_completed": True, "points_awarded": 500})
        self.assertEqual(self.record()["lab9_flag_attempt"], 1)
        self.assertEqual(catalog.CATALOG[8]["points"], 500)

    def test_duplicate_and_replayed_submissions_award_zero_and_do_not_write(self):
        first = self.complete()
        with self.app.store.connect() as db:
            before = db.execute("SELECT data,updated FROM sessions WHERE sid=?", (self.sid,)).fetchone()
        for _ in range(8):
            result = self.app.submit_flag(self.sid, 9, {"flag": first["lab"]["flag"]})
            self.assert_retained(result, first["lab"]["flag"])
            self.assertEqual(result["flag_submission"], {"newly_completed": False, "points_awarded": 0})
        with self.app.store.connect() as db:
            self.assertEqual(db.execute("SELECT data,updated FROM sessions WHERE sid=?", (self.sid,)).fetchone(), before)

    def test_eight_independent_backend_instances_award_exactly_once(self):
        flag = self.unlock()["lab"]["flag"]
        apps = [server.Application(self.directory) for _ in range(8)]
        ready = threading.Barrier(8)
        def submit(app):
            ready.wait(timeout=10)
            return app.submit_flag(self.sid, 9, {"flag": flag})
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, apps))
        self.assertEqual(sum(result["flag_submission"]["points_awarded"] for result in results), 500)
        self.assertEqual(sum(result["flag_submission"]["newly_completed"] for result in results), 1)
        for result in results:
            self.assert_retained(result, flag)

    def test_eight_backend_processes_award_exactly_once(self):
        flag = self.unlock()["lab"]["flag"]
        barrier = self.directory / "barrier"
        barrier.mkdir()
        code = """import json,os,sys,time
from pathlib import Path
import server
body=json.load(sys.stdin)
app=server.Application(Path(body['directory']))
barrier=Path(body['barrier'])
(barrier/('ready-'+str(os.getpid()))).touch()
deadline=time.monotonic()+15
while len(list(barrier.glob('ready-*'))) < 8:
    if time.monotonic()>deadline:raise RuntimeError('Submission barrier timed out')
    time.sleep(.01)
result=app.submit_flag(body['sid'],9,{'flag':body['flag']})
print(json.dumps({'flag_submission':result['flag_submission'],'score':result['summary']['score'],'completed':result['lab']['completed']}))
"""
        body = json.dumps({"directory": str(self.directory), "barrier": str(barrier), "sid": self.sid, "flag": flag})
        def submit(_):
            result = subprocess.run([sys.executable, "-S", "-B", "-c", code], cwd=catalog.ROOT,
                                    input=body, text=True, capture_output=True, timeout=25)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, range(8)))
        self.assertEqual(sum(result["flag_submission"]["points_awarded"] for result in results), 500)
        self.assertEqual(sum(result["flag_submission"]["newly_completed"] for result in results), 1)
        self.assertTrue(all(result["score"] == 500 and result["completed"] for result in results))

    def test_mixed_concurrent_invalid_flags_cannot_duplicate_the_award(self):
        flag = self.unlock()["lab"]["flag"]
        apps = [server.Application(self.directory) for _ in range(8)]
        ready = threading.Barrier(8)
        def submit(pair):
            index, app = pair
            ready.wait(timeout=10)
            try:
                return app.submit_flag(self.sid, 9, {"flag": flag if index % 2 else "PANOPTIS{wrong}"})
            except server.APIError as error:
                return error.status
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, enumerate(apps)))
        self.assertEqual(results.count(400), 4)
        self.assertEqual(sum(result["flag_submission"]["points_awarded"] for result in results if isinstance(result, dict)), 500)
        self.assert_retained(self.public(), flag)

    def test_invalid_flags_leave_state_unchanged_before_and_after_award(self):
        flag = self.unlock()["lab"]["flag"]
        for completed in (False, True):
            if completed:
                self.app.submit_flag(self.sid, 9, {"flag": flag})
            before = copy.deepcopy(self.app.store.get(self.sid)[1])
            for candidate in ("", "PANOPTIS{wrong}", "PANOPTIS{🙂}", "PANOPTIS{\ud800}", None, True, 123, "x" * 151):
                with self.subTest(completed=completed, kind=type(candidate).__name__):
                    with self.assertRaises(server.APIError) as error:
                        self.app.submit_flag(self.sid, 9, {"flag": candidate})
                    self.assertEqual(error.exception.status, 400)
                    self.assertEqual(self.app.store.get(self.sid)[1], before)

    def test_refresh_and_backend_restart_restore_completion(self):
        first = self.complete()
        reopened = server.Application(self.directory)
        state = reopened.refresh_state(self.sid)
        self.assert_retained({"lab": state["labs"]["9"], "summary": state["summary"]}, first["lab"]["flag"])
        repeated = reopened.submit_flag(self.sid, 9, {"flag": first["lab"]["flag"]})
        self.assertEqual(repeated["flag_submission"]["points_awarded"], 0)

    def test_restart_clears_completion_original_flag_and_private_progress(self):
        first = self.complete()
        result = self.app.reset(self.sid, 9)
        self.assert_reset(result)
        self.assert_new_attempt(result)
        self.assertEqual(result["lab"]["state_version"], [2, 0])
        self.assertNotIn("lab9_flag_attempt", self.record())
        self.assertEqual(self.record().get("lab9_verification_receipts", []), [])

    def test_all_difficulty_changes_preserve_completion_without_verification(self):
        first = self.complete()
        for level in (1, 2, 0):
            result = self.app.update_defense(self.sid, 9, {"level": level})
            self.assert_retained(result, first["lab"]["flag"])
            self.assert_new_attempt(result)
            self.assertEqual(result["lab"]["defense_level"], level)
            self.assertEqual(self.record()["lab9_flag_attempt"], 1)

    def test_revoked_flag_stays_invalid_after_difficulty_change_and_backend_restart(self):
        flag = self.complete()["lab"]["flag"]
        self.app.reset(self.sid, 9)
        self.app.update_defense(self.sid, 9, {"level": 2})
        reopened = server.Application(self.directory)
        with self.assertRaises(server.APIError) as error:
            reopened.submit_flag(self.sid, 9, {"flag": flag})
        self.assertEqual(error.exception.status, 403)
        self.assert_reset(self.public(reopened))

    def test_unsubmitted_flags_are_still_bound_to_the_current_verified_attempt(self):
        old = self.unlock()["lab"]["flag"]
        reset = self.app.reset(self.sid, 9)
        self.assertFalse(reset["lab"]["completed"])
        self.assertIsNone(reset["lab"]["flag"])
        with self.assertRaises(server.APIError) as error:
            self.app.submit_flag(self.sid, 9, {"flag": old})
        self.assertEqual(error.exception.status, 403)
        current = self.unlock()["lab"]["flag"]
        self.assertNotEqual(current, old)
        with self.assertRaises(server.APIError) as error:
            self.app.submit_flag(self.sid, 9, {"flag": old})
        self.assertEqual(error.exception.status, 400)
        self.assertEqual(self.app.submit_flag(self.sid, 9, {"flag": current})["flag_submission"]["points_awarded"], 500)

    def test_same_version_stale_session_write_cannot_erase_completion(self):
        flag = self.unlock()["lab"]["flag"]
        stale = copy.deepcopy(self.app.store.get(self.sid)[1])
        self.app.submit_flag(self.sid, 9, {"flag": flag})
        server.Application(self.directory).store.save(self.sid, stale)
        self.assert_retained(self.public(), flag)
        self.assertEqual(self.record()["lab9_flag_attempt"], 1)

    def test_stale_reset_snapshot_preserves_a_concurrently_earned_award(self):
        flag = self.unlock()["lab"]["flag"]
        snapshot = copy.deepcopy(self.record())
        self.app.submit_flag(self.sid, 9, {"flag": flag})
        fresh = catalog.fresh_lab(attempt=2, lab_id=9)
        app = server.Application(self.directory)
        data = app.store.save_lab9(self.sid, fresh, snapshot)
        result = app.lab_result(self.sid, 9, data)
        self.assert_retained(result, flag)
        self.assert_new_attempt(result)

    def test_old_attempt_stale_write_cannot_restore_old_verification(self):
        first = self.complete()
        stale = copy.deepcopy(self.app.store.get(self.sid)[1])
        self.app.reset(self.sid, 9)
        self.app.store.save(self.sid, stale)
        result = self.public()
        self.assert_reset(result)
        self.assert_new_attempt(result)
        self.assertEqual(result["lab"]["state_version"], [2, 0])

    def test_first_award_verification_runs_inside_the_sqlite_write_transaction(self):
        flag = self.unlock()["lab"]["flag"]
        check, locks = server._lab9_current, []
        def verified(sid, record):
            if not record["completed"]:
                with sqlite3.connect(self.app.store.path, timeout=0) as competing:
                    try:
                        competing.execute("BEGIN IMMEDIATE")
                    except sqlite3.OperationalError as error:
                        self.assertIn("locked", str(error))
                        locks.append(True)
                    else:
                        locks.append(False)
            return check(sid, record)
        with patch("server._lab9_current", side_effect=verified):
            result = self.app.submit_flag(self.sid, 9, {"flag": flag})
        self.assertEqual(locks, [True])
        self.assertEqual(result["flag_submission"]["points_awarded"], 500)

    def test_transaction_reads_latest_attempt_instead_of_a_stale_application_snapshot(self):
        flag = self.unlock()["lab"]["flag"]
        stale = copy.deepcopy(self.app.store.get(self.sid)[1])
        server.Application(self.directory).reset(self.sid, 9)
        with patch.object(self.app.store, "get", return_value=(self.sid, stale)):
            with self.assertRaises(server.APIError) as error:
                self.app.submit_flag(self.sid, 9, {"flag": flag})
        self.assertEqual(error.exception.status, 403)
        self.assertFalse(self.record()["completed"])

    def test_cross_student_flag_cannot_complete_another_student(self):
        first = self.complete()
        other, _ = self.app.store.get(None)
        other_flag = self.unlock(sid=other)["lab"]["flag"]
        before = copy.deepcopy(self.app.store.get(other)[1])
        with self.assertRaises(server.APIError) as error:
            self.app.submit_flag(other, 9, {"flag": first["lab"]["flag"]})
        self.assertEqual(error.exception.status, 400)
        self.assertEqual(self.app.store.get(other)[1], before)
        self.assertNotEqual(other_flag, first["lab"]["flag"])
        self.assert_retained(self.public(), first["lab"]["flag"])

    def test_previous_completion_never_verifies_a_new_attempt(self):
        first = self.complete()
        self.app.reset(self.sid, 9)
        result = self.ask(DISCLOSURE)
        self.assert_reset(result)
        self.assertFalse(result["lab"]["exploited"])
        self.assertFalse(result["lab"]["last_result"]["triggered"])
        self.assertEqual(result["lab"]["guardrail_status"], "normal")
        self.assertFalse(server._lab9_current(self.sid, self.record()))

    def test_one_refusal_does_not_unlock_first_award(self):
        self.ask(REFUSALS[0])
        result = self.ask(DISCLOSURE)
        self.assertFalse(result["lab"]["exploited"])
        self.assertIsNone(result["lab"]["flag"])
        flag = self.app.store.flag(self.sid, 9, 1)
        with self.assertRaises(server.APIError) as error:
            self.app.submit_flag(self.sid, 9, {"flag": flag})
        self.assertEqual(error.exception.status, 403)

    def test_legacy_exploited_boolean_is_not_first_award_evidence(self):
        _, data = self.app.store.get(self.sid)
        data["labs"]["9"]["exploited"] = True
        self.app.store.save(self.sid, data)
        with self.assertRaises(server.APIError) as error:
            self.app.submit_flag(self.sid, 9, {"flag": self.app.store.flag(self.sid, 9, 1)})
        self.assertEqual(error.exception.status, 403)
        self.assertFalse(self.record()["completed"])

    def test_later_unverified_reply_keeps_earned_flag_and_zero_point_replay(self):
        first = self.complete()
        result = self.ask()
        self.assert_retained(result, first["lab"]["flag"])
        self.assertFalse(result["lab"]["exploited"])
        repeated = self.app.submit_flag(self.sid, 9, {"flag": first["lab"]["flag"]})
        self.assertEqual(repeated["flag_submission"]["points_awarded"], 0)

    def test_verification_commit_preserves_a_concurrently_earned_flag_origin(self):
        flag = self.unlock()["lab"]["flag"]
        awards = []
        result = self.ask(during=lambda: awards.append(server.Application(self.directory).submit_flag(self.sid, 9, {"flag": flag})))
        self.assertEqual(awards[0]["flag_submission"]["points_awarded"], 500)
        self.assert_retained(result, flag)
        self.assertFalse(result["lab"]["exploited"])
        self.assertEqual(self.record()["lab9_flag_attempt"], 1)

    def test_restart_requires_new_refusals_and_disclosure_to_earn_a_new_flag(self):
        first = self.complete()
        self.app.reset(self.sid, 9)
        result = self.unlock()
        self.assertTrue(result["lab"]["exploited"])
        self.assertFalse(result["lab"]["completed"])
        self.assertEqual(result["summary"]["score"], 0)
        self.assertEqual(result["lab"]["guardrail_status"], "disclosed")
        self.assertNotEqual(result["lab"]["flag"], first["lab"]["flag"])
        self.assertEqual(self.record()["lab9_verification_receipts"][-1]["attempt"], 2)
        with self.assertRaises(server.APIError) as error:
            self.app.submit_flag(self.sid, 9, {"flag": first["lab"]["flag"]})
        self.assertEqual(error.exception.status, 400)
        earned = self.app.submit_flag(self.sid, 9, {"flag": result["lab"]["flag"]})
        self.assert_retained(earned, result["lab"]["flag"])
        self.assertEqual(self.record()["lab9_flag_attempt"], 2)
        self.assertEqual(earned["flag_submission"]["points_awarded"], 500)
        self.assertEqual(self.app.submit_flag(self.sid, 9, {"flag": result["lab"]["flag"]})["flag_submission"]["points_awarded"], 0)

    def test_completion_preserves_existing_receipts_flag_key_and_database_schema(self):
        flag = self.unlock()["lab"]["flag"]
        receipts = copy.deepcopy(self.record()["lab9_verification_receipts"])
        with self.app.store.connect() as db:
            schema = db.execute("SELECT name,sql FROM sqlite_schema ORDER BY name").fetchall()
            key = db.execute("SELECT value FROM metadata WHERE name='flag_key'").fetchone()
        result = self.app.submit_flag(self.sid, 9, {"flag": flag})
        self.app.submit_flag(self.sid, 9, {"flag": flag})
        self.assertEqual(self.record()["lab9_verification_receipts"], receipts)
        for private in ("lab9_flag_attempt", "lab9_verification_receipts", "prior_refusal_events"):
            self.assertNotIn(private, json.dumps(result))
        with self.app.store.connect() as db:
            self.assertEqual(db.execute("SELECT name,sql FROM sqlite_schema ORDER BY name").fetchall(), schema)
            self.assertEqual(db.execute("SELECT value FROM metadata WHERE name='flag_key'").fetchone(), key)

    def test_legacy_completed_records_work_without_migration_or_receipt_rewriting(self):
        _, data = self.app.store.get(self.sid)
        data["labs"]["9"].update(attempt=7, completed=True, exploited=False)
        with self.app.store.connect() as db:
            db.execute("UPDATE sessions SET data=? WHERE sid=?", (json.dumps(data), self.sid))
        expected = self.app.store.flag(self.sid, 9, 7)
        reopened = server.Application(self.directory)
        self.assert_retained(self.public(reopened), expected)
        self.assertFalse(self.public(reopened)["lab"]["exploited"])
        self.assertEqual(reopened.submit_flag(self.sid, 9, {"flag": expected})["flag_submission"]["points_awarded"], 0)

    def test_existing_prefix_and_whitespace_still_use_the_same_flag_digest(self):
        flag = self.unlock()["lab"]["flag"]
        legacy = "AIGOAT{" + flag[len("PANOPTIS{"):]
        result = self.app.submit_flag(self.sid, 9, {"flag": " \n" + legacy + " \n"})
        self.assert_retained(result, flag)
        self.assertEqual(result["flag_submission"]["points_awarded"], 500)

    def test_lab9_mutations_preserve_all_other_labs(self):
        _, data = self.app.store.get(self.sid)
        for lab_id in data["labs"]:
            if lab_id != "9":
                data["labs"][lab_id].update(completed=True, exploited=True)
        self.app.store.save(self.sid, data)
        before = {key: copy.deepcopy(value) for key, value in data["labs"].items() if key != "9"}
        first = self.complete()
        self.assertEqual(first["summary"]["score"], 2100)
        self.app.reset(self.sid, 9)
        self.app.update_defense(self.sid, 9, {"level": 1})
        after = self.app.store.get(self.sid)[1]
        self.assertEqual({key: value for key, value in after["labs"].items() if key != "9"}, before)
        self.assertEqual(self.app.summary(after)["score"], 1600)


if __name__ == "__main__":
    unittest.main()
