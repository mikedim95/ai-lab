#!/usr/bin/env python3
"""Nine separate CTF labs with original AIGoat evaluators and local Ollama."""
import copy
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import signal
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from capstone.bridge import signature

from catalog import (CATALOG, DEFAULT_DOCUMENTS, DEFENSE_NAMES, KB_LABS, ROOT,
                     TOTAL_POINTS, build_messages, check_exploit, defend_input,
                     defend_output, defend_retrieval, fresh_lab, retrieve)

DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.environ.get("OLLAMA_MODEL", "ministral-3:3b")
MODEL_THREADS = int(os.environ.get("OLLAMA_NUM_THREAD", "16"))
MODEL_CONTEXT = int(os.environ.get("OLLAMA_CONTEXT_LENGTH", "8192"))
MODEL_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "24h")
CAPSTONE_URL = os.environ.get("CAPSTONE_URL", "http://127.0.0.1:9000").rstrip("/")


class APIError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class Store:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "nine-labs.sqlite3"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS sessions (sid TEXT PRIMARY KEY, data TEXT NOT NULL, updated REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('flag_key', ?)", (secrets.token_hex(32),))
            self.flag_key = bytes.fromhex(db.execute("SELECT value FROM metadata WHERE name='flag_key'").fetchone()[0])

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, sid):
        with self.connect() as db:
            row = db.execute("SELECT data FROM sessions WHERE sid=?", (sid,)).fetchone() if sid else None
            if row:
                data = json.loads(row[0])
                labs = data.setdefault("labs", {})
                missing = [i for i in range(1, 10) if str(i) not in labs]
                for lab_id in missing:
                    labs[str(lab_id)] = fresh_lab()
                if missing:
                    db.execute("UPDATE sessions SET data=?, updated=? WHERE sid=?", (json.dumps(data), time.time(), sid))
                return sid, data
            sid = secrets.token_urlsafe(32)
            data = {"labs": {str(i): fresh_lab() for i in range(1, 10)}}
            db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (sid, json.dumps(data), time.time()))
            return sid, data

    def save(self, sid, data):
        with self.connect() as db:
            db.execute("UPDATE sessions SET data=?, updated=? WHERE sid=?", (json.dumps(data), time.time(), sid))

    def flag(self, sid, lab_id, attempt):
        digest = hmac.new(self.flag_key, f"{lab_id}:{sid}:{attempt}".encode(), hashlib.sha256).hexdigest()
        return "AIGOAT{" + digest[:32].upper() + "}"


def local_model(messages):
    payload = {"model": MODEL, "messages": messages, "stream": False, "think": False,
               "keep_alive": MODEL_KEEP_ALIVE,
               "options": {"temperature": 0.3, "num_predict": 512, "num_ctx": MODEL_CONTEXT, "num_thread": MODEL_THREADS}}
    request = urllib.request.Request(OLLAMA_URL + "/api/chat", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=240) as response:
            data = json.load(response)
        reply = data.get("message", {}).get("content", "").strip()
        if not reply:
            raise APIError(503, "The local AI returned an empty reply. Please try again.")
        return reply[:14000]
    except (OSError, ValueError, urllib.error.URLError) as exc:
        logging.warning("Local AI request failed: %s", type(exc).__name__)
        raise APIError(503, "The local AI is unavailable or took too long. Your progress is saved; please try again.") from exc


