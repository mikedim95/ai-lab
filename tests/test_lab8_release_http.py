"""Real request handling and concurrent awards on disposable Lab 8 sessions."""
from concurrent.futures import ThreadPoolExecutor
import http.client
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import server
from lab8_verification_fixtures import ADOPTED, EXISTING_INSTRUCTION, REFERENCE, SUMMARY


class Lab8ReleaseHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.app = server.Application(self.directory)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.app))
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.sid = self.new_student()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def request(self, action=None, body=None, *, sid=None):
        connection = http.client.HTTPConnection(*self.http.server_address)
        headers = {"Content-Type": "application/json"}
        if sid:
            headers["Cookie"] = "nine_sid=" + sid
        connection.request("POST" if action else "GET", "/api/labs/8/" + action if action else "/api/state",
                           json.dumps(body or {}) if action else None, headers)
        response = connection.getresponse()
        cookie = response.getheader("Set-Cookie")
        status, value = response.status, json.loads(response.read())
        connection.close()
        return status, value, cookie

    def new_student(self):
        status, _, cookie = self.request()
        self.assertEqual(status, 200)
        return cookie.split(";", 1)[0].split("=", 1)[1]

    def capture(self, sid=None):
        sid = sid or self.sid
        status, _, _ = self.request("kb", {"documents": [REFERENCE], "sync": True, "use_kb": True}, sid=sid)
        self.assertEqual(status, 200)
        response = io.BytesIO(json.dumps({"message": {"content": ADOPTED}}).encode())
        with patch("server.urllib.request.urlopen", return_value=response):
            status, value, _ = self.request("chat", {"message": EXISTING_INSTRUCTION}, sid=sid)
        self.assertEqual(status, 200)
        self.assertTrue(value["lab"]["last_result"]["triggered"])
        return value["lab"]["flag"]

    def test_first_and_duplicate_http_awards_use_authoritative_metadata(self):
        flag = self.capture()
        for expected in ({"newly_completed": True, "points_awarded": 400},
                         {"newly_completed": False, "points_awarded": 0}):
            status, value, _ = self.request("flag", {"flag": flag}, sid=self.sid)
            self.assertEqual(status, 200)
            self.assertEqual(value["flag_submission"], expected)
            self.assertEqual(value["summary"]["score"], 400)

    def test_eight_concurrent_http_submissions_award_400_points_total(self):
        flag = self.capture()
        with ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(pool.map(lambda _: self.request("flag", {"flag": flag}, sid=self.sid), range(8)))
        self.assertTrue(all(status == 200 for status, _, _ in replies))
        self.assertEqual(sum(value["flag_submission"]["points_awarded"] for _, value, _ in replies), 400)
        self.assertEqual(sum(value["flag_submission"]["newly_completed"] for _, value, _ in replies), 1)
        self.assertTrue(all(value["summary"]["score"] == 400 for _, value, _ in replies))

    def test_eight_independent_application_instances_share_one_transactional_award(self):
        flag = self.capture()
        applications = [server.Application(self.directory) for _ in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(pool.map(lambda app: app.submit_flag(self.sid, 8, {"flag": flag}), applications))
        self.assertEqual(sum(reply["flag_submission"]["points_awarded"] for reply in replies), 400)
        self.assertEqual(sum(reply["flag_submission"]["newly_completed"] for reply in replies), 1)

    def test_invalid_and_cross_student_http_flags_cannot_award(self):
        flag = self.capture()
        other = self.new_student()
        status, result, _ = self.request("flag", {"flag": flag}, sid=other)
        self.assertEqual(status, 403)
        self.assertIn("error", result)
        other_flag = self.capture(other)
        for sid, supplied in ((self.sid, other_flag), (other, flag), (self.sid, "PANOPTIS{incorrect}")):
            status, result, _ = self.request("flag", {"flag": supplied}, sid=sid)
            self.assertEqual(status, 400)
            self.assertIn("error", result)
        status, state, _ = self.request(sid=self.sid)
        self.assertEqual(state["summary"]["score"], 0)

    def test_http_summary_remains_unverified_despite_matching_record_words(self):
        self.request("kb", {"documents": [REFERENCE], "sync": True, "use_kb": True}, sid=self.sid)
        with patch("server.urllib.request.urlopen", return_value=io.BytesIO(json.dumps({"message": {"content": SUMMARY}}).encode())):
            status, value, _ = self.request("chat", {"message": EXISTING_INSTRUCTION, "triggered": True,
                "generation_source": "production_ollama", "verification_outcome": True}, sid=self.sid)
        self.assertEqual(status, 200)
        self.assertTrue(value["lab"]["last_result"]["kb_used"])
        self.assertFalse(value["lab"]["last_result"]["triggered"])
        self.assertIsNone(value["lab"]["flag"])

    def test_http_callback_and_client_evidence_do_not_unlock(self):
        self.request("kb", {"documents": [REFERENCE], "sync": True, "use_kb": True}, sid=self.sid)
        self.app.model = lambda _: ADOPTED
        status, value, _ = self.request("chat", {"message": EXISTING_INSTRUCTION,
            "receipt": {"verification_outcome": True, "generation_source": "production_ollama"},
            "student_id": self.sid, "completed": True, "exploited": True}, sid=self.sid)
        self.assertEqual(status, 200)
        self.assertFalse(value["lab"]["exploited"])
        self.assertIsNone(value["lab"]["flag"])
        self.assertEqual(value["summary"]["score"], 0)

    def test_http_completion_survives_refresh_and_difficulty_until_explicit_restart(self):
        flag = self.capture()
        self.request("flag", {"flag": flag}, sid=self.sid)
        for action, body in (("defense", {"level": 1}), ("defense", {"level": 2})):
            status, value, _ = self.request(action, body, sid=self.sid)
            self.assertEqual(status, 200)
            self.assertTrue(value["lab"]["completed"])
            self.assertFalse(value["lab"]["exploited"])
            self.assertEqual(value["lab"]["flag"], flag)
            self.assertEqual(value["summary"]["score"], 400)
            status, value, _ = self.request("flag", {"flag": flag}, sid=self.sid)
            self.assertEqual(value["flag_submission"]["points_awarded"], 0)
        _, value, _ = self.request(sid=self.sid)
        self.assertTrue(value["labs"]["8"]["completed"])
        self.assertEqual(value["labs"]["8"]["flag"], flag)
        status, reset, _ = self.request("reset", {}, sid=self.sid)
        self.assertEqual(status, 200)
        self.assertFalse(reset["lab"]["completed"])
        self.assertIsNone(reset["lab"]["flag"])
        self.assertEqual(reset["summary"]["score"], 0)
        status, _, _ = self.request("flag", {"flag": flag}, sid=self.sid)
        self.assertEqual(status, 403)

    def test_completion_during_generation_cannot_be_erased_by_old_reply(self):
        flag = self.capture()
        award = []
        def transport(request, timeout):
            other = server.Application(self.directory)
            award.append(other.submit_flag(self.sid, 8, {"flag": flag}))
            return io.BytesIO(json.dumps({"message": {"content": SUMMARY}}).encode())
        with patch("server.urllib.request.urlopen", side_effect=transport):
            status, _, _ = self.request("chat", {"message": EXISTING_INSTRUCTION}, sid=self.sid)
        self.assertEqual(status, 409)
        self.assertEqual(award[0]["flag_submission"]["points_awarded"], 400)
        _, state, _ = self.request(sid=self.sid)
        self.assertTrue(state["labs"]["8"]["completed"])
        self.assertEqual(state["labs"]["8"]["flag"], flag)
        self.assertEqual(state["summary"]["score"], 400)


if __name__ == "__main__":
    unittest.main()
