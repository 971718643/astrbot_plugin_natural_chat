"""Idempotent restoration of buffered diagnostic records, without re-timing."""

from copy import deepcopy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("outcome_append_subject", ROOT / "outcomes.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
OutcomeLog = module.OutcomeLog


class OutcomeAppendTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "outcomes.json"
        self.log = OutcomeLog(self.path, now=lambda: (_ for _ in ()).throw(AssertionError("must keep original time")))

    def row(self, index=1):
        return {"id": "record-" + str(index), "umo": "default:FriendMessage:111",
                "job_id": "job-" + str(index), "at": 1793444400.25 + index,
                "outcome": "silent", "reason": "model_chose_silence", "delivered_parts": 0}

    def test_original_identity_and_time_survive_restart(self):
        expected = self.row()
        self.assertEqual(self.log.append_record(expected), expected)
        self.assertEqual(OutcomeLog(self.path).last(), expected)

    def test_duplicate_id_returns_original_without_commit_or_reordering(self):
        original = self.row()
        self.log.append_record(original)
        self.log.append_record(self.row(2))
        edited = deepcopy(original)
        edited.update(outcome="sent", at=1793449900.0)
        before = self.path.read_bytes()
        with patch.object(self.log, "_commit", side_effect=AssertionError("duplicate must not write")):
            self.assertEqual(self.log.append_record(edited), original)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.log.last()["id"], "record-2")

    def test_input_and_return_are_not_references_to_stored_data(self):
        incoming = self.row()
        result = self.log.append_record(incoming)
        incoming["reason"] = "changed"
        result["outcome"] = "changed"
        self.assertEqual(self.log.last(), self.row())

    def test_validation_rejects_extra_body_prose_and_invalid_duplicate(self):
        self.log.append_record(self.row())
        before = self.path.read_bytes()
        variants = []
        for edits in ({"text": "private chat content"}, {"reason": "raw private exception"},
                      {"at": float("nan")}, {"outcome": "sk-private"}):
            row = self.row()
            row.update(edits)
            variants.append(row)
        for row in variants:
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.log.append_record(row)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.log.records(), [self.row()])

    def test_replace_failure_keeps_memory_and_disk_unmodified(self):
        self.log.append_record(self.row())
        before = self.path.read_bytes()
        with patch.object(module.os, "replace", side_effect=OSError("storage unavailable")):
            with self.assertRaises(OSError):
                self.log.append_record(self.row(2))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.log.records(), [self.row()])
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_restore_keeps_most_recent_hundred_records(self):
        for index in range(105):
            self.log.append_record(self.row(index))
        rows = OutcomeLog(self.path).records(limit=100)
        self.assertEqual(len(rows), 100)
        self.assertEqual((rows[0]["id"], rows[-1]["id"]), ("record-104", "record-5"))
        self.assertEqual(rows[-1]["at"], self.row(5)["at"])


if __name__ == "__main__":
    unittest.main()
