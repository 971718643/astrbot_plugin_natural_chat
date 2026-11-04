"""Capacity waiting and process-local releases retain durable task evidence."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_initiative as baseline


Store = baseline.Store
subject = baseline.module
A = "default:FriendMessage:111"
B = "default:FriendMessage:222"


class QueueWaitStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "state.json"
        self.clock = [baseline.stamp()]
        self.config = {"proactive_enabled": True, "self_initiated_enabled": True,
                       "proactive_all_groups": True, "private_self_initiated_enabled": True,
                       "proactive_quiet_start": 23, "proactive_quiet_end": 8}
        self.store = self.open()

    def open(self):
        return Store(self.config, self.path, now=lambda: self.clock[0])

    def plan(self, umo=A):
        return self.store.schedule(umo, 300, "原有具体话题", "active-persona-conversation")

    def queue(self, umo=A):
        job = self.plan(umo)
        self.clock[0] += 300
        self.assertEqual(self.store.pop_due(max_jobs=0), [])
        return job

    def test_fifth_job_waits_180_seconds_for_capacity_without_expiring(self):
        jobs = [self.plan(f"default:FriendMessage:{index}") for index in range(1, 6)]
        self.clock[0] += 300
        first = self.store.pop_due(max_jobs=4)
        self.assertEqual(len(first), 4)
        [waiting] = self.store.list_pending()
        self.assertEqual(waiting["id"], jobs[4]["id"])
        self.assertEqual(waiting["capacity_queued_at"], self.clock[0])
        self.clock[0] += 180
        self.store.finish_claim(first[0]["id"])
        [ready] = self.store.pop_due(max_jobs=1)
        self.assertEqual(ready["id"], jobs[4]["id"])
        self.assertEqual(ready["due_at"], jobs[4]["due_at"])
        self.assertEqual(ready["capacity_wait_seconds"], 180)
        self.assertEqual(subject.execution_due_at(ready), self.clock[0])
        self.assertEqual(self.store.drain_discarded(), [])

    def test_queue_timeout_is_separate_and_bounded(self):
        job = self.queue()
        self.clock[0] += 601
        self.assertEqual(self.store.pop_due(max_jobs=1), [])
        self.assertEqual(self.store.list_pending(), [])
        self.assertEqual(self.store.list_claimed(), [])
        [discarded] = self.store.drain_discarded()
        self.assertEqual(discarded["job"]["id"], job["id"])
        self.assertEqual(discarded["reason"], "queue_timeout")

    def test_exact_600_second_queue_boundary_is_allowed(self):
        job = self.queue()
        self.clock[0] += 600
        [ready] = self.store.pop_due(max_jobs=1)
        self.assertEqual(ready["id"], job["id"])
        self.assertEqual(ready["capacity_wait_seconds"], 600)
        self.assertEqual(subject.execution_due_at(ready), self.clock[0])

    def test_pre_queue_lateness_still_uses_original_grace(self):
        job = self.plan()
        self.clock[0] += 360
        self.store.pop_due(max_jobs=0)
        self.clock[0] += 180
        [ready] = self.store.pop_due(max_jobs=1)
        self.assertEqual(ready["id"], job["id"])
        self.assertEqual(self.clock[0] - subject.execution_due_at(ready), 60)

    def test_already_expired_unqueued_job_cannot_be_resurrected(self):
        job = self.plan()
        self.clock[0] += 430
        self.assertEqual(self.store.pop_due(max_jobs=0), [])
        [discarded] = self.store.drain_discarded()
        self.assertEqual((discarded["job"]["id"], discarded["reason"]), (job["id"], "expired"))
        self.assertNotIn("capacity_queued_at", discarded["job"])

    def test_queue_wait_survives_restart_and_only_queue_timeout_prunes_it(self):
        job = self.queue()
        self.clock[0] += 180
        reopened = self.open()
        self.assertEqual(reopened.list_pending()[0]["id"], job["id"])
        self.assertEqual(reopened.drain_discarded(), [])
        self.clock[0] += 421
        expired = self.open()
        self.assertEqual(expired.list_pending(), [])
        [discarded] = expired.drain_discarded()
        self.assertEqual(discarded["reason"], "queue_timeout")

    def test_queue_mark_write_failure_does_not_update_memory_or_disk(self):
        job = self.plan()
        self.clock[0] += 300
        original = self.path.read_bytes()
        with patch.object(self.store, "_commit", side_effect=OSError("unavailable")):
            with self.assertRaises(OSError):
                self.store.pop_due(max_jobs=0)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.store.list_pending(), [job])
        self.assertEqual(self.open().list_pending(), [job])

    def test_scope_and_quiet_still_apply_to_waiting_jobs(self):
        self.queue()
        self.clock[0] += 180
        self.config["private_self_initiated_enabled"] = False
        self.assertEqual(self.store.pop_due(max_jobs=1), [])
        self.assertEqual(self.store.drain_discarded()[0]["reason"], "scope_disabled")
        self.config["private_self_initiated_enabled"] = True
        self.queue(B)
        self.config.update(proactive_quiet_start=12, proactive_quiet_end=13)
        self.assertEqual(self.store.pop_due(max_jobs=1), [])
        self.assertEqual(self.store.drain_discarded()[0]["reason"], "quiet_hours")

    def test_excluded_and_blocking_claim_sessions_also_gain_queue_credit(self):
        first = self.plan()
        self.clock[0] += 300
        self.store.pop_due()
        pending = self.plan()
        self.clock[0] += 300
        self.assertEqual(self.store.pop_due(max_jobs=1), [])
        self.assertIn("capacity_queued_at", self.store.list_pending()[0])
        self.clock[0] += 180
        self.store.finish_claim(first["id"])
        [ready] = self.store.pop_due(max_jobs=1)
        self.assertEqual(ready["id"], pending["id"])
        self.assertEqual(ready["capacity_wait_seconds"], 180)
        other = self.plan(B)
        self.clock[0] += 300
        self.assertEqual(self.store.pop_due(max_jobs=1, exclude_umos={B}), [])
        self.clock[0] += 180
        [ready] = self.store.pop_due(max_jobs=1)
        self.assertEqual(ready["id"], other["id"])
        self.assertEqual(ready["capacity_wait_seconds"], 180)

    def test_local_release_unblocks_new_plan_without_erasing_disk_claim(self):
        first = self.plan()
        self.clock[0] += 300
        self.store.pop_due()
        original = self.path.read_bytes()
        self.assertTrue(self.store.release_claim_locally(first["id"]))
        self.assertTrue(self.store.is_claim_locally_released(first["id"]))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.store.list_blocking_claimed(A), [])
        self.assertEqual(len(self.store.list_claimed(A)), 1)
        pending = self.plan()
        self.clock[0] += 300
        self.assertEqual(self.store.pop_due(), [pending])
        disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn(first["id"], disk["claimed"])
        self.assertIn(pending["id"], disk["claimed"])
        reopened = self.open()
        self.assertFalse(reopened.is_claim_locally_released(first["id"]))
        self.assertEqual(len(reopened.list_blocking_claimed(A)), 2)
        self.assertEqual(reopened.pop_due(), [])

    def test_finish_failure_keeps_local_release_and_durable_evidence(self):
        first = self.plan()
        self.clock[0] += 300
        self.store.pop_due()
        self.store.release_claim_locally(first["id"])
        with patch.object(self.store, "_commit", side_effect=OSError("unavailable")):
            with self.assertRaises(OSError):
                self.store.finish_claim(first["id"])
        self.assertTrue(self.store.is_claim_locally_released(first["id"]))
        self.assertEqual(len(self.open().list_claimed(A)), 1)
        self.assertTrue(self.store.finish_claim(first["id"]))
        self.assertFalse(self.store.is_claim_locally_released(first["id"]))
        self.assertEqual(self.open().list_claimed(), [])

    def test_queue_fields_are_validated_without_altering_legacy_data(self):
        self.plan()
        legacy = self.path.read_bytes()
        self.assertEqual(subject.execution_due_at(self.store.list_pending()[0]),
                         self.store.list_pending()[0]["due_at"])
        self.assertEqual(self.open().path.read_bytes(), legacy)
        for field, value in (("capacity_queued_at", True), ("capacity_queued_at", float("nan")),
                             ("capacity_queued_at", self.clock[0]),
                             ("capacity_wait_seconds", 1)):
            state = json.loads(legacy)
            state["jobs"][A][field] = value
            self.path.write_text(json.dumps(state), encoding="utf-8")
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.open()
        self.path.write_bytes(legacy)
        for value in (True, -1, 601, float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                subject.execution_due_at({"due_at": self.clock[0], "capacity_wait_seconds": value})


if __name__ == "__main__":
    unittest.main()
