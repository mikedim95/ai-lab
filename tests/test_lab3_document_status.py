"""Read-only Lab 3 presentation metadata, using isolated session stores."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import catalog
import server
from test_app import LAB3_ADOPTED_REPLY, LAB3_BULLETIN, LAB3_QUESTION


class Lab3DocumentStatusTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.app = server.Application(self.directory)
        self.sid, _ = self.app.store.get(None)

    def save(self, *, sync=False, use_kb=False, documents=None):
        documents = [*catalog.LAB3_DOCUMENTS, LAB3_BULLETIN] if documents is None else documents
        return self.app.update_kb(self.sid, 3, {"documents": copy.deepcopy(documents), "sync": sync, "use_kb": use_kb})["lab"]

    def public(self, *, app=None, sid=None):
        app = app or self.app
        sid = sid or self.sid
        _, data = app.store.get(sid)
        return app.public_lab(sid, 3, data["labs"]["3"])

    def ask(self, reply="The PT-B01 inspection interval remains 12 simulated cycles."):
        with patch("server.urllib.request.urlopen", return_value=io.BytesIO(json.dumps({"message": {"content": reply}}).encode())):
            return self.app.chat(self.sid, 3, {"message": LAB3_QUESTION})["lab"]

    def test_fresh_original_documents_are_synced_with_retrieval_disabled(self):
        lab = self.public()
        self.assertFalse(lab["use_kb"])
        self.assertEqual(lab["kb_revision"], lab["synced_revision"])
        for document in catalog.LAB3_DOCUMENTS:
            self.assertEqual(lab["document_sync"][document["id"]], {"saved": True, "synced": True, "matches_saved": True})
        self.assertNotIn(LAB3_BULLETIN["id"], lab["document_sync"])

    def test_save_updates_saved_version_without_changing_the_synced_snapshot(self):
        lab = self.save()
        self.assertGreater(lab["kb_revision"], lab["synced_revision"])
        self.assertEqual(lab["document_sync"][LAB3_BULLETIN["id"]], {"saved": True, "synced": False, "matches_saved": False})
        private = self.app.store.get(self.sid)[1]["labs"]["3"]
        self.assertEqual(private["synced_documents"], catalog.LAB3_DOCUMENTS)
        for original in catalog.LAB3_DOCUMENTS:
            self.assertTrue(lab["document_sync"][original["id"]]["matches_saved"])

    def test_sync_activates_saved_version_and_enables_retrieval(self):
        self.save()
        lab = self.save(sync=True, use_kb=True)
        self.assertTrue(lab["use_kb"])
        self.assertEqual(lab["kb_revision"], lab["synced_revision"])
        self.assertEqual(lab["document_sync"][LAB3_BULLETIN["id"]], {"saved": True, "synced": True, "matches_saved": True})

    def test_later_save_identifies_an_older_retrievable_synced_version(self):
        self.save(sync=True, use_kb=True)
        documents = [*catalog.LAB3_DOCUMENTS, {**LAB3_BULLETIN, "content": LAB3_BULLETIN["content"] + " Saved draft note."}]
        lab = self.save(documents=documents, use_kb=True)
        self.assertEqual(lab["document_sync"][LAB3_BULLETIN["id"]], {"saved": True, "synced": True, "matches_saved": False})
        private = self.app.store.get(self.sid)[1]["labs"]["3"]
        old = next(doc for doc in private["synced_documents"] if doc["id"] == LAB3_BULLETIN["id"])
        self.assertEqual(old["content"], LAB3_BULLETIN["content"])
        self.assertIn(old, catalog.retrieve(LAB3_QUESTION, private["synced_documents"]))

    def test_disabling_retrieval_preserves_the_synced_version(self):
        self.save(sync=True, use_kb=True)
        lab = self.save(use_kb=False)
        self.assertFalse(lab["use_kb"])
        self.assertTrue(lab["document_sync"][LAB3_BULLETIN["id"]]["matches_saved"])

    def test_backend_reopen_and_refresh_restore_document_status(self):
        self.save(sync=True, use_kb=True)
        self.save(documents=[*catalog.LAB3_DOCUMENTS, {**LAB3_BULLETIN, "title": "Saved title revision"}], use_kb=True)
        before = self.public()
        after = self.public(app=server.Application(self.directory))
        for key in ["document_sync", "kb_revision", "synced_revision", "use_kb"]:
            self.assertEqual(after[key], before[key])

    def test_status_is_isolated_between_students(self):
        self.save(sync=True, use_kb=True)
        other, _ = self.app.store.get(None)
        second = self.public(sid=other)
        self.assertNotIn(LAB3_BULLETIN["id"], second["document_sync"])
        self.assertFalse(second["use_kb"])
        self.assertTrue(self.public()["use_kb"])

    def test_removed_saved_document_still_reports_its_retained_synced_version(self):
        self.save(sync=True, use_kb=True)
        lab = self.save(documents=catalog.LAB3_DOCUMENTS, use_kb=True)
        self.assertEqual(lab["document_sync"][LAB3_BULLETIN["id"]], {"saved": False, "synced": True, "matches_saved": False})

    def test_retrieval_metadata_is_independent_of_model_adoption(self):
        self.save(sync=True, use_kb=True)
        lab = self.ask()
        self.assertFalse(lab["last_result"]["triggered"])
        self.assertTrue(any(source["injected"] for source in lab["last_result"]["sources"]))
        self.assertTrue(lab["last_result"]["retrieval_snapshot_current"])

    def test_last_reply_uses_current_snapshot_after_save_but_earlier_snapshot_after_sync(self):
        self.save(sync=True, use_kb=True)
        self.ask()
        revised = [*catalog.LAB3_DOCUMENTS, {**LAB3_BULLETIN, "content": LAB3_BULLETIN["content"] + " Saved draft note."}]
        lab = self.save(documents=revised, use_kb=True)
        self.assertTrue(lab["last_result"]["retrieval_snapshot_current"])
        lab = self.save(documents=revised, sync=True, use_kb=True)
        self.assertFalse(lab["last_result"]["retrieval_snapshot_current"])

    def test_a_failed_generation_does_not_replace_last_displayed_reply_metadata(self):
        self.save(sync=True, use_kb=True)
        self.ask()
        with patch("server.urllib.request.urlopen", side_effect=OSError("fixture failure")):
            with self.assertRaises(server.APIError):
                self.app.chat(self.sid, 3, {"message": LAB3_QUESTION})
        self.assertTrue(self.public()["last_result"]["retrieval_snapshot_current"])

    def test_legacy_reply_without_receipt_reports_unknown_snapshot(self):
        self.save(sync=True, use_kb=True)
        self.ask()
        record = self.app.store.get(self.sid)[1]["labs"]["3"]
        record.pop("lab3_verification_receipts")
        self.assertIsNone(self.app.public_lab(self.sid, 3, record)["last_result"]["retrieval_snapshot_current"])

    def test_projection_does_not_modify_private_state_or_expose_receipts_and_hashes(self):
        self.save(sync=True, use_kb=True)
        self.ask(LAB3_ADOPTED_REPLY)
        record = self.app.store.get(self.sid)[1]["labs"]["3"]
        before = copy.deepcopy(record)
        public = self.app.public_lab(self.sid, 3, record)
        self.assertEqual(record, before)
        encoded = json.dumps(public)
        self.assertNotIn("synced_documents", public)
        self.assertNotIn("lab3_verification_receipts", encoded)
        self.assertNotIn("sha256", encoded)
        self.assertEqual(set(public["document_sync"][LAB3_BULLETIN["id"]]), {"saved", "synced", "matches_saved"})

    def test_presentation_fields_are_added_only_to_lab3(self):
        data = self.app.store.get(self.sid)[1]
        for lab_id in [1, 2, 4, 5, 6, 7, 8, 9]:
            with self.subTest(lab=lab_id):
                self.assertNotIn("document_sync", self.app.public_lab(self.sid, lab_id, data["labs"][str(lab_id)]))


if __name__ == "__main__":
    unittest.main()
