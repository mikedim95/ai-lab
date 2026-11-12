"""Disposable timeline browser fixture: actual chat/SSE, mocked Ollama only."""
import io
import json
from pathlib import Path
import re
import signal
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import catalog
import server
from test_app import LAB6_IDENTITY_REQUEST
from test_lab9_evidence import NORMAL, QUESTIONS, REFUSALS, DISCLOSURE

release = threading.Event()
started = threading.Event()
control_lock = threading.Lock()
control = {"mode": "normal", "hold": False, "calls": 0}
subscriptions = []
reservations = {str(lab): 0 for lab in range(1, 10)}
reservation_pending = threading.Event()
reservation_release = threading.Event()
delay_reservation = False
MODES = {"normal", "refusal_one", "refusal_two", "disclosure", "error"}


def transport(request, timeout):
    url = request if isinstance(request, str) else request.full_url
    if url == server.OLLAMA_URL + "/api/tags":
        return io.BytesIO(json.dumps({"models": [{"name": server.MODEL}]}).encode())
    if url != server.OLLAMA_URL + "/api/chat":
        raise AssertionError("Unexpected external request from timeline fixture")
    with control_lock:
        mode, hold = control["mode"], control["hold"]
        control["calls"] += 1
    release.clear()
    started.set()
    try:
        if hold and not release.wait(20):
            raise OSError("Timeline fixture inference timed out")
        if mode == "error":
            raise OSError("private-fixture-failure-marker")
        reply = {"normal": NORMAL, "refusal_one": REFUSALS[0],
                 "refusal_two": REFUSALS[1], "disclosure": DISCLOSURE}[mode]
        return io.BytesIO(json.dumps({"message": {"content": reply}}).encode())
    finally:
        started.clear()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    server.TRACE_STREAM_SECONDS = 1.5
    server.TRACE_POLL_SECONDS = .02
    server.TRACE_HEARTBEAT_SECONDS = .05
    with tempfile.TemporaryDirectory(prefix="panoptis-timeline-browser-") as directory:
        app = server.Application(Path(directory))
        base = server.make_handler(app)

        class Handler(base):
            def stream_trace(self, trace_id):
                subscriptions.append({"trace_id": trace_id, "cursor":
                    self.headers.get("Last-Event-ID") or
                    server.parse_qs(server.urlsplit(self.path).query).get("after", ["0"])[0]})
                return super().stream_trace(trace_id)

            def do_GET(self):
                if self.path == "/test/stats":
                    with control_lock:
                        status = dict(control)
                        reserved = dict(reservations)
                    return self.respond(200, {**status, "started": started.is_set(),
                        "reservation_pending": reservation_pending.is_set(),
                        "reservations": reserved,
                        "subscriptions": subscriptions,
                        "active_streams": sum(app.store.trace_streams.active.values())})
                return super().do_GET()

            def do_POST(self):
                global delay_reservation
                if self.path == "/test/reserve-delay":
                    with control_lock:
                        delay_reservation = True
                    return self.respond(200, {"configured": True})
                if self.path == "/test/reserve-release":
                    reservation_release.set()
                    return self.respond(200, {"released": True})
                if re.fullmatch(r"/api/labs/[1-9]/traces", self.path):
                    with control_lock:
                        reservations[self.path.split("/")[3]] += 1
                        delayed, delay_reservation = delay_reservation, False
                    if delayed:
                        reservation_release.clear()
                        reservation_pending.set()
                        reservation_release.wait(.6)
                        reservation_pending.clear()
                if self.path == "/test/release":
                    release.set()
                    return self.respond(200, {"released": True})
                if self.path in {"/test/mode", "/test/check-trace"}:
                    try:
                        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                    except (ValueError, UnicodeDecodeError):
                        return self.respond(400, {"error": "Invalid fixture request"})
                    if self.path == "/test/check-trace":
                        text = body.get("text", "")
                        safe = (isinstance(text, str)
                            and all(field["value"].casefold() not in text.casefold()
                                    for field in catalog.LAB9_PROTECTED_FIELDS)
                            and not re.search(r"(?:PANOPTIS|AIGOAT)\{[^}]+\}", text)
                            and "private-fixture-failure-marker" not in text)
                        return self.respond(200, {"safe": bool(safe)})
                    if body.get("mode") not in MODES or type(body.get("hold", False)) is not bool:
                        return self.respond(400, {"error": "Invalid fixture mode"})
                    with control_lock:
                        control.update(mode=body["mode"], hold=body.get("hold", False))
                    return self.respond(200, {"configured": True})
                return super().do_POST()

        with patch("server.urllib.request.urlopen", side_effect=transport):
            with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as http:
                http.daemon_threads = True
                print(json.dumps({"url": f"http://127.0.0.1:{http.server_port}",
                    "questions": QUESTIONS, "identity_request": LAB6_IDENTITY_REQUEST}), flush=True)
                http.serve_forever()
