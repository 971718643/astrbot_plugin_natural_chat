"""Real-file bound for evidence retained during a long journal failure."""

import json
import unittest
from unittest.mock import patch

import test_durable_claims as durable
import test_initiative as baseline


class ClaimRetentionStoreTests(unittest.TestCase):
    setUp = durable.DurableClaimTests.setUp
    open = durable.DurableClaimTests.open
    plan = durable.DurableClaimTests.plan

    def plans(self, count):
        return [self.plan("default:GroupMessage:" + str(9000 + index))
                for index in range(count)]

    def test_actual_hundred_claim_bound_survives_local_release_and_restart(self):
        self.assertEqual(baseline.module.MAX_RETAINED_CLAIMS, 100)
        jobs = self.plans(101)
        self.clock[0] += 300
        first_batch = self.store.pop_due(max_jobs=99)
        self.assertEqual(len(first_batch), 99)
        worker_batch = self.store.pop_due(max_jobs=4)
        self.assertEqual(len(worker_batch), 1)
        claimed = first_batch + worker_batch
        self.assertEqual(len(claimed), 100)
        self.assertEqual(self.store.claim_capacity_remaining(), 0)
        for job in claimed:
            self.assertTrue(self.store.release_claim_locally(job["id"]))
        self.assertEqual(self.store.list_blocking_claimed(), [])
        self.assertEqual(self.store.pop_due(), [])
        self.assertEqual(len(self.store.list_claimed()), 100)
        queued = self.store.list_pending()[0]
        self.assertEqual(queued["id"], jobs[-1]["id"])
        self.assertEqual(queued["capacity_queued_at"], self.clock[0])
        reopened = self.open()
        self.assertEqual(reopened.claim_capacity_remaining(), 0)
        self.assertEqual(reopened.pop_due(), [])
        self.assertEqual(len(reopened.list_claimed()), 100)
        self.assertTrue(reopened.finish_claim(claimed[0]["id"]))
        self.assertEqual(reopened.claim_capacity_remaining(), 1)
        resumed = reopened.pop_due()
        self.assertEqual([job["id"] for job in resumed], [jobs[-1]["id"]])
        self.assertEqual(len(reopened.list_claimed()), 100)
        self.assertEqual(self.open().list_pending(), [])

    def test_legacy_over_limit_is_preserved_and_not_increased(self):
        jobs = self.plans(102)
        self.clock[0] += 300
        claimed = self.store.pop_due()
        state = json.loads(self.path.read_text(encoding="utf-8"))
        legacy = state["jobs"].pop(jobs[-2]["umo"])
        state["claimed"][legacy["id"]] = {"job": legacy, "claimed_at": self.clock[0]}
        self.path.write_text(json.dumps(state), encoding="utf-8")
        original = self.path.read_bytes()
        reopened = self.open()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(len(reopened.list_claimed()), 101)
        self.assertEqual(reopened.claim_capacity_remaining(), 0)
        self.assertEqual(reopened.pop_due(), [])
        self.assertEqual(len(reopened.list_claimed()), 101)
        reopened.finish_claim(claimed[0]["id"])
        self.assertEqual(reopened.claim_capacity_remaining(), 0)
        self.assertEqual(reopened.pop_due(), [])
        reopened.finish_claim(claimed[1]["id"])
        self.assertEqual(reopened.claim_capacity_remaining(), 1)
        self.assertEqual([job["id"] for job in reopened.pop_due()], [jobs[-1]["id"]])
        self.assertEqual(len(reopened.list_claimed()), 100)

    def test_failed_capacity_queue_commit_keeps_pending_and_evidence(self):
        with patch.object(baseline.module, "MAX_RETAINED_CLAIMS", 1):
            jobs = self.plans(2)
            new = self.plan("default:GroupMessage:9999", delay=600)
            self.clock[0] += 300
            # A first claim and queue marker are committed together.
            self.store.pop_due()
            self.store.release_claim_locally(jobs[0]["id"])
            # The future plan was created before the cap filled, and was not
            # queued at the first 300-second poll.
            self.clock[0] += 300
            original = self.path.read_bytes()
            pending = self.store.list_pending()
            with patch.object(self.store, "_commit", side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    self.store.pop_due()
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(self.store.list_pending(), pending)
            self.assertEqual(len(self.store.list_claimed()), 1)
            self.assertTrue(any(job["id"] == new["id"] for job in pending))


if __name__ == "__main__":
    unittest.main()
