"""Disposable browser server with blocked, mocked Ollama transport only."""
import io
import json
from pathlib import Path
import signal
import sqlite3
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server

release = threading.Event()
started = threading.Event()
subscriptions = []
stream_reads = threading.local()
read_failure = threading.Event()
read_failure_lock = threading.Lock()
read_errors = 0
REPLY = "Public maintenance handovers record completed checks and the next planned review."


def transport(request, timeout):
    url = request if isinstance(request, str) else request.full_url
    if url.endswith('/api/tags'):
        return io.BytesIO(json.dumps({'models':[{'name':server.MODEL}]}).encode())
    if url != server.OLLAMA_URL + '/api/chat':
        raise AssertionError('Unexpected external request from browser fixture')
    release.clear(); started.set()
    try:
        if not release.wait(20):
            raise OSError('Mocked inference fixture timed out')
        return io.BytesIO(json.dumps({'message':{'content':REPLY}}).encode())
    finally:
        started.clear()


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    server.TRACE_STREAM_SECONDS = .15
    server.TRACE_POLL_SECONDS = .02
    server.TRACE_HEARTBEAT_SECONDS = .04
    with tempfile.TemporaryDirectory(prefix='panoptis-sse-browser-') as directory:
        app = server.Application(Path(directory))
        original_read = app.store.read_trace

        def read_trace(*args, **kwargs):
            global read_errors
            count = getattr(stream_reads, 'count', 0)
            stream_reads.count = count + 1
            with read_failure_lock:
                # Fail only after the authenticated initial read, so the real
                # SSE handler emits a nonpersisted transport-only terminal.
                if count > 0 and read_failure.is_set():
                    read_failure.clear()
                    read_errors += 1
                    raise sqlite3.OperationalError('Isolated transient trace read failure')
            return original_read(*args, **kwargs)

        app.store.read_trace = read_trace
        base = server.make_handler(app)
        class Handler(base):
            def stream_trace(self, trace_id):
                stream_reads.count = 0
                subscriptions.append({'trace_id':trace_id,'cursor':self.headers.get('Last-Event-ID') or
                    server.parse_qs(server.urlsplit(self.path).query).get('after',['0'])[0]})
                return super().stream_trace(trace_id)

            def do_GET(self):
                if self.path == '/test/stats':
                    return self.respond(200, {'started':started.is_set(),'subscriptions':subscriptions,
                                             'read_errors':read_errors,
                                             'active_streams':sum(app.store.trace_streams.active.values())})
                return super().do_GET()

            def do_POST(self):
                if self.path == '/test/read-error':
                    read_failure.set()
                    return self.respond(200, {'configured':True})
                if self.path == '/test/release':
                    release.set()
                    return self.respond(200, {'released':True})
                return super().do_POST()

        with patch('server.urllib.request.urlopen', side_effect=transport):
            with ThreadingHTTPServer(('127.0.0.1',0), Handler) as http:
                http.daemon_threads = True
                print(json.dumps({'url':f'http://127.0.0.1:{http.server_port}','reply':REPLY}),flush=True)
                http.serve_forever()
