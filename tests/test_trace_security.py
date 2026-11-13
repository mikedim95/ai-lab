"""Trace security with existing fixtures, disposable identities and mocked HTTP."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch

import catalog
import server
import test_trace_projection as projection_cases
from test_app import LAB6_IDENTITY_REQUEST, LAB6_ADOPTED_REPLY
from test_lab9_evidence import QUESTIONS, REFUSALS

REPLY = "Public monitoring procedures remain available."


class TraceSecurityTests(unittest.TestCase):
    # Reuse the existing isolated HTTP/SSE fixture, without rerunning its tests.
    setUp = projection_cases.TraceProjectionTests.setUp
    tearDown = projection_cases.TraceProjectionTests.tearDown
    request = projection_cases.TraceProjectionTests.request
    reserve = projection_cases.TraceProjectionTests.reserve

    def test_overlapping_students_keep_distinct_live_and_replayed_events(self):
        other_app = server.Application(self.directory)
        release = threading.Event()
        started = {9: threading.Event(), 6: threading.Event()}

        def transport(request, timeout):
            payload = json.loads(request.data)
            lab = 9 if payload["messages"][-1]["content"] == QUESTIONS[0] else 6
            started[lab].set()
            self.assertTrue(release.wait(5))
            reply = REFUSALS[0] if lab == 9 else LAB6_ADOPTED_REPLY
            return io.BytesIO(json.dumps({"message": {"content": reply}}).encode())

        with ThreadPoolExecutor(max_workers=2) as pool, patch("server.urllib.request.urlopen", side_effect=transport):
            first = pool.submit(self.app.chat, self.sid, 9, {"message": QUESTIONS[0]})
            second = pool.submit(other_app.chat, self.other, 6, {"message": LAB6_IDENTITY_REQUEST})
            try:
                self.assertTrue(started[9].wait(3)); self.assertTrue(started[6].wait(3))
                with self.app.store.connect() as db:
                    rows = db.execute("SELECT trace_id,student_id,lab_id FROM chat_traces").fetchall()
                self.assertEqual(len(rows), 2)
                ids = {}
                for trace_id, student_id, lab in rows:
                    owner = self.sid if lab == 9 else self.other
                    self.assertEqual(student_id, hashlib.sha256(owner.encode()).hexdigest())
                    _, events = self.app.store.read_trace(owner, trace_id)
                    self.assertTrue(all(event["metadata"] == {"lab_id": lab, "difficulty": 0, "attempt": 1} for event in events))
                    self.assertTrue(any(event["stage"] == "ollama" and event["status"] == "running" for event in events))
                    denied = self.request(f"/api/traces/{trace_id}/events", sid=self.other if lab == 9 else self.sid)
                    self.assertEqual(denied[0], 404)
                    ids[lab] = trace_id
            finally:
                release.set()
            self.assertFalse(first.result(timeout=5)["lab"]["last_result"]["triggered"])
            self.assertTrue(second.result(timeout=5)["lab"]["last_result"]["triggered"])
        for lab, expected in ((9, "MODEL_REFUSED"), (6, "EXPLOIT_VERIFIED")):
            owner = self.sid if lab == 9 else self.other
            status, events = self.request(f"/api/traces/{ids[lab]}/events", sid=owner)
            self.assertEqual(status, 200)
            self.assertTrue(all(event["trace_id"] == ids[lab] and event["metadata"]["lab_id"] == lab for event in events))
            self.assertEqual([event["event_id"] for event in events], list(range(1, len(events)+1)))
            self.assertEqual(events[-1]["outcome"], expected)

    def test_trace_write_and_cleanup_failures_preserve_native_success_and_scoring(self):
        for method in ("append_trace_event", "finish_trace", "_prune_traces"):
            with self.subTest(failed_method=method):
                sid, _ = self.app.store.get(None)
                replies = iter([
                    {"message": {"content": "", "tool_calls": [{"function": {"name": "set_boiler_inputs",
                        "arguments": {"water_in_pct": 48, "fuel_in_pct": 50}}}]}},
                    {"message": {"content": REPLY}},
                ])

                def transport(request, timeout):
                    return io.BytesIO(json.dumps(next(replies)).encode())

                with patch.object(self.app.store, method, side_effect=sqlite3.OperationalError("private-failure-canary")):
                    with patch("server.urllib.request.urlopen", side_effect=transport), self.assertLogs(level="WARNING") as logs:
                        result = self.app.chat(sid, 1, {"message": "Describe the public monitoring readings."})
                self.assertNotIn("private-failure-canary", str(logs.output))
                self.assertEqual(result["lab"]["messages"][-1]["content"], REPLY)
                self.assertTrue(result["lab"]["last_result"]["triggered"])
                self.assertEqual(result["summary"]["score"], 0)
                flag = result["lab"]["flag"]
                awarded = self.app.submit_flag(sid, 1, {"flag": flag})
                self.assertEqual(awarded["summary"]["score"], 100)
                self.assertEqual(self.app.submit_flag(sid, 1, {"flag": flag})["summary"]["score"], 100)
                self.assertIsNone(server._ACTIVE_TRACE.get())

    def test_sensitive_native_arguments_never_enter_student_sse(self):
        marker = "private-native-argument-canary"
        reserved = self.reserve(lab=1)
        replies = iter([
            {"message": {"content": "", "tool_calls": [{"function": {"name": "set_boiler_inputs",
                "arguments": {"water_in_pct": marker, "fuel_in_pct": 50}}}]}},
            {"message": {"content": REPLY}},
        ])

        def transport(request, timeout):
            return io.BytesIO(json.dumps(next(replies)).encode())

        with patch("server.urllib.request.urlopen", side_effect=transport):
            status, _ = self.request("/api/labs/1/chat", {"message": "Describe the public monitoring readings."},
                                     headers={"X-Trace-Id": reserved["trace_id"]})
        self.assertEqual(status, 200)
        private = self.app.store.get(self.sid)[1]["labs"]["1"]
        self.assertEqual(private["boiler_tool_calls"][-1]["arguments"]["water_in_pct"], marker)
        status, events = self.request(reserved["events_url"])
        self.assertEqual(status, 200)
        self.assertEqual(events[-1]["outcome"], "BACKEND_BLOCKED")
        public = json.dumps(events)
        for value in (marker, self.sid, catalog.PROMPTS[1], REPLY, "water_in_pct", "fuel_in_pct",
                      self.app.store.flag(self.sid, 1, 1), *(field["value"] for field in catalog.LAB9_PROTECTED_FIELDS)):
            self.assertNotIn(value, public)


if __name__ == "__main__":
    unittest.main()
