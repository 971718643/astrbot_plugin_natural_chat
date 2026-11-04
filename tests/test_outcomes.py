import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("outcomes_module", ROOT / "outcomes.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
OutcomeLog = module.OutcomeLog
A = "default:FriendMessage:111"
B = "default:FriendMessage:222"
GROUP = "default:GroupMessage:111"


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "outcomes.json"
        self.log = OutcomeLog(self.path, now=lambda: 1793444400.0)

    def test_new_log_empty_and_record_restart_roundtrip(self):
        self.assertIsNone(self.log.last())
        record = self.log.record(A, "abc-123", "delivery_failed", "send_exception_ActionFailed")
        self.assertEqual(set(record), {"id", "umo", "job_id", "at", "outcome", "reason", "delivered_parts"})
        self.assertEqual(OutcomeLog(self.path).last(A), record)
        self.assertEqual(record["delivered_parts"], 0)

    def test_each_session_isolated_latest_first_and_ids_unique(self):
        records = [self.log.record(umo, "job", outcome) for umo, outcome in
                   [(A, "sent"), (B, "delivery_failed"), (GROUP, "chose_silence"), (A, "expired")]]
        self.assertEqual([row["outcome"] for row in self.log.records(A)], ["expired", "sent"])
        self.assertEqual(self.log.last(B), records[1])
        self.assertEqual(self.log.last(GROUP), records[2])
        self.assertEqual(self.log.last(), records[3])
        self.assertEqual(len({row["id"] for row in records}), 4)

    def test_hard_100_record_bound_and_custom_bound(self):
        for index in range(105):
            self.log.record(A, str(index), "sent")
        restarted = OutcomeLog(self.path)
        self.assertEqual(len(restarted.records(limit=100)), 100)
        self.assertEqual(restarted.records(limit=100)[-1]["job_id"], "5")
        small = OutcomeLog(self.path, max_records=2)
        small.record(B, "latest", "sent")
        self.assertEqual(len(small.records()), 2)
        for value in (0, 101, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                OutcomeLog(self.path, max_records=value)

    def test_returned_records_are_independent_copies(self):
        record = self.log.record(A, "job", "sent")
        record["outcome"] = "tampered"
        latest = self.log.last(A)
        latest["reason"] = "tampered"
        rows = self.log.records()
        rows.clear()
        self.assertEqual(self.log.last(A)["outcome"], "sent")
        self.assertEqual(self.log.last(A)["reason"], "")

    def test_write_replace_failure_preserves_disk_memory_and_cleans_temp(self):
        self.log.record(A, "old", "sent")
        original = self.path.read_bytes()
        with patch.object(module.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.log.record(A, "new", "delivery_failed")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.log.last()["job_id"], "old")
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_fsync_failure_does_not_publish_uncommitted_record(self):
        with patch.object(module.os, "fsync", side_effect=OSError("fsync error")):
            with self.assertRaises(OSError):
                self.log.record(A, "new", "sent")
        self.assertIsNone(self.log.last())
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_corrupt_file_and_extra_fields_preserved_for_evidence(self):
        for raw in (b"not JSON", b'{"version":1,"records":[],"text":"secret"}',
                    b'{"version":true,"records":[]}'):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    OutcomeLog(self.path)
                self.assertEqual(self.path.read_bytes(), raw)
        self.path.unlink()
        row = self.log.record(A, "job", "sent")
        row["text"] = "a chat body"
        self.path.write_text(json.dumps({"version": 1, "records": [row]}), encoding="utf-8")
        original = self.path.read_bytes()
        with self.assertRaises(ValueError):
            OutcomeLog(self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_prose_urls_secrets_and_length_limits_rejected_before_persist(self):
        cases = [dict(outcome=""), dict(outcome="x" * 49), dict(reason="x" * 97),
                 dict(reason="发送失败了"), dict(reason="failed with raw error"),
                 dict(reason="https://example.com"), dict(reason="sk-secret-key"),
                 dict(reason="Bearer:abc123"), dict(job_id="a" * 65),
                 dict(job_id="job_with_underscore"), dict(delivered_parts=-1),
                 dict(delivered_parts=True), dict(delivered_parts=1.1)]
        for overrides in cases:
            args = dict(umo=A, job_id="job", outcome="sent", reason="", delivered_parts=0)
            args.update(overrides)
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.log.record(**args)
        self.assertFalse(self.path.exists())

    def test_invalid_session_types_and_empty_parts_are_rejected(self):
        for umo in ("", "default:OtherMessage:111", ":FriendMessage:111",
                    "default:FriendMessage:", "default:FriendMessage:   ", None,
                    "default:FriendMessage:111\nbody"):
            with self.subTest(umo=umo), self.assertRaises(ValueError):
                self.log.record(umo, "job", "sent")
        self.assertFalse(self.path.exists())

    def test_nonfinite_clock_rejected_and_read_limits_validated(self):
        for now in (float("nan"), float("inf"), True, "1793444400"):
            bad = OutcomeLog(self.path, now=lambda: now)
            with self.subTest(now=now), self.assertRaises(ValueError):
                bad.record(A, "job", "sent")
        self.log.record(A, "job", "sent", delivered_parts=2)
        self.assertEqual(self.log.records(limit=0), [])
        self.assertEqual(self.log.records(limit=1)[0]["delivered_parts"], 2)
        for limit in (-1, 101, True, 0.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.log.records(limit=limit)


if __name__ == "__main__":
    unittest.main()
