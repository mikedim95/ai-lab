"""Safe trace identity projection; no instructor identity is invented."""
import http.client
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import catalog
import server
from test_app import LAB6_IDENTITY_REQUEST
from test_lab9_evidence import DISCLOSURE, QUESTIONS, REFUSALS


class TraceProjectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.app = server.Application(self.directory)
        self.sid, _ = self.app.store.get(None)
        self.other, _ = self.app.store.get(None)
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.make_handler(self.app))
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    def request(self, path, body=None, *, sid=None, headers=None):
        connection = http.client.HTTPConnection(*self.httpd.server_address, timeout=5)
        values = {'Content-Type': 'application/json'}
        if sid is not False:
            values['Cookie'] = 'nine_sid=' + (sid or self.sid)
        values.update(headers or {})
        payload = json.dumps(body).encode() if body is not None else None
        connection.request('POST' if payload is not None else 'GET', path, payload, values)
        response = connection.getresponse()
        content = response.read().decode()
        status = response.status
        mime = response.getheader('Content-Type', '')
        connection.close()
        if mime.startswith('application/json'):
            return status, json.loads(content)
        return status, [json.loads(line[6:]) for line in content.splitlines() if line.startswith('data: ')]

    def reserve(self, body=None, lab=9):
        status, result = self.request(f'/api/labs/{lab}/traces', {} if body is None else body)
        self.assertEqual(status, 201)
        return result

    def complete(self, lab=9, message='Explain public maintenance procedures.', reply='Public maintenance procedures remain available.'):
        reserved = self.reserve(lab=lab)
        def transport(request, timeout):
            return io.BytesIO(json.dumps({'message': {'content': reply}}).encode())
        with patch('server.urllib.request.urlopen', side_effect=transport):
            status, _ = self.request(f'/api/labs/{lab}/chat', {'message': message},
                                     headers={'X-Trace-Id': reserved['trace_id']})
        self.assertEqual(status, 200)
        return reserved

    def test_reservation_metadata_comes_from_the_saved_trace_not_client_fields(self):
        self.app.update_defense(self.sid, 9, {'level': 2})
        reserved = self.reserve({'lab_id': 1, 'difficulty': 0, 'attempt': 999, 'role': 'instructor'})
        self.assertEqual(reserved['metadata'], {'lab_id': 9, 'difficulty': 2, 'attempt': 2})
        with self.app.store.connect() as db:
            row = db.execute('SELECT lab_id,difficulty,attempt FROM chat_traces WHERE trace_id=?',
                             (reserved['trace_id'],)).fetchone()
        self.assertEqual(tuple(reserved['metadata'][key] for key in ('lab_id', 'difficulty', 'attempt')), row)
        self.assertEqual(set(reserved), {'trace_id', 'events_url', 'metadata'})

    def test_every_durable_stage_and_terminal_carries_original_attempt_metadata(self):
        self.app.update_defense(self.sid, 9, {'level': 1})
        reserved = self.complete()
        original = reserved['metadata']
        self.app.reset(self.sid, 9)
        self.app.update_defense(self.sid, 9, {'level': 2})
        status, events = self.request(reserved['events_url'])
        self.assertEqual(status, 200)
        self.assertTrue(events)
        self.assertEqual(events[-1]['stage'], 'trace')
        self.assertTrue(all(event['persisted'] is True for event in events))
        self.assertTrue(all(event['metadata'] == original for event in events))
        self.assertEqual(original, {'lab_id': 9, 'difficulty': 1, 'attempt': 2})
        self.assertNotEqual(original['attempt'], self.app.store.get(self.sid)[1]['labs']['9']['attempt'])

    def test_replayed_metadata_is_identical_to_initial_delivery(self):
        reserved = self.complete()
        _, events = self.request(reserved['events_url'])
        cursor = events[4]['event_id']
        status, replay = self.request(reserved['events_url'] + '?after=' + str(cursor))
        self.assertEqual(status, 200)
        self.assertEqual(replay, events[5:])
        self.assertTrue(all(event['metadata'] == reserved['metadata'] for event in replay))

    def test_instructor_view_is_denied_by_backend_even_with_spoofed_roles(self):
        reserved = self.complete()
        for query in ('view=instructor', 'view=INSTRUCTOR', 'view=admin', 'view=student&view=instructor'):
            with self.subTest(query=query):
                status, result = self.request(reserved['events_url'] + '?' + query,
                    headers={'X-Role': 'instructor', 'X-User-Role': 'admin', 'X-Instructor': 'true'})
                self.assertEqual(status, 403)
                self.assertEqual(set(result), {'error'})
        self.assertEqual(self.app.store.trace_streams.active, {})

    def test_role_hints_without_privileged_view_return_only_safe_student_fields(self):
        reserved = self.complete()
        status, events = self.request(reserved['events_url'] + '?role=instructor&instructor=true',
                                      headers={'X-Role': 'instructor', 'X-Instructor': 'true'})
        self.assertEqual(status, 200)
        allowed = {'trace_id', 'event_id', 'span_id', 'stage', 'status', 'timestamp',
                   'duration_ms', 'reason_code', 'persisted', 'metadata'}
        for event in events:
            self.assertEqual(set(event), allowed | ({'outcome'} if event['stage'] == 'trace' else set()))
            self.assertEqual(set(event['metadata']), {'lab_id', 'difficulty', 'attempt'})
        serialized = json.dumps(events)
        self.assertNotIn(self.sid, serialized)
        self.assertNotIn('student_id', serialized)
        self.assertNotIn('backend_control', serialized)
        self.assertNotIn('defense_reason', serialized)
        self.assertNotIn('system_prompt', serialized)
        self.assertNotIn('receipt', serialized)
        for field in catalog.LAB9_PROTECTED_FIELDS:
            self.assertNotIn(field['value'], serialized)

    def test_owner_authorization_is_preserved_for_metadata(self):
        reserved = self.complete()
        status, result = self.request(reserved['events_url'], sid=self.other)
        self.assertEqual(status, 404)
        self.assertNotIn('metadata', result)
        self.assertEqual(self.request(reserved['events_url'], sid=False)[0], 401)

    def test_invalid_stored_metadata_fails_projection_without_leaking_contents(self):
        reserved = self.complete()
        with self.app.store.connect() as db:
            db.execute('UPDATE chat_traces SET difficulty=? WHERE trace_id=?',
                       ('private-metadata-marker', reserved['trace_id']))
        status, result = self.request(reserved['events_url'])
        self.assertEqual(status, 503)
        self.assertNotIn('private-metadata-marker', json.dumps(result))
        self.assertNotIn('metadata', result)

    def test_metadata_projection_does_not_write_existing_student_records(self):
        reserved = self.complete()
        with self.app.store.connect() as db:
            before = db.execute('SELECT * FROM sessions ORDER BY sid').fetchall()
        self.assertEqual(self.request(reserved['events_url'])[0], 200)
        with self.app.store.connect() as db:
            after = db.execute('SELECT * FROM sessions ORDER BY sid').fetchall()
        self.assertEqual(before, after)

    def test_projection_accepts_only_small_integer_identity_fields(self):
        for changed in ({'lab_id': 10}, {'difficulty': 3}, {'attempt': 0}, {'difficulty': True},
                        {'attempt': 1.5}, {'attempt': 9007199254740992}):
            with self.subTest(field=tuple(changed)):
                with self.assertRaises(RuntimeError):
                    server._trace_metadata({'lab_id': 9, 'difficulty': 0, 'attempt': 1, **changed})

    def test_successful_current_verification_projects_no_exploit_outcome(self):
        reserved = self.complete()
        _, events = self.request(reserved['events_url'])
        self.assertEqual(events[-1]['outcome'], 'NO_EXPLOIT')
        self.assertTrue(all('outcome' not in event for event in events[:-1]))
        with self.app.store.connect() as db:
            self.assertEqual(db.execute('SELECT final_outcome FROM chat_traces WHERE trace_id=?',
                                       (reserved['trace_id'],)).fetchone()[0], 'NO_EXPLOIT')

    def test_genuine_refusal_outcome_comes_only_from_existing_receipt(self):
        reserved = self.complete(message=QUESTIONS[0], reply=REFUSALS[0])
        _, events = self.request(reserved['events_url'])
        self.assertEqual(events[-1]['outcome'], 'MODEL_REFUSED')
        self.assertIn('model_refusal', [event['reason_code'] for event in events])

    def test_existing_completion_cannot_verify_a_new_attempt_outcome(self):
        reserved = None
        for message, reply in zip(QUESTIONS, (*REFUSALS, DISCLOSURE)):
            reserved = self.complete(message=message, reply=reply)
        _, events = self.request(reserved['events_url'])
        self.assertEqual(events[-1]['outcome'], 'EXPLOIT_VERIFIED')
        record = self.app.store.get(self.sid)[1]['labs']['9']
        original_flag = self.app.public_lab(self.sid, 9, record)['flag']
        completed = self.app.submit_flag(self.sid, 9, {'flag': original_flag})
        self.assertEqual(completed['summary']['score'], 500)
        self.app.reset(self.sid, 9)
        fresh = self.complete()
        _, events = self.request(fresh['events_url'])
        self.assertEqual(events[-1]['outcome'], 'NO_EXPLOIT')
        state = self.app.store.get(self.sid)[1]
        self.assertTrue(state['labs']['9']['completed'])
        self.assertEqual(self.app.summary(state)['score'], 500)

    def test_actual_input_policy_rejection_projects_backend_blocked(self):
        self.app.update_defense(self.sid, 6, {'level': 1})
        reserved = self.complete(lab=6, message=LAB6_IDENTITY_REQUEST)
        _, events = self.request(reserved['events_url'])
        self.assertEqual(events[-1]['outcome'], 'BACKEND_BLOCKED')
        self.assertIn('input_policy_rejected', [event['reason_code'] for event in events])

    def test_partial_rag_source_rejection_does_not_claim_whole_request_blocked(self):
        self.app.update_kb(self.sid, 3, {'documents': [{'id': 'projection-source', 'title': 'Boiler inspection',
            'content': 'Boiler inspection maintenance is scheduled for the next review.'}], 'sync': True, 'use_kb': True})
        self.app.update_defense(self.sid, 3, {'level': 2})
        reserved = self.complete(lab=3, message='What boiler inspection maintenance is scheduled?')
        _, events = self.request(reserved['events_url'])
        self.assertTrue(any(event['stage'] == 'rag' and event['status'] == 'blocked' for event in events))
        self.assertTrue(any(event['stage'] == 'ollama' and event['status'] == 'passed' for event in events))
        self.assertEqual(events[-1]['outcome'], 'NO_EXPLOIT')

    def test_recovered_lab1_model_error_preserves_verified_request_outcome(self):
        reserved = self.reserve(lab=1)
        replies = iter([{'message': {'content': '', 'tool_calls': [{'function': {'name': 'set_boiler_inputs',
            'arguments': {'water_in_pct': 48, 'fuel_in_pct': 50}}}]}}, OSError('private transport failure')])
        def transport(request, timeout):
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return io.BytesIO(json.dumps(reply).encode())
        with patch('server.urllib.request.urlopen', side_effect=transport):
            status, result = self.request('/api/labs/1/chat', {'message': 'Describe the public monitoring readings.'},
                                         headers={'X-Trace-Id': reserved['trace_id']})
        self.assertEqual(status, 200)
        self.assertTrue(result['lab']['last_result']['triggered'])
        self.assertEqual(result['summary']['score'], 0)
        _, events = self.request(reserved['events_url'])
        self.assertTrue(any(event['stage'] == 'ollama' and event['status'] == 'error' for event in events))
        self.assertEqual(events[-1]['status'], 'error')  # Existing Task 2 contract retained.
        self.assertEqual(events[-1]['outcome'], 'EXPLOIT_VERIFIED')

    def test_unrecovered_request_failure_projects_error(self):
        reserved = self.reserve()
        with patch('server.urllib.request.urlopen', side_effect=OSError('private transport failure')):
            status, _ = self.request('/api/labs/9/chat', {'message': 'Describe public maintenance.'},
                                    headers={'X-Trace-Id': reserved['trace_id']})
        self.assertEqual(status, 503)
        _, events = self.request(reserved['events_url'])
        self.assertEqual(events[-1]['outcome'], 'ERROR')

    def test_unavailable_trace_does_not_invent_a_request_outcome(self):
        reserved = self.reserve()
        with patch.object(self.app.store, 'append_trace_event', side_effect=sqlite3.OperationalError('unavailable')):
            with patch('server.urllib.request.urlopen', return_value=io.BytesIO(json.dumps(
                    {'message': {'content': 'Public maintenance remains available.'}}).encode())):
                status, _ = self.request('/api/labs/9/chat', {'message': 'Describe public maintenance.'},
                                        headers={'X-Trace-Id': reserved['trace_id']})
        self.assertEqual(status, 200)
        _, events = self.request(reserved['events_url'])
        self.assertIsNone(events[-1]['outcome'])
        self.assertEqual(events[-1]['reason_code'], 'trace_unavailable')

    def test_unrecognized_stored_outcome_is_not_exposed(self):
        reserved = self.complete()
        with self.app.store.connect() as db:
            db.execute('UPDATE chat_traces SET final_outcome=? WHERE trace_id=?',
                       ('private-outcome-marker', reserved['trace_id']))
        status, result = self.request(reserved['events_url'])
        self.assertEqual(status, 503)
        self.assertNotIn('private-outcome-marker', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
