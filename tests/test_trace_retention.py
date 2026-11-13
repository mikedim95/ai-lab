"""Trace-only retention regressions; all state lives in disposable stores."""
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import server

REPLY = "Public monitoring procedures remain available."


class TraceRetentionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.app = server.Application(Path(directory.name), model=lambda messages: REPLY)
        self.sid, _ = self.app.store.get(None)

    def limits(self, **values):
        stack = ExitStack()
        self.addCleanup(stack.close)
        for name, value in values.items():
            stack.enter_context(patch.object(server, name, value, create=True))

    def ask(self, sid=None):
        result = self.app.chat(sid or self.sid, 7, {"message": "Explain public monitoring procedures."})
        self.assertEqual(result["lab"]["messages"][-1]["content"], REPLY)
        self.assertEqual(result["summary"]["score"], 0)
        return result

    def rows(self, sid=None):
        with self.app.store.connect() as db:
            query = "SELECT trace_id FROM chat_traces"
            args = ()
            if sid:
                query += " WHERE student_id=?"
                args = (hashlib.sha256(sid.encode()).hexdigest(),)
            return {row[0] for row in db.execute(query, args)}

    def assert_no_orphans(self):
        with self.app.store.connect() as db:
            self.assertEqual(db.execute("""SELECT COUNT(*) FROM chat_trace_events e
                WHERE NOT EXISTS (SELECT 1 FROM chat_traces t WHERE t.trace_id=e.trace_id)""").fetchone()[0], 0)

    def test_ordinary_chats_without_reservations_keep_only_200_finished_traces(self):
        for _ in range(210):
            self.ask()
        self.assertEqual(len(self.rows()), 200)
        self.assert_no_orphans()

    def test_retention_preserves_other_students_and_the_latest_trace(self):
        self.limits(TRACE_RETAIN_FINISHED=3)
        other, _ = self.app.store.get(None)
        self.ask(other)
        saved = self.rows(other)
        for _ in range(7):
            self.ask()
        self.assertEqual(len(self.rows(self.sid)), 3)
        self.assertEqual(self.rows(other), saved)
        with self.app.store.connect() as db:
            row = db.execute("SELECT trace_id FROM chat_traces WHERE student_id=? ORDER BY finished_at DESC LIMIT 1",
                             (hashlib.sha256(self.sid.encode()).hexdigest(),)).fetchone()
        self.assertEqual(self.app.store.read_trace(self.sid, row[0])[1][-1]["outcome"], "NO_EXPLOIT")
        self.assert_no_orphans()

    def test_unbound_rejected_requests_also_have_bounded_retention(self):
        self.limits(TRACE_RETAIN_FINISHED=3)
        for _ in range(7):
            trace = server.ChatTrace(self.app.store, 7)
            span = trace.begin("input")
            trace.end(span, "blocked", "invalid_input")
            trace.close(server.APIError(400, "Invalid isolated request"))
        with self.app.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM chat_traces WHERE student_id IS NULL").fetchone()[0], 3)
        self.assert_no_orphans()

    def test_expired_failed_traces_and_reservations_are_removed_with_events(self):
        expired = self.app.store.reserve_trace(self.sid, 7)["trace_id"]
        with patch.object(self.app.store, "finish_trace", side_effect=sqlite3.OperationalError("isolated failure")):
            with self.assertLogs(level="WARNING"):
                self.ask()
        failed, = self.rows() - {expired}
        with self.app.store.connect() as db:
            db.execute("UPDATE chat_traces SET created_at=? WHERE trace_id=?", (time.time()-40, expired))
            db.execute("UPDATE chat_traces SET created_at=?,started_at=? WHERE trace_id=?",
                       (time.time()-1000, time.time()-1000, failed))
        # A new ordinary chat must run maintenance even without a reservation.
        self.ask()
        self.assertNotIn(expired, self.rows())
        self.assertNotIn(failed, self.rows())
        self.assert_no_orphans()

    def test_a_global_budget_bounds_traces_across_fresh_identities(self):
        self.limits(TRACE_MAX_STORED=5)
        for _ in range(9):
            sid, _ = self.app.store.get(None)
            self.ask(sid)
        self.assertEqual(len(self.rows()), 5)
        self.assert_no_orphans()

    def test_full_budget_preserves_live_traces_and_does_not_reject_chat(self):
        self.limits(TRACE_MAX_STORED=2)
        first = self.app.store.reserve_trace(self.sid, 7)["trace_id"]
        other, _ = self.app.store.get(None)
        second = self.app.store.reserve_trace(other, 7)["trace_id"]
        with self.assertRaises(server.APIError) as rejected:
            self.app.store.reserve_trace(self.sid, 7)
        self.assertEqual(rejected.exception.status, 429)
        with self.assertLogs(level="WARNING"):
            self.ask()
        self.assertEqual(self.rows(), {first, second})
        self.assertEqual(self.app.store.read_trace(self.sid, first)[1], [])
        self.assertEqual(self.app.store.read_trace(other, second)[1], [])

    def test_concurrent_reservations_cannot_overfill_the_global_budget(self):
        self.limits(TRACE_MAX_STORED=2)
        students = [self.app.store.get(None)[0] for _ in range(8)]
        start = threading.Barrier(len(students))

        def reserve(sid):
            start.wait(timeout=5)
            try:
                return self.app.store.reserve_trace(sid, 7)["trace_id"]
            except server.APIError as exc:
                self.assertEqual(exc.status, 429)
            except sqlite3.OperationalError:
                # Trace-only lock admission may fail after its short timeout.
                pass
            return None

        with ThreadPoolExecutor(max_workers=len(students)) as pool:
            accepted = {trace_id for trace_id in pool.map(reserve, students) if trace_id}
        self.assertTrue(accepted)
        self.assertLessEqual(len(accepted), 2)
        self.assertEqual(self.rows(), accepted)
        self.assert_no_orphans()

    def test_legacy_backlog_cleanup_commits_even_when_no_new_trace_fits(self):
        for _ in range(15):
            self.ask()
        self.limits(TRACE_MAX_STORED=5, TRACE_PRUNE_BATCH=2)
        with self.assertLogs(level="WARNING"):
            self.ask()
        self.assertLess(len(self.rows()), 15)
        for _ in range(8):
            self.ask()
        self.assertLessEqual(len(self.rows()), 5)
        self.assert_no_orphans()


if __name__ == "__main__":
    unittest.main()