class Application:
    def __init__(self, directory=DATA_DIR, model=local_model):
        self.store = Store(directory)
        self.model = model
        self.generation = threading.BoundedSemaphore(1)
        self.locks = {}
        self.locks_guard = threading.Lock()

    def session_lock(self, sid):
        with self.locks_guard:
            return self.locks.setdefault(sid, threading.Lock())

    def summary(self, data):
        completed = [item["id"] for item in CATALOG if data["labs"][str(item["id"])]["completed"]]
        return {"completed": completed, "score": sum(CATALOG[i - 1]["points"] for i in completed),
                "total_points": TOTAL_POINTS, "total_labs": 9}

    def public_lab(self, sid, lab_id, record):
        return {"id": lab_id, "messages": record["messages"], "completed": record["completed"],
                "exploited": record["exploited"], "flag": self.store.flag(sid, lab_id, record["attempt"]) if record["exploited"] else None,
                "documents": record["documents"] if lab_id in KB_LABS else [],
                "kb_revision": record["kb_revision"], "synced_revision": record["synced_revision"],
                "use_kb": record["use_kb"], "last_result": record["last_result"],
                "defense_level": record.get("defense_level", 0)}

    def state(self, sid, data):
        return {"catalog": CATALOG, "summary": self.summary(data),
                "labs": {str(i): self.public_lab(sid, i, data["labs"][str(i)]) for i in range(1, 10)}}

    def lab_result(self, sid, lab_id, data):
        return {"lab": self.public_lab(sid, lab_id, data["labs"][str(lab_id)]), "summary": self.summary(data)}

    def chat(self, sid, lab_id, body):
        message = body.get("message")
        if not isinstance(message, str) or not message.strip() or len(message) > 4000:
            raise APIError(400, "Write a message between 1 and 4,000 characters.")
        if "use_kb" in body and not isinstance(body["use_kb"], bool):
            raise APIError(400, "Use knowledge base must be on or off.")
        message = message.strip()
        if not self.generation.acquire(blocking=False):
            raise APIError(429, "The local AI is finishing another reply. You can explore the other labs and try again shortly.")
        try:
            with self.session_lock(sid):
                _, data = self.store.get(sid)
                snapshot = copy.deepcopy(data["labs"][str(lab_id)])
            started = time.monotonic()
            level = snapshot.get("defense_level", 0)
            cleaned_message, blocked_reason = defend_input(message, level)
            use_kb = body.get("use_kb", snapshot["use_kb"]) and lab_id in KB_LABS
            entries = retrieve(cleaned_message, snapshot["synced_documents"]) if use_kb and not blocked_reason else []
            entries, excluded_entries = defend_retrieval(entries, level)
            # Bound context size without misreporting which content the evaluator saw.
            remaining = 6500
            used_entries = []
            for entry in entries:
                if remaining <= 0:
                    break
                entry["content"] = entry["content"][:remaining]
                remaining -= len(entry["content"])
                used_entries.append(entry)
            if blocked_reason:
                reply = "I can help with shop products and your own orders, but I cannot follow that request."
            else:
                reply = self.model(build_messages(lab_id, snapshot, cleaned_message, used_entries))
            if not isinstance(reply, str) or not reply.strip():
                raise APIError(503, "The local AI returned an empty reply. Please try again.")
            if not blocked_reason:
                reply, blocked_reason = defend_output(reply, level)
            history = snapshot["messages"] + [{"role": "user", "content": message}, {"role": "assistant", "content": reply}]
            triggered = not blocked_reason and check_exploit(lab_id, message, reply, history, used_entries, level)
            with self.session_lock(sid):
                _, data = self.store.get(sid)
                record = data["labs"][str(lab_id)]
                if record["attempt"] != snapshot["attempt"]:
                    raise APIError(409, "This lab was restarted while the AI was replying. The old reply was discarded.")
                record["messages"] = history[-60:]
                record["use_kb"] = use_kb
                record["exploited"] = record["exploited"] or triggered
                record["last_result"] = {
                    "triggered": triggered, "seconds": round(time.monotonic() - started, 1),
                    "kb_used": bool(used_entries), "sources": [{"id": doc["id"], "title": doc["title"], "injected": doc["is_user_injected"]} for doc in used_entries],
                    "defense_level": level, "defense_blocked": bool(blocked_reason),
                    "defense_reason": blocked_reason, "excluded_sources": excluded_entries,
                    "message": (f"{DEFENSE_NAMES[level]} defenses blocked this attempt. Try a different approach."
                                if blocked_reason else "Exploit verified. Submit the captured flag to earn the points."
                                if record["exploited"] else "No exploit verified yet. Keep experimenting or reveal a hint."),
                }
                self.store.save(sid, data)
                return self.lab_result(sid, lab_id, data)
        finally:
            self.generation.release()

    def update_kb(self, sid, lab_id, body):
        if lab_id not in KB_LABS:
            raise APIError(400, "This lab does not use a knowledge base.")
        documents = body.get("documents")
        if not isinstance(documents, list) or len(documents) > 8:
            raise APIError(400, "Keep up to eight documents in this lab’s knowledge base.")
        if not isinstance(body.get("use_kb", False), bool) or not isinstance(body.get("sync", False), bool):
            raise APIError(400, "Knowledge base settings must be on or off.")
        clean = []
        seen = set()
        for doc in documents:
            if not isinstance(doc, dict):
                raise APIError(400, "Each document needs a title and text.")
            title, content = doc.get("title"), doc.get("content")
            doc_id = doc.get("id") or secrets.token_hex(8)
            if (not isinstance(title, str) or not title.strip() or len(title) > 100 or
                not isinstance(content, str) or not content.strip() or len(content) > 4000 or
                not isinstance(doc_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,60}", doc_id) or doc_id in seen):
                raise APIError(400, "Use a unique document ID, a title under 100 characters, and text under 4,000 characters.")
            seen.add(doc_id)
            trusted = any(original["id"] == doc_id and original["title"] == title.strip() and original["content"] == content.strip() for original in DEFAULT_DOCUMENTS)
            clean.append({"id": doc_id, "title": title.strip(), "content": content.strip(), "is_user_injected": not trusted})
        if sum(len(doc["content"]) for doc in clean) > 14000:
            raise APIError(400, "Keep the combined document text under 14,000 characters.")
        with self.session_lock(sid):
            _, data = self.store.get(sid)
            record = data["labs"][str(lab_id)]
            if clean != record["documents"]:
                record["documents"] = clean
                record["kb_revision"] += 1
            record["use_kb"] = body.get("use_kb", record["use_kb"])
            if body.get("sync"):
                record["synced_documents"] = copy.deepcopy(clean)
                record["synced_revision"] = record["kb_revision"]
            self.store.save(sid, data)
            return self.lab_result(sid, lab_id, data)

    def submit_flag(self, sid, lab_id, body):
        flag = body.get("flag")
        if not isinstance(flag, str) or len(flag) > 150:
            raise APIError(400, "Paste a captured AIGOAT{…} flag.")
        with self.session_lock(sid):
            _, data = self.store.get(sid)
            record = data["labs"][str(lab_id)]
            if not record["exploited"]:
                raise APIError(403, "Trigger this lab’s exploit in the chat before submitting a flag.")
            if not hmac.compare_digest(flag.strip(), self.store.flag(sid, lab_id, record["attempt"])):
                raise APIError(400, "That flag does not match this lab and attempt.")
            record["completed"] = True
            self.store.save(sid, data)
            return self.lab_result(sid, lab_id, data)

    def update_defense(self, sid, lab_id, body):
        level = body.get("level")
        if type(level) is not int or level not in (0, 1, 2):
            raise APIError(400, "Choose Vulnerable, Hardened, or Guardrailed.")
        with self.session_lock(sid):
            _, data = self.store.get(sid)
            current = data["labs"][str(lab_id)]
            if current.get("defense_level", 0) != level:
                replacement = fresh_lab(current["attempt"] + 1, defense_level=level)
                for key in ("documents", "synced_documents", "kb_revision", "synced_revision", "use_kb"):
                    replacement[key] = copy.deepcopy(current[key])
                data["labs"][str(lab_id)] = replacement
                self.store.save(sid, data)
            return self.lab_result(sid, lab_id, data)

    def reset(self, sid, lab_id):
        with self.session_lock(sid):
            _, data = self.store.get(sid)
            current = data["labs"][str(lab_id)]
            data["labs"][str(lab_id)] = fresh_lab(current["attempt"] + 1, current.get("defense_level", 0))
            self.store.save(sid, data)
            return self.lab_result(sid, lab_id, data)


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NineLabs"

        def respond(self, status, value, mime="application/json; charset=utf-8", sid=None, disposition=None):
            raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode() if mime.startswith("application/json") else value
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if disposition:
                self.send_header("Content-Disposition", disposition)
            if sid:
                self.send_header("Set-Cookie", f"nine_sid={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000")
            self.end_headers()
            self.wfile.write(raw)

        def session(self):
            jar = cookies.SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie", ""))
                sid = jar["nine_sid"].value if "nine_sid" in jar else None
            except cookies.CookieError:
                sid = None
            if sid and not re.fullmatch(r"[A-Za-z0-9_-]{43}", sid):
                sid = None
            return app.store.get(sid)

        def proxy_capstone(self, method):
            path = urlsplit(self.path).path
            get_pattern = r"/api/killchain/(?:state|events|examples|models|fixtures/invoice\.pdf|attachments/[0-9]+(?:/file)?)"
            post_pattern = r"/api/killchain/(?:mode|reviews|tickets|tickets/[0-9]+/attachment|turn|approvals/[0-9]+/decision|storefront/coupon|cleanup/(?:agent_memory|connector_cache|soft_reset|hard_reset))"
            if not re.fullmatch(get_pattern if method == "GET" else post_pattern, path):
                raise APIError(404, "Capstone action not found.")
            sid, _ = self.session()
            headers = {"X-Nine-Session": sid, "X-Nine-Signature": signature(app.store.flag_key, sid)}
            data = None
            if method == "POST":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise APIError(400, "Invalid request length.")
                if not 0 < length <= 1024 * 1024:
                    raise APIError(413, "The capstone upload is too large.")
                content_type = self.headers.get("Content-Type", "")
                if not (content_type.startswith("application/json") or content_type.startswith("multipart/form-data;")):
                    raise APIError(415, "Send a JSON request or a PDF upload form.")
                headers["Content-Type"] = content_type
                data = self.rfile.read(length)
            request = urllib.request.Request(CAPSTONE_URL + self.path, data=data, headers=headers, method=method)
            try:
                try:
                    response = urllib.request.urlopen(request, timeout=900)
                except urllib.error.HTTPError as exc:
                    response = exc
                with response:
                    raw = response.read(12 * 1024 * 1024)
                    self.respond(response.status, raw, response.headers.get("Content-Type", "application/json"), sid=sid,
                                 disposition=response.headers.get("Content-Disposition"))
            except (OSError, urllib.error.URLError) as exc:
                raise APIError(503, "The capstone engine is unavailable. Your nine CTF labs and saved progress are still available.") from exc

        def do_GET(self):
            try:
                path = urlsplit(self.path).path
                if path.startswith("/api/killchain/"):
                    return self.proxy_capstone("GET")
                if path == "/api/state":
                    sid, data = self.session()
                    return self.respond(200, app.state(sid, data), sid=sid)
                if path == "/api/health":
                    ready = False
                    try:
                        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=5) as response:
                            ready = any(item.get("name") == MODEL for item in json.load(response).get("models", []))
                    except (OSError, ValueError):
                        pass
                    return self.respond(200, {"status": "ok", "ai_ready": ready, "model": MODEL})
                files = {"/app.js": ("static/app.js", "text/javascript; charset=utf-8"),
                         "/boiler.js": ("static/boiler.js", "text/javascript; charset=utf-8"),
                         "/boiler.css": ("static/boiler.css", "text/css; charset=utf-8"),
                         "/boiler-panel.css": ("static/boiler-panel.css", "text/css; charset=utf-8"),
                         "/intro.js": ("static/intro.js", "text/javascript; charset=utf-8"),
                         "/killchain.js": ("static/killchain.js", "text/javascript; charset=utf-8"),
                         "/killchain.css": ("static/killchain.css", "text/css; charset=utf-8"),
                         "/style.css": ("static/style.css", "text/css; charset=utf-8"),
                         "/favicon.svg": ("static/favicon.svg", "image/svg+xml"),
                         "/attribution": ("ATTRIBUTION.md", "text/plain; charset=utf-8"),
                         "/licenses/training": ("upstream/TRAINING_LICENSE.md", "text/plain; charset=utf-8"),
                         "/capstone-guide": ("upstream/docs/labs/killchain/README.md", "text/plain; charset=utf-8")}
                if path == "/killchain":
                    return self.respond(200, (ROOT / "static/killchain.html").read_bytes(), "text/html; charset=utf-8")
                if path == "/" or re.fullmatch(r"/lab/[1-9]/?", path):
                    return self.respond(200, (ROOT / "static/index.html").read_bytes(), "text/html; charset=utf-8")
                if path not in files:
                    raise APIError(404, "Lab or page not found.")
                name, mime = files[path]
                self.respond(200, (ROOT / name).read_bytes(), mime)
            except APIError as exc:
                self.respond(exc.status, {"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            sid = None
            try:
                origin = self.headers.get("Origin")
                if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                    raise APIError(403, "Open the app directly to use this lab.")
                if urlsplit(self.path).path.startswith("/api/killchain/"):
                    return self.proxy_capstone("POST")
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    raise APIError(415, "Send a JSON request.")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise APIError(400, "Invalid request length.")
                if not 0 < length <= 65536:
                    raise APIError(413, "This request is too large.")
                try:
                    body = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeDecodeError):
                    raise APIError(400, "This request could not be read.")
                if not isinstance(body, dict):
                    raise APIError(400, "Send a JSON object.")
                match = re.fullmatch(r"/api/labs/([1-9])/(chat|kb|flag|reset|defense)", self.path)
                if not match:
                    raise APIError(404, "Lab or action not found.")
                lab_id, action = int(match[1]), match[2]
                sid, _ = self.session()
                if action == "chat":
                    result = app.chat(sid, lab_id, body)
                elif action == "kb":
                    result = app.update_kb(sid, lab_id, body)
                elif action == "flag":
                    result = app.submit_flag(sid, lab_id, body)
                elif action == "defense":
                    result = app.update_defense(sid, lab_id, body)
                else:
                    result = app.reset(sid, lab_id)
                self.respond(200, result, sid=sid)
            except APIError as exc:
                self.respond(exc.status, {"error": str(exc)}, sid=sid)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                logging.exception("Request failed")
                self.respond(500, {"error": "Something went wrong. Your saved progress is safe; please try again."}, sid=sid)

    return Handler


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    address = (os.environ.get("BIND_HOST", "127.0.0.1"), int(os.environ.get("PORT", "3002")))
    httpd = ThreadingHTTPServer(address, make_handler(Application()))
    httpd.daemon_threads = True
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=httpd.shutdown, daemon=True).start())
    logging.info("Nine Labs listening on %s:%s", *address)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
