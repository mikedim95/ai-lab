"""Lab 8 application lifecycle; benign fixtures and mocked model/evaluator only."""
import copy
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import catalog
import server


NOTE = {"id": "student-handover", "title": "Turbine notebook",
        "content": "Zephyr bearing inspection completed during the fictional maintenance handover."}
QUESTION = "Zephyr bearing"
REPLY = "The fictional inspection notebook records a completed bearing inspection."


class Lab8ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.calls = []
        self.app = server.Application(self.directory, model=self.model)
        self.sid, _ = self.app.store.get(None)

    def tearDown(self):
        self.temp.cleanup()

    def model(self, messages):
        self.calls.append(copy.deepcopy(messages))
        return REPLY

    def private(self, sid=None, app=None):
        return (app or self.app).store.get(sid or self.sid)[1]["labs"]["8"]

    def save(self, documents=None, *, sync=False, enabled=True, sid=None, app=None):
        return (app or self.app).update_kb(sid or self.sid, 8, {
            "documents": copy.deepcopy(documents if documents is not None else [NOTE]),
            "sync": sync, "use_kb": enabled})

    def chat(self, **body):
        return self.app.chat(self.sid, 8, {"message": QUESTION, **body})

    def raw_record(self, sid, record):
        # Simulate an on-disk pre-migration session, without invoking Store.get.
        with self.app.store.connect() as db:
            data = json.loads(db.execute("SELECT data FROM sessions WHERE sid=?", (sid,)).fetchone()[0])
            data["labs"]["8"] = copy.deepcopy(record)
            db.execute("UPDATE sessions SET data=? WHERE sid=?", (json.dumps(data), sid))

    def test_context_and_seeds_use_panoptis_at_every_difficulty(self):
        for level in (0, 1, 2):
            record = catalog.fresh_lab(defense_level=level, lab_id=8)
            system = catalog.build_messages(8, record, QUESTION, record["synced_documents"])[0]["content"]
            for term in ("PANOPTIS", "Boiler B-01", "Generator G-01", "maintenance handover", "internal simulation records", "read-only security auditor"):
                self.assertIn(term.lower(), system.lower())
            self.assertNotRegex(system.lower(), r"\b(shop|products|orders|customer|alice|apparel|drinkware|mug)\b")
            self.assertEqual(record["documents"], catalog.LAB8_DOCUMENTS)
            self.assertTrue(all(not doc["is_user_injected"] for doc in record["documents"]))

    def test_scenario_describes_equipment_documents_and_student_role(self):
        text = catalog.public_catalog()[7]["description"]
        for term in ("PANOPTIS", "Boiler B-01", "Generator G-01", "handover", "internal simulation records", "read-only security auditor"):
            self.assertIn(term.lower(), text.lower())

    def test_save_creates_id_without_syncing_or_trusting_client_provenance(self):
        note = {**NOTE, "id": None, "is_user_injected": False}
        original = copy.deepcopy(self.private()["synced_documents"])
        result = self.save([note])["lab"]
        self.assertTrue(result["documents"][0]["id"])
        self.assertTrue(result["documents"][0]["is_user_injected"])
        self.assertGreater(result["kb_revision"], result["synced_revision"])
        self.assertEqual(self.private()["synced_documents"], original)
        self.assertFalse(self.chat()["lab"]["last_result"]["kb_used"])

    def test_sync_copies_saved_version_and_preserves_retrieved_identifier(self):
        self.save(sync=True)
        result = self.chat()["lab"]["last_result"]
        self.assertEqual([s["id"] for s in result["sources"]], [NOTE["id"]])
        self.assertIn(NOTE["content"], self.calls[-1][0]["content"])
        self.assertEqual(self.private()["kb_revision"], self.private()["synced_revision"])

    def test_unsynced_edits_and_additions_are_excluded_from_bm25(self):
        self.save(sync=True)
        changed = {**NOTE, "content": "Aster revised inspection draft."}
        added = {**NOTE, "id": "new-draft", "title": "Aster draft", "content": "Aster draft awaiting review."}
        self.save([changed, added])
        record = self.private()
        self.assertEqual(catalog.retrieve("Aster", record["synced_documents"]), [])
        self.assertEqual(catalog.retrieve("Zephyr", record["synced_documents"])[0]["id"], NOTE["id"])
        self.chat()
        self.assertIn(NOTE["content"], self.calls[-1][0]["content"])
        self.assertNotIn(changed["content"], self.calls[-1][0]["content"])
        self.save([changed, added], sync=True)
        self.assertEqual(catalog.retrieve("Zephyr", self.private()["synced_documents"]), [])

    def test_unsynced_deletion_keeps_snapshot_until_synchronized(self):
        self.save(sync=True)
        self.save([])
        self.assertEqual(self.chat()["lab"]["last_result"]["sources"][0]["id"], NOTE["id"])
        self.save([], sync=True)
        self.assertFalse(self.chat()["lab"]["last_result"]["kb_used"])

    def test_sync_and_retrieval_return_independent_copies(self):
        self.save(sync=True)
        record = self.private()
        record["documents"][0]["content"] = "Changed saved object."
        self.assertEqual(record["synced_documents"][0]["content"], NOTE["content"])
        entries = catalog.retrieve("Zephyr", record["synced_documents"])
        entries[0]["content"] = "Changed retrieved object."
        self.assertEqual(self.private()["synced_documents"][0]["content"], NOTE["content"])

    def test_seed_provenance_is_derived_from_panoptis_originals(self):
        originals = [{**doc, "is_user_injected": True} for doc in catalog.LAB8_DOCUMENTS]
        result = self.save(originals, sync=True)["lab"]
        self.assertTrue(all(not doc["is_user_injected"] for doc in result["documents"]))
        edited = [{**originals[0], "content": originals[0]["content"] + " Student annotation.",
                   "is_user_injected": False}]
        result = self.save(edited, sync=True)["lab"]
        self.assertTrue(result["documents"][0]["is_user_injected"])

    def test_forged_body_documents_and_student_id_cannot_select_another_snapshot(self):
        other, _ = self.app.store.get(None)
        self.save(sync=True, sid=other)
        result = self.chat(student_id=other, documents=[NOTE], synced_documents=[NOTE], use_kb=True)
        self.assertFalse(result["lab"]["last_result"]["kb_used"])
        self.assertNotIn(NOTE["content"], self.calls[-1][0]["content"])

    def test_students_and_other_labs_are_unchanged_by_save_sync_and_reset(self):
        other, before_other = self.app.store.get(None)
        _, before = self.app.store.get(self.sid)
        self.save(sync=True)
        self.app.update_defense(self.sid, 8, {"level": 2})
        reset = self.app.reset(self.sid, 8)["lab"]
        self.assertEqual(reset["documents"], catalog.LAB8_DOCUMENTS)
        self.assertFalse(reset["use_kb"])
        self.assertEqual(reset["defense_level"], 2)
        self.assertEqual(self.app.store.get(other)[1], before_other)
        _, after = self.app.store.get(self.sid)
        for key in before["labs"]:
            if key != "8":
                self.assertEqual(after["labs"][key], before["labs"][key])

    def test_difficulty_and_both_document_versions_persist_across_restart(self):
        self.save(sync=True)
        self.save([{**NOTE, "content": "Saved handover revision pending synchronization."}])
        before = self.private()
        self.app.update_defense(self.sid, 8, {"level": 1})
        reopened = server.Application(self.directory, model=self.model)
        after = self.private(app=reopened)
        for key in ("documents", "synced_documents", "kb_revision", "synced_revision", "use_kb"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after["defense_level"], 1)
        self.app = reopened
        result = self.chat(defense_level=0)["lab"]["last_result"]
        self.assertEqual(result["defense_level"], 1)

    def test_request_ids_are_unique_and_match_the_committed_response(self):
        responses = [self.chat() for _ in range(3)]
        ids = [response["request_id"] for response in responses]
        self.assertEqual(len(set(ids)), 3)
        for response in responses:
            self.assertEqual(response["request_id"], response["lab"]["last_result"]["request_id"])
        self.assertEqual(self.private()["last_result"]["request_id"], ids[-1])

    def test_every_lab8_mutation_advances_persisted_state_revision(self):
        results = [self.save(), self.save(sync=True), self.chat(),
                   self.app.update_defense(self.sid, 8, {"level": 1}), self.app.reset(self.sid, 8)]
        versions = [tuple(r["lab"]["state_version"]) for r in results]
        self.assertEqual(versions, sorted(set(versions)))
        self.assertEqual(len({r["request_id"] for r in results}), len(results))

    def test_changed_snapshot_during_generation_rejects_old_reply(self):
        self.save(sync=True)
        def model(_):
            self.save([{**NOTE, "content": "Revised synchronized maintenance handover."}], sync=True)
            return REPLY
        self.app.model = model
        with self.assertRaises(server.APIError) as error:
            self.chat()
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.private()["messages"], [])
        self.assertIsNone(self.private()["last_result"])
        self.assertIn("Revised", self.private()["synced_documents"][0]["content"])

    def test_unsynced_save_during_generation_cannot_be_overwritten(self):
        self.save(sync=True)
        self.app.model = lambda _: (self.save([{**NOTE, "title": "Latest saved draft"}]), REPLY)[1]
        with self.assertRaises(server.APIError) as error:
            self.chat()
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.private()["documents"][0]["title"], "Latest saved draft")
        self.assertEqual(self.private()["synced_documents"][0]["title"], NOTE["title"])

    def test_reset_and_difficulty_change_reject_previous_attempt_reply(self):
        for change in (lambda: self.app.reset(self.sid, 8),
                       lambda: self.app.update_defense(self.sid, 8, {"level": 2})):
            self.app.model = lambda _: (change(), REPLY)[1]
            with self.assertRaises(server.APIError) as error:
                self.chat()
            self.assertEqual(error.exception.status, 409)
            self.assertEqual(self.private()["messages"], [])

    def test_concurrent_applications_cannot_replace_a_newer_reply(self):
        other = server.Application(self.directory, model=lambda _: "Newer maintenance response.")
        newer = []
        def model(_):
            newer.append(other.chat(self.sid, 8, {"message": "Explain the handover."}))
            return REPLY
        self.app.model = model
        with self.assertRaises(server.APIError) as error:
            self.chat()
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.private()["last_result"]["request_id"], newer[0]["request_id"])
        self.assertEqual(self.private()["messages"][-1]["content"], "Newer maintenance response.")

    def test_transaction_rejects_stale_commit_and_preserves_other_lab_changes(self):
        snapshot = self.private()
        candidate = copy.deepcopy(snapshot)
        self.app.update_defense(self.sid, 7, {"level": 1})
        self.app.store.save_lab8(self.sid, candidate, snapshot)
        self.assertEqual(self.app.store.get(self.sid)[1]["labs"]["7"]["defense_level"], 1)
        with self.assertRaises(server.APIError) as error:
            self.app.store.save_lab8(self.sid, candidate, snapshot)
        self.assertEqual(error.exception.status, 409)

    def test_stale_shared_store_save_cannot_restore_older_lab8_state(self):
        _, old = self.app.store.get(self.sid)
        newer = self.save(sync=True)["lab"]
        old["labs"]["7"]["use_kb"] = True
        self.app.store.save(self.sid, old)
        self.assertEqual(self.private()["documents"], newer["documents"])
        self.assertEqual(self.app.store.get(self.sid)[1]["labs"]["7"]["use_kb"], True)

    def test_legacy_seeds_migrate_each_copy_without_synchronizing_student_edits(self):
        for saved_legacy, synced_legacy in ((True, True), (True, False), (False, True), (False, False)):
            sid, _ = self.app.store.get(None)
            record = catalog.fresh_lab(lab_id=8)
            record.update(documents=copy.deepcopy(catalog.DEFAULT_DOCUMENTS if saved_legacy else [NOTE]),
                          synced_documents=copy.deepcopy(catalog.DEFAULT_DOCUMENTS if synced_legacy else [NOTE]),
                          kb_revision=9, synced_revision=6, defense_level=1)
            self.raw_record(sid, record)
            migrated = self.private(sid)
            self.assertEqual(migrated["documents"], catalog.LAB8_DOCUMENTS if saved_legacy else [NOTE])
            self.assertEqual(migrated["synced_documents"], catalog.LAB8_DOCUMENTS if synced_legacy else [NOTE])
            self.assertEqual((migrated["kb_revision"], migrated["synced_revision"]), (9, 6))
            self.assertEqual(migrated["defense_level"], 1)
            self.assertEqual(self.private(sid), migrated)

    def test_edited_legacy_seed_is_preserved_verbatim(self):
        record = self.private()
        edited = {**catalog.DEFAULT_DOCUMENTS[0], "content": catalog.DEFAULT_DOCUMENTS[0]["content"] + " Student note."}
        record.update(documents=[edited], synced_documents=copy.deepcopy(catalog.DEFAULT_DOCUMENTS))
        self.raw_record(self.sid, record)
        self.assertEqual(self.private()["documents"], [edited])
        self.assertEqual(self.private()["synced_documents"], catalog.LAB8_DOCUMENTS)

    def test_lab3_and_lab8_legacy_seed_migrations_can_run_together(self):
        with self.app.store.connect() as db:
            data = json.loads(db.execute("SELECT data FROM sessions WHERE sid=?", (self.sid,)).fetchone()[0])
            for lab_id in (3, 8):
                for key in ("documents", "synced_documents"):
                    data["labs"][str(lab_id)][key] = copy.deepcopy(catalog.DEFAULT_DOCUMENTS)
            db.execute("UPDATE sessions SET data=? WHERE sid=?", (json.dumps(data), self.sid))
        _, migrated = self.app.store.get(self.sid)
        self.assertEqual(migrated["labs"]["3"]["documents"], catalog.LAB3_DOCUMENTS)
        self.assertEqual(migrated["labs"]["8"]["documents"], catalog.LAB8_DOCUMENTS)

    def test_concurrent_legacy_migration_does_not_advance_revision_twice(self):
        other = server.Application(self.directory, model=self.model)
        record = self.private()
        record.update(documents=copy.deepcopy(catalog.DEFAULT_DOCUMENTS),
                      synced_documents=copy.deepcopy(catalog.DEFAULT_DOCUMENTS))
        self.raw_record(self.sid, record)
        parse, raced = json.loads, []
        def interleaved_parse(value, *args, **kwargs):
            parsed = parse(value, *args, **kwargs)
            if not raced and isinstance(parsed, dict) and "labs" in parsed:
                raced.append(True)
                other.store.get(self.sid)
            return parsed
        with patch.object(server.json, "loads", side_effect=interleaved_parse):
            migrated = self.private()
        self.assertEqual(migrated["lab8_state_revision"], record["lab8_state_revision"] + 1)
        self.assertEqual(migrated["documents"], catalog.LAB8_DOCUMENTS)

    def test_other_lab_legacy_migration_cannot_roll_back_newer_lab8_work(self):
        with self.app.store.connect() as db:
            data = json.loads(db.execute("SELECT data FROM sessions WHERE sid=?", (self.sid,)).fetchone()[0])
            data["labs"]["3"]["documents"] = copy.deepcopy(catalog.DEFAULT_DOCUMENTS)
            db.execute("UPDATE sessions SET data=? WHERE sid=?", (json.dumps(data), self.sid))
        duplicate, raced = copy.deepcopy, []
        def interleaved_copy(value, *args, **kwargs):
            if not raced and value is catalog.LAB3_DOCUMENTS[0]:
                raced.append(True)
                self.save(sync=True)
            return duplicate(value, *args, **kwargs)
        with patch.object(server.copy, "deepcopy", side_effect=interleaved_copy):
            _, migrated = self.app.store.get(self.sid)
        self.assertTrue(raced)
        self.assertEqual(migrated["labs"]["8"]["documents"][0]["id"], NOTE["id"])
        self.assertEqual(self.private()["synced_documents"][0]["id"], NOTE["id"])

    def test_same_document_identifier_retrieves_only_its_student_session_content(self):
        other, _ = self.app.store.get(None)
        self.save(sync=True)
        different = {**NOTE, "content": "Zephyr bearing review belongs to the second student notebook."}
        self.save([different], sid=other, sync=True)
        self.chat()
        self.assertIn(NOTE["content"], self.calls[-1][0]["content"])
        self.assertNotIn(different["content"], self.calls[-1][0]["content"])
        self.app.chat(other, 8, {"message": QUESTION})
        self.assertIn(different["content"], self.calls[-1][0]["content"])
        self.assertNotIn(NOTE["content"], self.calls[-1][0]["content"])

    def test_resetting_other_labs_keeps_lab8_student_work(self):
        self.save(sync=True)
        before = self.private()
        for lab_id in (1, 2, 3, 4, 5, 6, 7, 9):
            self.app.reset(self.sid, lab_id)
            self.assertEqual(self.private(), before)

    def test_model_failure_cannot_replace_current_response(self):
        self.chat()
        before = self.private()
        self.app.model = lambda _: (_ for _ in ()).throw(server.APIError(503, "Fixture transport unavailable."))
        with self.assertRaises(server.APIError):
            self.chat()
        after = self.private()
        for key in ("messages", "last_result", "documents", "synced_documents", "completed"):
            self.assertEqual(after[key], before[key])
        self.assertFalse(after["lab8_verification_receipts"][-1]["verification_outcome"])
        self.assertIsNone(after["lab8_verification_receipts"][-1]["displayed_response_sha256"])

    def test_refusal_wording_is_panoptis_without_changing_the_block_decision(self):
        self.app.update_defense(self.sid, 8, {"level": 1})
        with patch.object(server, "defend_input", return_value=(QUESTION, "fixture_block")):
            result = self.chat()["lab"]
        self.assertEqual(result["last_result"]["defense_reason"], "fixture_block")
        self.assertTrue(result["last_result"]["defense_blocked"])
        self.assertFalse(result["last_result"]["triggered"])
        self.assertIn("PANOPTIS", result["messages"][-1]["content"])
        self.assertNotIn("shop", result["messages"][-1]["content"])
        self.assertEqual(self.calls, [])

    def test_http_cookie_isolation_and_unique_request_ids_for_success_and_failure(self):
        self.save(sync=True)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.app))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        def request(body, sid=None):
            connection = http.client.HTTPConnection(*httpd.server_address)
            headers = {"Content-Type": "application/json"}
            if sid:
                headers["Cookie"] = "nine_sid=" + sid
            connection.request("POST", "/api/labs/8/chat", json.dumps(body), headers)
            reply = connection.getresponse()
            status, data = reply.status, json.loads(reply.read())
            connection.close()
            return status, data
        try:
            status, first = request({"message": QUESTION}, self.sid)
            self.assertEqual(status, 200)
            self.assertEqual(first["request_id"], first["lab"]["last_result"]["request_id"])
            self.assertEqual(first["lab"]["last_result"]["sources"][0]["id"], NOTE["id"])
            status, second = request({"message": QUESTION, "use_kb": True, "student_id": self.sid})
            self.assertEqual(status, 200)
            self.assertFalse(second["lab"]["last_result"]["kb_used"])
            status, failed = request({"message": ""}, self.sid)
            self.assertEqual(status, 400)
            self.assertIn("request_id", failed)
            self.assertEqual(len({first["request_id"], second["request_id"], failed["request_id"]}), 3)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join()

    def test_private_evidence_binds_current_request_and_actual_retrieval(self):
        self.save(sync=True)
        result = self.chat()
        receipt = self.private()["lab8_verification_receipts"][-1]
        self.assertEqual(receipt["student_id"], self.sid)
        self.assertEqual(receipt["message_sha256"], server._lab3_hash(QUESTION))
        self.assertEqual([entry["id"] for entry in receipt["retrieved_documents"]], [NOTE["id"]])
        self.assertEqual(receipt["defense_level"], 0)
        self.assertFalse(result["lab"]["last_result"]["triggered"])
        self.assertIsNone(result["lab"]["flag"])

    def test_legacy_evaluator_boolean_cannot_replace_production_evidence(self):
        with patch.object(server, "check_exploit", return_value=True):
            response = self.chat()
        self.assertFalse(response["lab"]["exploited"])
        self.assertEqual(response["summary"]["score"], 0)
        self.assertIsNone(response["lab"]["flag"])
        self.assertEqual(self.private()["lab8_verification_receipts"][-1]["generation_source"], "test_fixture")

    def test_other_lab_defaults_and_document_lifecycles_are_preserved(self):
        for lab_id, originals in ((3, catalog.LAB3_DOCUMENTS), (7, catalog.LAB7_DOCUMENTS)):
            self.assertEqual(catalog.fresh_lab(lab_id=lab_id)["documents"], originals)
            self.app.update_kb(self.sid, lab_id, {"documents": [NOTE], "sync": False})
            record = self.app.store.get(self.sid)[1]["labs"][str(lab_id)]
            self.assertEqual(record["synced_documents"], originals)
            self.app.update_kb(self.sid, lab_id, {"documents": [NOTE], "sync": True})
            record = self.app.store.get(self.sid)[1]["labs"][str(lab_id)]
            self.assertEqual(catalog.retrieve("Zephyr", record["synced_documents"])[0]["id"], NOTE["id"])
        for lab_id in (1, 2, 4, 5, 6, 9):
            self.assertEqual(catalog.fresh_lab(lab_id=lab_id)["documents"], catalog.DEFAULT_DOCUMENTS)


if __name__ == "__main__":
    unittest.main()
