from datetime import datetime
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("discarded_initiative", ROOT / "initiative.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Store = module.InitiativeStore
UMO = "default:FriendMessage:111"


def stamp(hour=12, minute=0, second=0):
    return datetime(2026, 10, 31, hour, minute, second, tzinfo=module.CHINA_TIME).timestamp()


class DiscardedPlanTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "initiative.json"
        self.clock = [stamp()]
        self.config = {"proactive_enabled": True, "self_initiated_enabled": True,
                       "private_self_initiated_enabled": True,
                       "proactive_all_groups": True,
                       "proactive_quiet_start": 23, "proactive_quiet_end": 8}
        self.store = Store(self.config, self.path, now=lambda: self.clock[0])

    def plan(self):
        return self.store.schedule(UMO, 300, "稍后接着聊", "conv-1")

    def test_constructor_records_only_committed_expired_removals_without_schema_change(self):
        old = self.plan()
        self.clock[0] = old["due_at"] + 121
        restarted = Store(self.config, self.path, now=lambda: self.clock[0])
        self.assertEqual(restarted.list_pending(), [])
        self.assertEqual(restarted.drain_discarded(), [{"job": old, "reason": "expired"}])
        self.assertEqual(restarted.drain_discarded(), [])
        self.assertEqual(set(json.loads(self.path.read_text(encoding="utf-8"))),
                         {"version", "jobs", "recent"})

    def test_pop_due_records_expired_scope_and_quiet_reasons(self):
        for reason in ("expired", "scope_disabled", "quiet_hours"):
            with self.subTest(reason=reason):
                self.config["proactive_enabled"] = True
                self.clock[0] = stamp(22, 54, 40) if reason == "quiet_hours" else stamp()
                job = self.plan()
                self.clock[0] = job["due_at"] + (121 if reason == "expired" else 20)
                if reason == "scope_disabled":
                    self.config["proactive_enabled"] = False
                self.assertEqual(self.store.pop_due(), [])
                self.assertEqual(self.store.drain_discarded(), [{"job": job, "reason": reason}])
                self.assertEqual(self.store.list_pending(), [])

    def test_returned_due_job_and_not_yet_due_job_are_not_reported_as_discarded(self):
        job = self.plan()
        self.clock[0] = job["due_at"] - 1
        self.assertEqual(self.store.pop_due(), [])
        self.assertEqual(self.store.drain_discarded(), [])
        self.clock[0] = job["due_at"]
        self.assertEqual(self.store.pop_due(), [job])
        self.assertEqual(self.store.drain_discarded(), [])

    def test_drain_is_once_only_and_returns_a_deep_copy(self):
        job = self.plan()
        self.clock[0] = job["due_at"] + 121
        self.store.pop_due()
        original_reference = self.store._discarded[0]
        result = self.store.drain_discarded()
        result[0]["job"]["reason"] = "changed by caller"
        self.assertEqual(original_reference["job"]["reason"], job["reason"])
        self.assertEqual(self.store.drain_discarded(), [])

    def test_pop_due_write_failure_does_not_announce_or_forget_discard(self):
        job = self.plan()
        self.clock[0] = job["due_at"] + 121
        original = self.path.read_bytes()
        with patch.object(self.store, "_commit", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                self.store.pop_due()
        self.assertEqual(self.store.drain_discarded(), [])
        self.assertEqual(self.store.list_pending(), [job])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.store.pop_due(), [])
        self.assertEqual(self.store.drain_discarded(), [{"job": job, "reason": "expired"}])

    def test_constructor_write_failure_does_not_report_uncommitted_discard(self):
        job = self.plan()
        self.clock[0] = job["due_at"] + 121
        original = self.path.read_bytes()
        unfinished = Store.__new__(Store)
        with patch.object(Store, "_commit", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                Store.__init__(unfinished, self.config, self.path, now=lambda: self.clock[0])
        self.assertEqual(unfinished.drain_discarded(), [])
        self.assertEqual(unfinished.list_pending(), [job])
        self.assertEqual(self.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
