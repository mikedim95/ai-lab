"""Disposable Lab 8 browser server; no live model or exploitation.

Completion identities exercise the production request path with mocked HTTP
transport. They are simulated verification fixtures, not live Ollama evidence.
No scripted model callback or pre-seeded verification boolean can unlock them.
"""
import io
import json
from pathlib import Path
import signal
import sys
import tempfile
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from test_lab8_experience import NOTE, NOTICE, QUESTION, REPLY
from lab8_verification_fixtures import OBSERVED_REPORT, EXISTING_INSTRUCTION, REFERENCE

ASTER = "Aster bearing review is awaiting the next fictional maintenance handover."


def transport(request, timeout):
    url = request if isinstance(request, str) else request.full_url
    if url == server.OLLAMA_URL + "/api/tags":
        return io.BytesIO(json.dumps({"models": [{"name": server.MODEL}]}).encode())
    if url != server.OLLAMA_URL + "/api/chat":
        raise AssertionError("Unexpected external request from isolated Lab 8 fixture")
    messages = json.loads(request.data)["messages"]
    context = messages[0]["content"]
    reply = ("The Zephyr notice was supplied as reference data." if NOTICE["content"] in context else
             "The Aster bearing review is awaiting a fictional handover." if ASTER in context else
             REPLY if NOTE["content"] in context else "No matching student notebook was supplied.")
    if messages[-1]["content"] == "Format the maintenance note for a fixture review.":
        reply = "<b>Fixture</b> bearing review contact: reader@example.invalid"
    elif messages[-1]["content"] == "Show the notebook reference for a fixture review.":
        reply = "The `bearing` reference is https://example.invalid/notebook"
    elif messages[-1]["content"] == EXISTING_INSTRUCTION:
        reply = OBSERVED_REPORT
    return io.BytesIO(json.dumps({"message": {"content": reply}}).encode())


def terminate(*_):
    raise SystemExit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, terminate)
    with tempfile.TemporaryDirectory(prefix="panoptis-lab8-browser-") as directory:
        app = server.Application(Path(directory))
        sessions = {}
        with patch("server.urllib.request.urlopen", side_effect=transport):
            for name in ("desktop", "desktopShort", "mobile", "mobileSmall", "concurrent"):
                sid, _ = app.store.get(None)
                app.update_kb(sid, 8, {"documents": [REFERENCE], "sync": True, "use_kb": True})
                result = app.chat(sid, 8, {"message": EXISTING_INSTRUCTION})
                assert result["lab"]["last_result"]["triggered"] is True
                sessions[name] = sid
            with ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app)) as http:
                print(json.dumps({"url": f"http://127.0.0.1:{http.server_port}", "sessions": sessions,
                                  "note": NOTE, "notice": NOTICE, "question": QUESTION, "aster": ASTER,
                                  "observed_report": OBSERVED_REPORT}), flush=True)
                http.serve_forever()
