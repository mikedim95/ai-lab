"""Explicit restart revokes only the selected lab's award in isolated stores."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import catalog
import server


class LabRestartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.app = server.Application(self.directory)

    def completed_student(self):
        sid, data = self.app.store.get(None)
        # Legacy completed rows are an existing supported fixture format. No
        # model calls, objective changes, or production student data are used.
        for lab_id, record in data["labs"].items():
            record.update(completed=True, exploited=True, defense_level=int(lab_id) % 3)
        with self.app.store.connect() as db:
            db.execute("UPDATE sessions SET data=? WHERE sid=?", (json.dumps(data), sid))
        return sid, copy.deepcopy(data)

    def assert_cleared(self, response, lab_id):
        self.assertFalse(response["lab"]["completed"])
        self.assertFalse(response["lab"]["exploited"])
        self.assertIsNone(response["lab"]["flag"])
        self.assertEqual(response["lab"]["messages"], [])
        self.assertNotIn(lab_id, response["summary"]["completed"])
        self.assertEqual(response["summary"]["score"],
                         catalog.TOTAL_POINTS - catalog.CATALOG[lab_id - 1]["points"])

    def test_each_restart_revokes_flag_and_points_and_preserves_other_students_and_labs(self):
        other_sid, other_before = self.completed_student()
        for lab_id in range(1, 10):
            with self.subTest(lab=lab_id):
                sid, before = self.completed_student()
                old_flag = self.app.public_lab(sid, lab_id, before["labs"][str(lab_id)])["flag"]
                response = self.app.reset(sid, lab_id)
                self.assert_cleared(response, lab_id)
                after = self.app.store.get(sid)[1]
                key = str(lab_id)
                self.assertEqual(after["labs"][key]["attempt"], before["labs"][key]["attempt"] + 1)
                self.assertEqual(after["labs"][key]["defense_level"], before["labs"][key]["defense_level"])
                self.assertEqual({k: r for k, r in after["labs"].items() if k != key},
                                 {k: r for k, r in before["labs"].items() if k != key})
                reopened = server.Application(self.directory)
                self.assert_cleared(reopened.lab_result(sid, lab_id, reopened.store.get(sid)[1]), lab_id)
                with self.assertRaises(server.APIError) as error:
                    reopened.submit_flag(sid, lab_id, {"flag": old_flag})
                self.assertEqual(error.exception.status, 403)
                self.assertEqual(self.app.store.get(other_sid)[1], other_before)

    def test_stale_unrelated_save_cannot_restore_any_restarted_lab_award(self):
        for lab_id in range(1, 10):
            with self.subTest(lab=lab_id):
                sid, stale = self.completed_student()
                self.app.reset(sid, lab_id)
                reset_record = copy.deepcopy(self.app.store.get(sid)[1]["labs"][str(lab_id)])
                other = "1" if lab_id != 1 else "7"
                stale["labs"][other]["messages"] = [{"role": "user", "content": "Public maintenance question."}]
                server.Application(self.directory).store.save(sid, stale)
                after = self.app.store.get(sid)[1]
                self.assertEqual(after["labs"][str(lab_id)], reset_record)
                self.assertEqual(after["labs"][other]["messages"], stale["labs"][other]["messages"])
                self.assert_cleared(self.app.lab_result(sid, lab_id, after), lab_id)

    def test_reset_reads_latest_row_and_does_not_replace_other_labs_from_a_stale_snapshot(self):
        sid, stale = self.completed_student()
        latest = copy.deepcopy(stale)
        latest["labs"]["6"]["attempt"] = 6
        latest["labs"]["1"]["messages"] = [{"role": "user", "content": "Latest public maintenance note."}]
        with self.app.store.connect() as db:
            db.execute("UPDATE sessions SET data=? WHERE sid=?", (json.dumps(latest), sid))
        with patch.object(self.app.store, "get", return_value=(sid, stale)):
            self.assert_cleared(self.app.reset(sid, 6), 6)
        after = self.app.store.get(sid)[1]
        self.assertEqual(after["labs"]["6"]["attempt"], 7)
        self.assertEqual(after["labs"]["1"], latest["labs"]["1"])

    def test_concurrent_restarts_of_distinct_labs_preserve_every_reset(self):
        sid, _ = self.completed_student()
        def reset(lab_id):
            return server.Application(self.directory).reset(sid, lab_id)
        with ThreadPoolExecutor(max_workers=9) as pool:
            list(pool.map(reset, range(1, 10)))
        after = self.app.refresh_state(sid)
        self.assertEqual(after["summary"]["score"], 0)
        self.assertEqual(after["summary"]["completed"], [])
        for record in self.app.store.get(sid)[1]["labs"].values():
            self.assertEqual(record["attempt"], 2)
            self.assertFalse(record["completed"])
            self.assertFalse(record["exploited"])

    def test_concurrent_restarts_of_one_lab_have_distinct_attempts(self):
        sid, _ = self.completed_student()
        def reset(_):
            return server.Application(self.directory).reset(sid, 6)
        with ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(reset, range(6)))
        self.assertEqual(sorted(response["lab"]["attempt"] for response in responses), list(range(2, 8)))
        for response in responses:
            self.assert_cleared(response, 6)
        self.assertEqual(self.app.store.get(sid)[1]["labs"]["6"]["attempt"], 7)

    def test_missing_identity_cannot_reset_or_create_a_student(self):
        with self.app.store.connect() as db:
            before = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        with self.assertRaises(server.APIError) as error:
            self.app.reset("missing-student", 6)
        self.assertEqual(error.exception.status, 409)
        with self.app.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], before)


if __name__ == "__main__":
    unittest.main()
