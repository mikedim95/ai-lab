"""Real concurrent HTTP/SSE integration; temporary identities and databases."""
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
from test_app import LAB6_IDENTITY_REQUEST
from test_lab9_evidence import NORMAL, QUESTIONS, REFUSALS


class TraceStreamTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.app = server.Application(self.directory)
        self.sid, _ = self.app.store.get(None)
        self.other, _ = self.app.store.get(None)
        self.connections = []
        self.patches = [patch.object(server, 'TRACE_POLL_SECONDS', .01),
                        patch.object(server, 'TRACE_HEARTBEAT_SECONDS', .03),
                        patch.object(server, 'TRACE_STREAM_SECONDS', 2)]
        for item in self.patches:
            item.start(); self.addCleanup(item.stop)
        self.start_http()

    def start_http(self):
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.make_handler(self.app))
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        for connection, response in self.connections:
            response.close(); connection.close()
        self.httpd.shutdown(); self.httpd.server_close(); self.thread.join()

    def request(self, path, body=None, *, sid=None, headers=None, raw=None):
        connection = http.client.HTTPConnection(*self.httpd.server_address, timeout=5)
        values = {'Content-Type':'application/json'}
        if sid is not False: values['Cookie'] = 'nine_sid=' + (sid or self.sid)
        values.update(headers or {})
        payload = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        connection.request('POST' if payload is not None else 'GET', path, payload, values)
        response = connection.getresponse()
        result = (response.status, dict(response.getheaders()), json.loads(response.read()))
        connection.close()
        return result

    def reserve(self, lab=9, sid=None):
        status, _, value = self.request(f'/api/labs/{lab}/traces', {}, sid=sid)
        self.assertEqual(status, 201)
        self.assertRegex(value['trace_id'], r'^[a-f0-9]{32}$')
        return value['trace_id']

    def chat(self, trace_id=None, *, lab=9, sid=None, message='Explain public maintenance procedures.'):
        return self.request(f'/api/labs/{lab}/chat', {'message':message}, sid=sid,
                            headers={'X-Trace-Id':trace_id} if trace_id else {})

    def stream(self, trace_id, *, sid=None, after=None, header=None):
        connection = http.client.HTTPConnection(*self.httpd.server_address, timeout=3)
        headers = {} if sid is False else {'Cookie':'nine_sid='+(sid or self.sid)}
        if header is not None: headers['Last-Event-ID'] = str(header)
        connection.request('GET', f'/api/traces/{trace_id}/events'+('' if after is None else '?after='+str(after)), headers=headers)
        response = connection.getresponse()
        self.connections.append((connection, response))
        return response

    @staticmethod
    def next_event(response):
        fields = {}
        while True:
            line = response.readline()
            if not line: return None
            text = line.decode().rstrip('\r\n')
            if not text:
                if 'data' in fields:
                    return fields.get('event', 'message'), fields.get('id'), json.loads(fields['data'])
                fields = {}; continue
            if not text.startswith(':') and ':' in text:
                name, value = text.split(':', 1); fields[name] = value.lstrip()

    def all_events(self, response):
        events = []
        while True:
            item = self.next_event(response)
            if item is None: break
            events.append(item)
            if item[0] in {'terminal','end'}: break
        return events

    @staticmethod
    def transport(request, timeout):
        return io.BytesIO(json.dumps({'message':{'content':NORMAL}}).encode())

    def assert_released(self):
        deadline = time.monotonic()+2
        while self.app.store.trace_streams.active and time.monotonic()<deadline:
            time.sleep(.02)
        self.assertEqual(self.app.store.trace_streams.active, {})

    def test_trace_id_and_live_events_arrive_before_ollama_finishes(self):
        trace_id = self.reserve()
        stream = self.stream(trace_id)
        self.assertEqual(stream.status, 200)
        self.assertEqual(stream.getheader('Content-Type'), 'text/event-stream; charset=utf-8')
        started, release = threading.Event(), threading.Event()
        def slow(request, timeout):
            started.set()
            if not release.wait(4): raise RuntimeError('Fixture timed out')
            return self.transport(request, timeout)
        with ThreadPoolExecutor(1) as pool, patch('server.urllib.request.urlopen', side_effect=slow):
            future = pool.submit(self.chat, trace_id)
            received = []
            try:
                self.assertTrue(started.wait(2))
                while True:
                    item = self.next_event(stream); received.append(item)
                    if item[2]['stage']=='ollama' and item[2]['status']=='running': break
                self.assertFalse(future.done())
            finally: release.set()
            status, headers, value = future.result(timeout=4)
            received += self.all_events(stream)
        self.assertEqual(status, 200)
        self.assertEqual(headers['X-Trace-Id'], trace_id)
        self.assertEqual(value['summary']['score'], 0)
        self.assertEqual(received[-1][0], 'terminal')
        self.assertEqual(received[-1][2]['status'], 'passed')
        ids = [item[2]['event_id'] for item in received]
        self.assertEqual(ids, list(range(1, len(ids)+1)))
        for kind, event_id, event in received:
            self.assertEqual(event_id, str(event['event_id']))
            self.assertEqual(event['trace_id'], trace_id)
            self.assertTrue({'stage','status','timestamp','duration_ms','reason_code'} <= set(event))
        self.assert_released()

    def test_reconnect_replays_missed_events_and_terminal_in_order(self):
        trace_id = self.reserve()
        with patch('server.urllib.request.urlopen', side_effect=self.transport): self.chat(trace_id)
        first = self.all_events(self.stream(trace_id))
        cursor = first[5][2]['event_id']
        replay = self.all_events(self.stream(trace_id, header=cursor))
        self.assertEqual(replay, first[6:])
        self.assertEqual(self.all_events(self.stream(trace_id, after=cursor)), first[6:])
        self.assertEqual(self.all_events(self.stream(trace_id, header=first[-1][2]['event_id']))[0][0], 'end')

    def test_terminal_distinguishes_backend_block_from_genuine_model_refusal(self):
        self.app.update_defense(self.sid, 6, {'level':1})
        trace_id = self.reserve(6)
        self.assertEqual(self.chat(trace_id, lab=6, message=LAB6_IDENTITY_REQUEST)[0], 200)
        blocked = self.all_events(self.stream(trace_id))
        self.assertEqual(blocked[-1][2]['status'], 'blocked')
        trace_id = self.reserve()
        with patch('server.urllib.request.urlopen', return_value=io.BytesIO(json.dumps({'message':{'content':REFUSALS[0]}}).encode())):
            self.chat(trace_id, message=QUESTIONS[0])
        refusal = self.all_events(self.stream(trace_id))
        self.assertEqual(refusal[-1][2]['status'], 'passed')
        self.assertIn('model_refusal', [item[2]['reason_code'] for item in refusal])
        self.assertNotIn('blocked', [item[2]['status'] for item in refusal])

    def test_ollama_error_has_a_sanitized_persisted_terminal(self):
        trace_id = self.reserve()
        with patch('server.urllib.request.urlopen', side_effect=OSError('private-credential-marker')):
            self.assertEqual(self.chat(trace_id)[0], 503)
        events = self.all_events(self.stream(trace_id))
        self.assertEqual(events[-1][2]['status'], 'error')
        self.assertTrue(events[-1][2]['persisted'])
        self.assertNotIn('private-credential-marker', json.dumps(events))

    def test_rejected_invalid_body_uses_reserved_trace_and_finishes_blocked(self):
        trace_id = self.reserve()
        status, headers, _ = self.request('/api/labs/9/chat', raw=b'not-json', headers={'X-Trace-Id':trace_id})
        self.assertEqual(status, 400)
        self.assertEqual(headers['X-Trace-Id'], trace_id)
        self.assertEqual(self.all_events(self.stream(trace_id))[-1][2]['status'], 'blocked')

    def test_other_students_cannot_read_or_claim_a_trace(self):
        trace_id = self.reserve()
        self.assertEqual(self.stream(trace_id, sid=self.other).status, 404)
        self.assertEqual(self.stream(trace_id, sid=False).status, 401)
        with patch('server.urllib.request.urlopen', side_effect=self.transport):
            status, headers, _ = self.chat(trace_id, sid=self.other)
        self.assertEqual(status, 200)
        self.assertNotEqual(headers['X-Trace-Id'], trace_id)
        self.assertEqual(self.app.store.read_trace(self.sid, trace_id)[1], [])
        self.assertEqual(self.stream(headers['X-Trace-Id']).status, 404)

    def test_reservation_requires_an_existing_session_and_never_creates_one(self):
        self.assertEqual(self.request('/api/labs/9/traces', {}, sid=False)[0], 401)
        self.assertEqual(self.request('/api/labs/9/traces', {}, sid='a'*43)[0], 401)
        with self.app.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 2)

    def test_replayed_expired_wrong_lab_and_stale_reservations_do_not_attach_to_new_chat(self):
        with patch('server.urllib.request.urlopen', side_effect=self.transport):
            trace_id = self.reserve(); self.chat(trace_id)
            before = self.app.store.read_trace(self.sid, trace_id)[1]
            self.assertNotEqual(self.chat(trace_id)[1]['X-Trace-Id'], trace_id)
            self.assertEqual(self.app.store.read_trace(self.sid, trace_id)[1], before)
            expired = self.reserve()
            with self.app.store.connect() as db: db.execute('UPDATE chat_traces SET created_at=? WHERE trace_id=?', (time.time()-40, expired))
            self.assertNotEqual(self.chat(expired)[1]['X-Trace-Id'], expired)
            wrong = self.reserve()
            self.assertNotEqual(self.chat(wrong, lab=5)[1]['X-Trace-Id'], wrong)
            stale = self.reserve()
            self.app.reset(self.sid, 9)
            self.assertNotEqual(self.chat(stale)[1]['X-Trace-Id'], stale)

    def test_reservation_quota_does_not_block_an_untraced_chat(self):
        for _ in range(4): self.reserve()
        self.assertEqual(self.request('/api/labs/9/traces', {})[0], 429)
        with patch('server.urllib.request.urlopen', side_effect=self.transport): self.assertEqual(self.chat()[0], 200)

    def test_cursor_validation_and_stream_quota(self):
        trace_id = self.reserve()
        for cursor in ('-1','abc','999999999999', 1):
            self.assertEqual(self.stream(trace_id, after=cursor).status, 400)
        one, two = self.stream(trace_id), self.stream(trace_id)
        self.assertEqual((one.status, two.status), (200,200))
        self.assertEqual(self.stream(trace_id).status, 429)
        for response in (one,two):response.close()
        for connection, _ in self.connections:connection.close()
        self.assert_released()

    def test_rotating_a_long_running_stream_releases_its_slot(self):
        trace_id = self.reserve()
        with patch.object(server, 'TRACE_STREAM_SECONDS', .04):
            self.assertEqual(self.all_events(self.stream(trace_id)), [])
        self.assert_released()
        with patch('server.urllib.request.urlopen', side_effect=self.transport): self.chat(trace_id)
        self.assertEqual(self.all_events(self.stream(trace_id))[-1][0], 'terminal')

    def test_trace_write_failure_still_returns_chat_and_a_terminal(self):
        trace_id = self.reserve()
        with patch.object(self.app.store, 'append_trace_event', side_effect=sqlite3.OperationalError('private-failure')):
            with patch('server.urllib.request.urlopen', side_effect=self.transport):
                self.assertEqual(self.chat(trace_id)[0], 200)
        events = self.all_events(self.stream(trace_id))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][2]['reason_code'], 'trace_unavailable')

    def test_failed_terminal_write_closes_stream_without_claiming_chat_failed(self):
        trace_id = self.reserve()
        with patch.object(self.app.store, 'finish_trace', side_effect=sqlite3.OperationalError('private-failure')):
            with patch('server.urllib.request.urlopen', side_effect=self.transport):
                self.assertEqual(self.chat(trace_id)[0], 200)
        events = self.all_events(self.stream(trace_id))
        self.assertEqual(events[-1][0], 'terminal')
        self.assertFalse(events[-1][2]['persisted'])
        self.assertEqual(events[-1][2]['scope'], 'stream')
        self.assertIsNone(events[-1][1])
        self.assert_released()

    def test_midstream_database_failure_terminates_only_the_stream(self):
        trace_id = self.reserve()
        original = self.app.store.read_trace
        with patch.object(self.app.store, 'read_trace', side_effect=[original(self.sid, trace_id), sqlite3.OperationalError('private-failure')]):
            events = self.all_events(self.stream(trace_id))
        self.assertEqual(events[-1][2]['reason_code'], 'stream_unavailable')
        self.assertFalse(events[-1][2]['persisted'])
        with patch('server.urllib.request.urlopen', side_effect=self.transport): self.assertEqual(self.chat(trace_id)[0], 200)

    def test_completed_replay_survives_backend_restart(self):
        trace_id = self.reserve()
        with patch('server.urllib.request.urlopen', side_effect=self.transport): self.chat(trace_id)
        before = self.all_events(self.stream(trace_id))
        self.httpd.shutdown(); self.httpd.server_close(); self.thread.join()
        self.app = server.Application(self.directory); self.start_http()
        self.assertEqual(self.all_events(self.stream(trace_id)), before)

    def test_completion_after_a_read_snapshot_drains_the_durable_terminal(self):
        trace_id = self.reserve()
        with patch('server.urllib.request.urlopen', side_effect=self.transport): self.chat(trace_id)
        original = self.app.store.read_trace
        record, events = original(self.sid, trace_id)
        stale = {**record, 'finished_at':None, 'state':'running'}
        calls = 0
        def snapshot(sid, requested, after=0):
            nonlocal calls
            calls += 1
            return (stale, events[:-1]) if calls == 1 else original(sid, requested, after)
        with patch.object(self.app.store, 'read_trace', side_effect=snapshot):
            received = self.all_events(self.stream(trace_id))
        self.assertTrue(received[-1][2]['persisted'])
        self.assertEqual(received[-1][2]['reason_code'], 'trace_completed')

    def test_trace_reservation_database_failure_does_not_affect_chat(self):
        with patch.object(self.app.store, 'reserve_trace', side_effect=sqlite3.OperationalError('private-failure')):
            status, _, body = self.request('/api/labs/9/traces', {})
        self.assertEqual(status, 503); self.assertNotIn('private-failure', json.dumps(body))
        with patch('server.urllib.request.urlopen', side_effect=self.transport): self.assertEqual(self.chat()[0], 200)

    def test_task1_schema_migrates_without_erasing_existing_bindings_or_sessions(self):
        with self.app.store.connect() as db:
            sessions = db.execute('SELECT * FROM sessions ORDER BY sid').fetchall()
            db.execute('DROP TABLE chat_traces')
            db.execute('''CREATE TABLE chat_traces (trace_id TEXT PRIMARY KEY,student_id TEXT,lab_id INTEGER NOT NULL,
                difficulty INTEGER,attempt INTEGER,turn_id TEXT NOT NULL,created_at REAL NOT NULL,finished_at REAL)''')
            db.execute('INSERT INTO chat_traces VALUES (?,?,?,?,?,?,?,?)',
                       ('b'*32,hashlib.sha256(self.sid.encode()).hexdigest(),9,0,1,'c'*32,1,10))
        reopened = server.Application(self.directory)
        with reopened.store.connect() as db:
            self.assertEqual(db.execute('SELECT * FROM sessions ORDER BY sid').fetchall(), sessions)
            self.assertEqual(db.execute('SELECT trace_id,attempt,finished_at,state,started_at FROM chat_traces').fetchone(),
                             ('b'*32,1,10,'finished',None))

    def test_events_have_no_cookie_prompts_records_flags_or_credentials(self):
        trace_id = self.reserve()
        with patch('server.urllib.request.urlopen', side_effect=self.transport): self.chat(trace_id, message='private-input-marker')
        serialized = json.dumps(self.all_events(self.stream(trace_id)))
        for value in (self.sid, 'private-input-marker', NORMAL, catalog.LAB9_PROMPT,
                      self.app.store.flag(self.sid, 9, 1), *[field['value'] for field in catalog.LAB9_PROTECTED_FIELDS]):
            self.assertNotIn(value, serialized)

    def test_malformed_persisted_reason_is_never_projected(self):
        trace_id = self.reserve()
        with patch('server.urllib.request.urlopen', side_effect=self.transport): self.chat(trace_id)
        with self.app.store.connect() as db: db.execute('UPDATE chat_trace_events SET reason_code=? WHERE trace_id=? AND sequence=1', ('private-record-value',trace_id))
        response = self.stream(trace_id)
        self.assertEqual(response.status, 503)
        self.assertNotIn('private-record-value', response.read().decode())

    def test_runtime_and_event_buffers_are_bounded(self):
        registry = server.TraceStreams()
        for index in range(16): self.assertTrue(registry.acquire(str(index)))
        self.assertFalse(registry.acquire('extra'))
        for index in range(16): registry.release(str(index))
        for index in range(200): registry.finish(str(index), 'passed', 'trace_completed')
        self.assertEqual(len(registry.completed), 128)
        trace_id = self.reserve()
        with patch.object(server, 'TRACE_MAX_EVENTS', 1), patch('server.urllib.request.urlopen', side_effect=self.transport):
            self.assertEqual(self.chat(trace_id)[0], 200)
        with self.app.store.connect() as db:
            self.assertLessEqual(db.execute('SELECT COUNT(*) FROM chat_trace_events WHERE trace_id=?',(trace_id,)).fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
