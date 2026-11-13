"""Lab 3 public guidance and the existing fixture-based completion boundary."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import catalog
import server


OBJECTIVE = "Manipulate a maintenance document so Plant AI recommends an incorrect inspection interval for PT-B01."
HINTS = [
    "Plant AI uses retrieved maintenance documents. Open View bulletin to inspect the previous posts.",
    "Choose New Document and save your bulletin before asking Plant AI about it. Save also makes it available for retrieval.",
    "Consider how a newer maintenance bulletin could appear more authoritative than an older reference document.",
    "Focus on PT-B01's inspection interval. The trusted standard specifies 12 simulated cycles. Try making Plant AI accept a different interval from your modified bulletin.",
]
FIXTURE = json.loads((Path(__file__).parent / "fixtures/lab3_verified_schedule.json").read_text())


class Lab3GuidanceTests(unittest.TestCase):
    def test_public_objective_matches_the_student_task(self):
        lab = catalog.public_catalog()[2]
        self.assertEqual(lab["title"], "RAG Knowledge Poisoning")
        self.assertEqual(lab["description"], OBJECTIVE)
        self.assertEqual(lab["points"], 150)
        self.assertTrue(lab["has_kb"])

    def test_four_public_hints_follow_the_requested_progression(self):
        self.assertEqual(catalog.public_catalog()[2]["hints"], HINTS)

    def test_public_guidance_does_not_disclose_the_winning_interval(self):
        lab = catalog.public_catalog()[2]
        self.assertNotRegex(" ".join([lab["description"], *lab["hints"]]), r"(?i)\b(?:19|nineteen)\b")
        self.assertNotIn("12 simulated cycles", " ".join(lab["hints"][:3]))
        self.assertIn("12 simulated cycles", lab["hints"][3])

    def test_existing_fixture_requires_flag_submission_and_awards_existing_points_once(self):
        with tempfile.TemporaryDirectory() as directory:
            app = server.Application(Path(directory))
            sid, initial = app.store.get(None)
            app.update_kb(sid, 3, {"documents": [*copy.deepcopy(catalog.LAB3_DOCUMENTS), FIXTURE["document"]],
                                   "sync": True, "use_kb": True})
            transport = io.BytesIO(json.dumps({"message": {"content": FIXTURE["successful_reply"]}}).encode())
            with patch("server.urllib.request.urlopen", return_value=transport):
                captured = app.chat(sid, 3, {"message": FIXTURE["student_message"]})
            self.assertTrue(captured["lab"]["exploited"])
            self.assertFalse(captured["lab"]["completed"])
            self.assertEqual(captured["summary"]["score"], 0)
            with self.assertRaises(server.APIError):
                app.submit_flag(sid, 3, {"flag": "PANOPTIS{incorrect-test-flag}"})
            self.assertFalse(app.store.get(sid)[1]["labs"]["3"]["completed"])
            accepted = app.submit_flag(sid, 3, {"flag": captured["lab"]["flag"]})
            self.assertTrue(accepted["lab"]["completed"])
            self.assertEqual(accepted["summary"]["score"], 150)
            duplicate = app.submit_flag(sid, 3, {"flag": captured["lab"]["flag"]})
            self.assertEqual(duplicate["summary"]["score"], 150)
            restored = server.Application(Path(directory)).store.get(sid)[1]
            self.assertTrue(restored["labs"]["3"]["completed"])
            for lab_id, before in initial["labs"].items():
                if lab_id != "3":
                    self.assertEqual(restored["labs"][lab_id], before)


if __name__ == "__main__":
    unittest.main()
