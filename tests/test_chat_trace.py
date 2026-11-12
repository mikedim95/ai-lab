"""Private pipeline telemetry, using disposable SQLite and mocked HTTP only."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import catalog
import server
from test_lab9_evidence import DISCLOSURE, NORMAL, QUESTIONS, REFUSALS
from test_app import LAB6_IDENTITY_REQUEST


class ChatTraceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.app = server.Application(self.directory)
        self.sid, _ = self.app.store.get(None)

    def ask(self, lab=9, message="Explain public maintenance procedures.", reply=NORMAL, **body):
        def transport(request, timeout):
            self.assertEqual(request.full_url, server.OLLAMA_URL + "/api/chat")
            if isinstance(reply, Exception):
                raise reply
            return io.BytesIO(json.dumps({"message": {"content": reply}}).encode())
        with patch("server.urllib.request.urlopen", side_effect=transport):
            return self.app.chat(self.sid, lab, {"message": message, **body})

    def traces(self):
        with self.app.store.connect() as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM chat_traces ORDER BY created_at,trace_id")]

    def events(self, trace_id=None):
        trace_id = trace_id or self.traces()[-1]["trace_id"]
        with self.app.store.connect() as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(
                "SELECT * FROM chat_trace_events WHERE trace_id=? ORDER BY sequence", (trace_id,))]

    def terminal(self, stage, trace_id=None):
        return [event for event in self.events(trace_id) if event["stage"] == stage
                and event["status"] not in {"waiting", "running"}]

    def native(self, calls, *, level=0, final=NORMAL):
        self.app.update_defense(self.sid, 1, {"level": level})
        replies = [{"message": {"content": "", "tool_calls": [call]}} for call in calls]
        replies.append(final if isinstance(final, Exception) else {"message": {"content": final}})
        def transport(request, timeout):
            response = replies.pop(0)
            if isinstance(response, Exception):
                raise response
            return io.BytesIO(json.dumps(response).encode())
        with patch("server.urllib.request.urlopen", side_effect=transport):
            return self.app.chat(self.sid, 1, {"message": "Describe the public monitoring readings."})

    @staticmethod
    def write(arguments=None):
        return {"function": {"name": "set_boiler_inputs", "arguments": arguments if arguments is not None
                             else {"water_in_pct": 48, "fuel_in_pct": 50}}}

    def test_actual_stages_are_ordered_and_linked_to_the_existing_turn(self):
        result = self.ask()
        trace, = self.traces()
        self.assertRegex(trace["trace_id"], r"^[a-f0-9]{32}$")
        self.assertEqual(trace["student_id"], hashlib.sha256(self.sid.encode()).hexdigest())
        self.assertEqual((trace["lab_id"], trace["difficulty"], trace["attempt"]), (9, 0, 1))
        receipt = self.app.store.get(self.sid)[1]["labs"]["9"]["lab9_verification_receipts"][-1]
        self.assertEqual(trace["turn_id"], receipt["turn_id"])
        self.assertIsNotNone(trace["finished_at"])
        seen = [event["stage"] for event in self.events() if event["status"] == "running"]
        self.assertLess(seen.index("input"), seen.index("policy"))
        self.assertLess(seen.index("policy"), seen.index("context"))
        self.assertLess(seen.index("context"), seen.index("ollama"))
        self.assertLess(seen.index("ollama"), seen.index("output_protection"))
        self.assertLess(seen.index("output_protection"), seen.index("exploit_verification"))
        self.assertEqual(self.terminal("exploit_verification")[-1]["reason_code"], "condition_not_met")
        self.assertEqual(result["summary"]["score"], 0)
        self.assertIsNone(server._ACTIVE_TRACE.get())

    def test_span_pairs_have_timestamps_monotonic_durations_and_supported_statuses(self):
        self.ask()
        spans = {}
        for event in self.events():
            self.assertIn(event["stage"], server.TRACE_EVENT_STAGES)
            self.assertIn(event["status"], server.TRACE_STATUSES)
            self.assertIn(event["reason_code"], server.TRACE_REASONS)
            self.assertGreater(event["timestamp"], 0)
            self.assertGreaterEqual(event["duration_ms"], 0)
            spans.setdefault(event["span_id"], []).append(event)
        for events in spans.values():
            self.assertIn(events[-1]["status"], {"passed", "blocked", "skipped", "error"})
            self.assertEqual(len({event["stage"] for event in events}), 1)
            self.assertEqual([event["duration_ms"] for event in events], sorted(event["duration_ms"] for event in events))
        self.assertIn("waiting", {event["status"] for event in self.events()})

    def test_all_nine_labs_get_unique_bound_traces(self):
        for lab in range(1, 10):
            self.ask(lab=lab)
        traces = self.traces()
        self.assertEqual([trace["lab_id"] for trace in traces], list(range(1, 10)))
        self.assertEqual(len({trace["trace_id"] for trace in traces}), 9)
        self.assertEqual(len({trace["turn_id"] for trace in traces}), 9)
        self.assertTrue(all(trace["student_id"] and trace["attempt"] == 1 for trace in traces))

    def test_existing_difficulty_rule_records_a_real_backend_input_block(self):
        self.app.update_defense(self.sid, 6, {"level": 1})
        result = self.ask(lab=6, message=LAB6_IDENTITY_REQUEST)
        self.assertTrue(result["lab"]["last_result"]["defense_blocked"])
        self.assertEqual(self.terminal("policy")[-1]["status"], "blocked")
        self.assertEqual(self.terminal("ollama")[-1]["status"], "skipped")

    def test_disabled_rag_and_unused_tools_are_skipped_without_fake_completions(self):
        self.ask(use_kb=True)  # Lab 9 has no RAG component.
        for stage in ("rag", "tool_authorization", "tool_execution"):
            self.assertEqual([event["status"] for event in self.terminal(stage)], ["skipped"])
            self.assertFalse(any(event["stage"] == stage and event["status"] == "running" for event in self.events()))

    def test_context_retrieval_records_only_real_retrieval_and_screening(self):
        self.ask(lab=3, message="What boiler inspection maintenance is scheduled?", use_kb=True)
        self.assertEqual(self.terminal("rag")[-1]["reason_code"], "rag_completed")
        self.app.update_kb(self.sid, 3, {"documents": [{"id": "trace-source", "title": "Boiler inspection",
            "content": "Boiler inspection maintenance is scheduled for the next review."}], "sync": True, "use_kb": True})
        self.app.update_defense(self.sid, 3, {"level": 2})
        self.ask(lab=3, message="What boiler inspection maintenance is scheduled?", use_kb=True)
        self.assertEqual(self.terminal("rag")[-1]["status"], "blocked")
        self.assertEqual(self.terminal("rag")[-1]["reason_code"], "rag_sources_rejected")
        self.assertEqual(self.terminal("ollama")[-1]["status"], "passed")

    def test_input_policy_rejection_is_blocked_and_inference_is_skipped(self):
        with patch("server.defend_input", return_value=("", "secret-validator-details")):
            self.ask()
        self.assertEqual(self.terminal("policy")[-1]["status"], "blocked")
        for stage in ("context", "ollama", "output_protection"):
            self.assertEqual([event["status"] for event in self.terminal(stage)], ["skipped"])
        self.assertEqual(self.terminal("exploit_verification")[-1]["status"], "passed")

    def test_output_policy_rejection_is_distinct_from_model_generation(self):
        with patch("server.defend_output", return_value=(catalog.LAB9_REFUSAL, "secret-validator-details")):
            self.ask()
        self.assertEqual(self.terminal("ollama")[-1]["status"], "passed")
        self.assertEqual(self.terminal("output_protection")[-1]["status"], "blocked")
        self.assertEqual(self.terminal("exploit_verification")[-1]["reason_code"], "condition_not_met")

    def test_short_circuited_lab7_evaluator_is_skipped_after_output_rejection(self):
        with patch("server.defend_output", return_value=(NORMAL, "protected_output")), \
                patch("server.check_exploit", side_effect=AssertionError("Evaluator must not run")):
            self.ask(lab=7)
        self.assertEqual(self.terminal("output_protection")[-1]["status"], "blocked")
        self.assertEqual(self.terminal("exploit_verification")[-1]["status"], "skipped")
        self.assertEqual(self.terminal("exploit_verification")[-1]["reason_code"], "protection_rejected")

    def test_genuine_model_refusals_and_disclosure_preserve_verification_and_manual_scoring(self):
        for message, reply in zip(QUESTIONS, (*REFUSALS, DISCLOSURE)):
            result = self.ask(message=message, reply=reply)
        reasons = [self.terminal("exploit_verification", trace["trace_id"])[-1]["reason_code"] for trace in self.traces()]
        self.assertEqual(reasons, ["model_refusal", "model_refusal", "verified"])
        for trace in self.traces()[:2]:
            self.assertFalse(any(event["status"] == "blocked" for event in self.events(trace["trace_id"])))
        self.assertTrue(result["lab"]["exploited"])
        self.assertEqual(result["summary"]["score"], 0)
        submitted = self.app.submit_flag(self.sid, 9, {"flag": result["lab"]["flag"]})
        self.assertEqual(submitted["summary"]["score"], 500)
        self.assertEqual(len(self.traces()), 3)

    def test_unmet_disclosure_prerequisite_is_not_a_backend_block(self):
        result = self.ask(message=QUESTIONS[-1], reply=DISCLOSURE)
        self.assertIsNone(result["lab"]["flag"])
        self.assertEqual(self.terminal("exploit_verification")[-1]["reason_code"], "condition_not_met")
        self.assertFalse(any(event["status"] == "blocked" for event in self.events()))

    def test_non_production_callback_does_not_claim_ollama_execution(self):
        self.app.model = lambda messages: NORMAL
        self.app.chat(self.sid, 9, {"message": "Public handover checklist?"})
        self.assertEqual([event["status"] for event in self.terminal("ollama")], ["skipped"])
        self.assertEqual(self.terminal("ollama")[-1]["reason_code"], "non_production_model")

    def test_ollama_transport_error_does_not_invent_output_or_verification_success(self):
        with self.assertRaises(server.APIError):
            self.ask(reply=OSError("credential-that-must-stay-private"))
        self.assertEqual(self.terminal("ollama")[-1]["status"], "error")
        for stage in ("output_protection", "exploit_verification"):
            self.assertEqual(self.terminal(stage)[-1]["status"], "skipped")

    def test_empty_ollama_reply_is_error_rather_than_passed(self):
        with self.assertRaises(server.APIError):
            self.ask(reply="")
        self.assertEqual(self.terminal("ollama")[-1]["status"], "error")

    def test_native_tools_have_actual_authorization_execution_and_per_request_ollama_spans(self):
        result = self.native([self.write(), {"function": {"name": "get_boiler_status", "arguments": {}}}])
        self.assertEqual([event["reason_code"] for event in self.terminal("ollama")],
                         ["model_tool_request", "model_tool_request", "model_response"])
        self.assertEqual([event["status"] for event in self.terminal("tool_execution")], ["passed", "passed"])
        self.assertIn("authorization_bypassed", [event["reason_code"] for event in self.terminal("tool_authorization")])
        self.assertEqual(self.traces()[-1]["turn_id"], result["lab"]["boiler_tool_calls"][-1]["turn_id"])
        self.assertEqual(result["summary"]["score"], 0)

    def test_real_tool_denial_is_blocked_and_execution_is_skipped(self):
        result = self.native([self.write()], level=1)
        self.assertFalse(result["lab"]["exploited"])
        self.assertEqual(self.terminal("tool_authorization")[-1]["status"], "blocked")
        self.assertEqual(self.terminal("tool_execution")[-1]["status"], "skipped")

    def test_invalid_tool_arguments_are_blocked_without_execution(self):
        self.native([self.write({"water_in_pct": "not-an-integer", "fuel_in_pct": 50})])
        self.assertEqual(self.terminal("tool_authorization")[-1]["status"], "blocked")
        self.assertEqual(self.terminal("tool_execution")[-1]["status"], "skipped")

    def test_unknown_native_tool_is_a_backend_rejection(self):
        with self.assertRaises(server.APIError):
            self.native([{"function": {"name": "private-tool-name", "arguments": {}}}])
        self.assertEqual(self.terminal("tool_authorization")[-1]["status"], "blocked")
        self.assertFalse(any(event["stage"] == "tool_execution" and event["status"] == "passed" for event in self.events()))

    def test_failed_followup_generation_keeps_the_actual_tool_success(self):
        result = self.native([self.write()], final=OSError("private-transport-details"))
        self.assertTrue(result["lab"]["exploited"])
        self.assertEqual([event["status"] for event in self.terminal("ollama")], ["passed", "error"])
        self.assertEqual(self.terminal("exploit_verification")[-1]["reason_code"], "verified")

    def test_invalid_prompt_is_blocked_before_model_execution(self):
        for body in ({"message": ""}, {"message": "a" * 4001}, {"message": "Hi", "use_kb": "yes"}):
            with self.subTest(body_kind=len(body)), self.assertRaises(server.APIError):
                self.app.chat(self.sid, 9, body)
            self.assertEqual(self.terminal("input")[-1]["status"], "blocked")
            self.assertEqual(self.terminal("ollama")[-1]["status"], "skipped")
        self.assertEqual(len({trace["trace_id"] for trace in self.traces()}), 3)

    def test_capacity_rejection_is_blocked_without_an_ollama_completion(self):
        self.app.generation.acquire()
        try:
            with self.assertRaises(server.APIError) as failure:
                self.ask()
        finally:
            self.app.generation.release()
        self.assertEqual(failure.exception.status, 429)
        self.assertEqual(self.terminal("session")[-1]["reason_code"], "generation_busy")
        self.assertEqual(self.terminal("session")[-1]["status"], "blocked")
        self.assertEqual(self.terminal("ollama")[-1]["status"], "skipped")

    def test_events_are_persisted_while_processing_is_running_and_students_are_isolated(self):
        started, release = threading.Event(), threading.Event()
        other, _ = self.app.store.get(None)
        def transport(request, timeout):
            started.set()
            self.assertTrue(release.wait(5))
            return io.BytesIO(json.dumps({"message": {"content": NORMAL}}).encode())
        with ThreadPoolExecutor(max_workers=1) as pool, patch("server.urllib.request.urlopen", side_effect=transport):
            future = pool.submit(self.app.chat, self.sid, 9, {"message": "Public maintenance?"})
            try:
                self.assertTrue(started.wait(5))
                first, = self.traces()
                self.assertIsNone(first["finished_at"])
                self.assertEqual(self.events(first["trace_id"])[-1]["status"], "running")
                self.assertEqual(self.events(first["trace_id"])[-1]["stage"], "ollama")
                self.assertEqual(self.terminal("ollama", first["trace_id"]), [])
                with self.assertRaises(server.APIError):
                    self.app.chat(other, 5, {"message": "Public maintenance?"})
            finally:
                release.set()
            future.result(timeout=5)
        self.assertEqual(len(self.traces()), 2)
        self.assertEqual(len({trace["student_id"] for trace in self.traces()}), 2)
        self.assertEqual(len({trace["trace_id"] for trace in self.traces()}), 2)
        self.assertEqual(len({trace["turn_id"] for trace in self.traces()}), 2)

    def test_reset_difficulty_and_restart_retain_original_trace_binding(self):
        self.ask()
        original = self.traces()[0]
        self.app.reset(self.sid, 9)
        self.app.update_defense(self.sid, 9, {"level": 1})
        self.app = server.Application(self.directory)
        self.assertEqual(self.traces()[0], original)
        self.ask()
        self.assertEqual((self.traces()[-1]["difficulty"], self.traces()[-1]["attempt"]), (1, 3))

    def test_stale_attempt_discard_is_blocked_without_rebinding_to_the_new_attempt(self):
        def transport(request, timeout):
            self.app.reset(self.sid, 9)
            return io.BytesIO(json.dumps({"message": {"content": NORMAL}}).encode())
        with patch("server.urllib.request.urlopen", side_effect=transport), self.assertRaises(server.APIError) as failure:
            self.app.chat(self.sid, 9, {"message": "Public maintenance?"})
        self.assertEqual(failure.exception.status, 409)
        self.assertEqual(self.traces()[-1]["attempt"], 1)
        self.assertEqual(self.terminal("session")[-1]["status"], "blocked")
        self.assertEqual(self.terminal("session")[-1]["reason_code"], "stale_attempt")

    def test_trace_writes_failing_do_not_change_prompt_processing(self):
        with patch.object(self.app.store, "append_trace_event", side_effect=sqlite3.OperationalError("private failure")):
            with self.assertLogs(level="WARNING") as logs:
                result = self.ask()
        self.assertEqual(result["lab"]["messages"][-1]["content"], NORMAL)
        self.assertNotIn("private failure", str(logs.output))
        self.assertIsNone(server._ACTIVE_TRACE.get())
        self.assertTrue(self.app.generation.acquire(blocking=False))
        self.app.generation.release()

    def test_trace_binding_and_finalization_failures_do_not_break_chat(self):
        for method in ("bind", "close"):
            with self.subTest(method=method), patch.object(server.ChatTrace, method, side_effect=RuntimeError("private failure")):
                result = self.ask()
                self.assertEqual(result["lab"]["messages"][-1]["content"], NORMAL)
                self.assertIsNone(server._ACTIVE_TRACE.get())

    def test_trace_schema_failure_does_not_prevent_startup_or_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(server.Store, "initialize_trace_schema", side_effect=sqlite3.OperationalError("unavailable")):
                self.app = server.Application(Path(directory))
            self.sid, _ = self.app.store.get(None)
            self.assertEqual(self.ask()["lab"]["messages"][-1]["content"], NORMAL)

    def test_busy_trace_writer_has_a_short_timeout_and_does_not_break_chat(self):
        trace = server.ChatTrace(self.app.store, 9)
        token = server._ACTIVE_TRACE.set(trace)
        try:
            with self.app.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                started = time.monotonic()
                server._trace_call("begin", "input")
                self.assertLess(time.monotonic() - started, .5)
                self.assertTrue(trace.disabled)
            self.assertEqual(self.ask()["lab"]["messages"][-1]["content"], NORMAL)
        finally:
            server._ACTIVE_TRACE.reset(token)

    def test_no_sensitive_content_or_freeform_reason_is_persisted_or_projected(self):
        marker = "private-credential-marker"
        result = self.ask(message=QUESTIONS[-1] + " " + marker, reply=DISCLOSURE)
        with self.app.store.connect() as db:
            serialized = json.dumps([db.execute("SELECT * FROM chat_traces").fetchall(),
                                     db.execute("SELECT * FROM chat_trace_events").fetchall()])
        for secret in [marker, self.sid, catalog.LAB9_PROMPT, self.app.store.flag(self.sid, 9, 1),
                       *[field["value"] for field in catalog.LAB9_PROTECTED_FIELDS]]:
            self.assertNotIn(secret, serialized)
        self.assertNotIn("trace_id", json.dumps(result))
        self.assertNotIn("chat_trace", json.dumps(self.app.state(self.sid, self.app.store.get(self.sid)[1])))
        trace = server.ChatTrace(self.app.store, 9)
        token = server._ACTIVE_TRACE.set(trace)
        try:
            span = server._trace_call("begin", "input", reason=marker)
            server._trace_call("end", span, "passed", marker)
            server._trace_call("close")
        finally:
            server._ACTIVE_TRACE.reset(token)
        self.assertNotIn(marker, json.dumps(self.events(trace.trace_id)))

    def http(self, raw, *, headers=None):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.app))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection(*httpd.server_address)
            request_headers = {"Content-Type": "application/json", "Cookie": "nine_sid=" + self.sid}
            request_headers.update(headers or {})
            conn.request("POST", "/api/labs/9/chat", raw, request_headers)
            response = conn.getresponse()
            status, data = response.status, json.loads(response.read())
            conn.close()
            return status, data
        finally:
            httpd.shutdown(); httpd.server_close(); thread.join()

    def test_http_rejections_are_traced_and_bound_without_creating_sessions(self):
        self.assertEqual(self.http(b"not-json")[0], 400)
        self.assertEqual(self.http(b"{}", headers={"Origin": "https://other.invalid"})[0], 403)
        for trace in self.traces():
            self.assertEqual(trace["student_id"], hashlib.sha256(self.sid.encode()).hexdigest())
            self.assertEqual(self.terminal("input", trace["trace_id"])[-1]["status"], "blocked")
            self.assertEqual(self.terminal("ollama", trace["trace_id"])[-1]["status"], "skipped")
        with self.app.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)

    def test_http_and_application_share_one_trace_and_keep_responses_unchanged(self):
        with patch("server.urllib.request.urlopen", return_value=io.BytesIO(json.dumps({"message": {"content": NORMAL}}).encode())):
            status, result = self.http(json.dumps({"message": "Public maintenance?"}).encode())
        self.assertEqual(status, 200)
        self.assertEqual(len(self.traces()), 1)
        self.assertEqual(result["lab"]["messages"][-1]["content"], NORMAL)
        self.assertNotIn("trace_id", result)


if __name__ == "__main__":
    unittest.main()
