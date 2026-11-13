"""The shared bulletin UI uses the existing atomic save/sync API."""
import copy
from pathlib import Path
import tempfile
import unittest

import catalog
import server


class BulletinPostTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = server.Application(Path(self.directory.name), model=self.unexpected_model)
        self.sid, _ = self.app.store.get(None)

    def unexpected_model(self, _messages):
        self.fail("Bulletin operations must not invoke a model")

    def test_each_lab_exposes_two_reference_posts_without_mutating_records(self):
        before = self.app.store.get(self.sid)[1]
        for lab_id in (3, 7, 8):
            with self.subTest(lab=lab_id):
                result = self.app.lab_result(self.sid, lab_id, before)
                references = result["lab"]["reference_documents"]
                self.assertEqual(len(references), 2)
                self.assertTrue(all(doc["is_user_injected"] is False for doc in references))
                references[0]["content"] = "Changed only in the returned projection."
        self.assertEqual(self.app.store.get(self.sid)[1], before)
        self.assertNotEqual(catalog.LAB3_DOCUMENTS[0]["content"], "Changed only in the returned projection.")

    def test_one_save_adds_the_third_post_to_the_actual_retrieval_snapshot(self):
        for lab_id in (3, 7, 8):
            with self.subTest(lab=lab_id):
                before = self.app.store.get(self.sid)[1]
                originals = self.app.lab_result(self.sid, lab_id, before)["lab"]["reference_documents"]
                post = {"title": "Unique fixture bulletin", "content": "fixturebulletin observations for retrieval"}
                result = self.app.update_kb(self.sid, lab_id, {
                    "documents": [*originals, post], "sync": True, "use_kb": True})
                record = self.app.store.get(self.sid)[1]["labs"][str(lab_id)]
                self.assertEqual(len(record["documents"]), 3)
                self.assertEqual(record["documents"], record["synced_documents"])
                self.assertEqual(record["kb_revision"], record["synced_revision"])
                self.assertTrue(record["use_kb"])
                self.assertTrue(record["documents"][-1]["is_user_injected"])
                self.assertFalse(any(doc["is_user_injected"] for doc in record["documents"][:2]))
                retrieved = catalog.retrieve("fixturebulletin", record["synced_documents"])
                self.assertIn(record["documents"][-1], retrieved)
                self.assertFalse(result["lab"]["exploited"])
                self.assertIsNone(result["lab"]["flag"])
                self.assertEqual(result["summary"]["score"], 0)
                after = self.app.store.get(self.sid)[1]
                for other in before["labs"]:
                    if int(other) != lab_id:
                        self.assertEqual(after["labs"][other], before["labs"][other])

    def test_old_lab7_sessions_get_two_visible_references_without_a_data_migration(self):
        data = self.app.store.get(self.sid)[1]
        record = data["labs"]["7"]
        record["documents"] = copy.deepcopy(catalog.LAB7_DOCUMENTS[:1])
        record["synced_documents"] = copy.deepcopy(record["documents"])
        self.app.store.save(self.sid, data)
        before = self.app.store.get(self.sid)[1]
        result = self.app.lab_result(self.sid, 7, before)
        self.assertEqual(len(result["lab"]["reference_documents"]), 2)
        self.assertEqual(len(result["lab"]["documents"]), 1)
        self.assertEqual(self.app.store.get(self.sid)[1], before)

    def test_failed_save_preserves_posts_snapshot_and_score(self):
        before = self.app.store.get(self.sid)[1]
        for lab_id in (3, 7, 8):
            with self.subTest(lab=lab_id), self.assertRaises(server.APIError):
                self.app.update_kb(self.sid, lab_id, {
                    "documents": [{"title": "", "content": ""}], "sync": True, "use_kb": True})
        self.assertEqual(self.app.store.get(self.sid)[1], before)

    def test_posts_and_enabled_retrieval_remain_student_scoped(self):
        other, before = self.app.store.get(None)
        docs = [*copy.deepcopy(catalog.LAB7_DOCUMENTS), {"title": "Private fixture post", "content": "Student-specific note."}]
        self.app.update_kb(self.sid, 7, {"documents": docs, "sync": True, "use_kb": True})
        self.assertEqual(self.app.store.get(other)[1], before)


if __name__ == "__main__":
    unittest.main()
