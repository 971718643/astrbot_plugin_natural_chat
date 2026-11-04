"""Atomic terminal evidence with real files and process-death boundaries."""

from copy import deepcopy
import json
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

import test_durable_claims as durable
import test_initiative as baseline


class TerminalReceiptStoreTests(unittest.TestCase):
    setUp = durable.DurableClaimTests.setUp
    open = durable.DurableClaimTests.open
    plan = durable.DurableClaimTests.plan

    def claim(self, umo=durable.A):
        job = self.plan(umo)
        self.clock[0] += 300
        self.assertEqual(self.store.pop_due(), [job])
        return job

    def row(self, job, **changes):
        result = {"id": uuid.uuid4().hex, "umo": job["umo"], "job_id": job["id"],
                  "at": self.clock[0], "outcome": "silent", "reason": "model_chose_silence",
                  "delivered_parts": 0}
        result.update(changes)
        return result

    def test_atomic_receipt_releases_capacity_and_survives_restart_without_reclaim(self):
        job = self.claim()
        row = self.row(job)
        self.store.release_claim_locally(job["id"])
        self.assertTrue(self.store.archive_claim(job["id"], row))
        self.assertEqual(self.store.list_claimed(), [])
        self.assertFalse(self.store.is_claim_locally_released(job["id"]))
        self.assertEqual(self.store.claim_capacity_remaining(), 100)
        self.assertEqual(self.store.list_terminal_receipts(), [row])
        disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(disk["claimed"], {})
        self.assertEqual(disk["terminal_receipts"], [row])
        self.assertEqual(disk["terminal_receipts_dropped"], 0)
        reopened = self.open()
        self.assertEqual(reopened.list_terminal_receipts(), [row])
        self.assertEqual(reopened.pop_due(), [])
        self.assertEqual(reopened.list_claimed(), [])
        next_job = reopened.schedule(job["umo"], 300, "新的具体话题", job["conversation_id"])
        self.clock[0] += 300
        self.assertEqual(reopened.pop_due(), [next_job])

    def test_failed_archive_keeps_claim_receipt_file_and_local_release_unchanged(self):
        job = self.claim()
        row = self.row(job)
        self.store.release_claim_locally(job["id"])
        original = self.path.read_bytes()
        claims = self.store.list_claimed()
        with patch.object(self.store, "_commit", side_effect=OSError("queue unavailable")):
            with self.assertRaises(OSError):
                self.store.archive_claim(job["id"], row)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.store.list_claimed(), claims)
        self.assertEqual(self.store.list_terminal_receipts(), [])
        self.assertTrue(self.store.is_claim_locally_released(job["id"]))
        self.assertEqual(self.open().list_claimed(), claims)

    def test_archive_is_idempotent_and_does_not_replace_original_identity_or_time(self):
        job = self.claim()
        row = self.row(job)
        self.store.archive_claim(job["id"], row)
        original = self.path.read_bytes()
        retry = self.row(job, at=row["at"] + 30, outcome="interrupted", reason="restart_after_claim")
        self.assertTrue(self.store.archive_claim(job["id"], retry))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.store.list_terminal_receipts(), [row])
        self.assertEqual(self.store.terminal_receipts_dropped(), 0)
        self.assertFalse(self.store.archive_claim("missing-job", self.row({"id": "missing-job", "umo": durable.A})))
        self.assertEqual(self.path.read_bytes(), original)

    def test_finish_receipt_failure_preserves_evidence_until_successful_commit(self):
        job = self.claim()
        row = self.row(job)
        self.store.archive_claim(job["id"], row)
        original = self.path.read_bytes()
        with patch.object(self.store, "_commit", side_effect=OSError("queue unavailable")):
            with self.assertRaises(OSError):
                self.store.finish_terminal_receipt(job["id"])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.store.list_terminal_receipts(), [row])
        self.assertTrue(self.store.finish_terminal_receipt(job["id"]))
        self.assertFalse(self.store.finish_terminal_receipt(job["id"]))
        self.assertEqual(self.open().list_terminal_receipts(), [])
        self.assertEqual(self.open().terminal_receipts_dropped(), 0)

    def test_production_ring_keeps_latest_hundred_and_counts_older_retention(self):
        self.assertEqual(baseline.module.MAX_TERMINAL_RECEIPTS, 100)
        rows = []
        for _ in range(105):
            job = self.claim()
            row = self.row(job)
            rows.append(row)
            self.store.archive_claim(job["id"], row)
        self.assertEqual(self.store.list_claimed(), [])
        self.assertEqual(self.store.list_terminal_receipts(), list(reversed(rows[-100:])))
        self.assertEqual(self.store.terminal_receipts_dropped(), 5)
        reopened = self.open()
        self.assertEqual(reopened.list_terminal_receipts(), list(reversed(rows[-100:])))
        self.assertEqual(reopened.terminal_receipts_dropped(), 5)
        reopened.finish_terminal_receipt(rows[-1]["job_id"])
        self.assertEqual(len(reopened.list_terminal_receipts()), 99)
        self.assertEqual(self.open().terminal_receipts_dropped(), 5)

    def test_receipts_are_filtered_deep_copies_and_sorted_by_original_time(self):
        jobs = [self.plan(durable.A), self.plan(durable.B)]
        self.clock[0] += 300
        self.store.pop_due()
        newer = self.row(jobs[0], at=self.clock[0] + 10)
        older = self.row(jobs[1], at=self.clock[0])
        self.store.archive_claim(jobs[0]["id"], newer)
        self.store.archive_claim(jobs[1]["id"], older)
        newer["reason"] = "caller_mutation"
        rows = self.store.list_terminal_receipts(durable.A)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["job_id"], jobs[0]["id"])
        rows[0]["reason"] = "read_mutation"
        self.assertEqual([row["job_id"] for row in self.store.list_terminal_receipts()],
                         [jobs[0]["id"], jobs[1]["id"]])
        self.assertTrue(all(row["reason"] == "model_chose_silence"
                            for row in self.open().list_terminal_receipts()))

    def test_identity_mismatch_and_diagnostic_prose_are_rejected_without_mutation(self):
        job = self.claim()
        valid = self.row(job)
        original = self.path.read_bytes()
        invalid = [dict(valid, job_id="different-job"), dict(valid, umo=durable.B),
                   dict(valid, text="private conversation"), dict(valid, reason="完整异常和聊天正文"),
                   dict(valid, outcome="sk-secret"), dict(valid, reason="bearer:secret"),
                   dict(valid, at=float("nan")), dict(valid, at=True),
                   dict(valid, delivered_parts=True), dict(valid, id="bad id"),
                   dict(valid, umo="default:GroupMessage:unsafe name")]
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.store.archive_claim(job["id"], row)
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(self.store.list_terminal_receipts(), [])

    def test_duplicate_receipt_id_for_different_job_is_rejected(self):
        jobs = [self.plan(durable.A), self.plan(durable.B)]
        self.clock[0] += 300
        self.store.pop_due()
        row = self.row(jobs[0])
        self.store.archive_claim(jobs[0]["id"], row)
        original = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.store.archive_claim(jobs[1]["id"], self.row(jobs[1], id=row["id"]))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.store.list_claimed()[0]["job"]["id"], jobs[1]["id"])

    def test_old_v1_file_has_no_receipt_migration_write(self):
        self.plan()
        original = self.path.read_bytes()
        reopened = self.open()
        self.assertEqual(reopened.list_terminal_receipts(), [])
        self.assertEqual(reopened.terminal_receipts_dropped(), 0)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(reopened.finish_terminal_receipt("missing"))
        self.assertEqual(self.path.read_bytes(), original)

    def test_corrupt_receipt_state_is_rejected_without_overwriting_evidence(self):
        job = self.claim()
        row = self.row(job)
        self.store.archive_claim(job["id"], row)
        valid = json.loads(self.path.read_text(encoding="utf-8"))
        malformed = []
        duplicate = deepcopy(valid)
        duplicate["terminal_receipts"].append(deepcopy(row))
        malformed.append(duplicate)
        overlap = deepcopy(valid)
        overlap["claimed"][job["id"]] = {"job": job, "claimed_at": self.clock[0]}
        malformed.append(overlap)
        pending_overlap = deepcopy(valid)
        pending_overlap["jobs"][job["umo"]] = job
        malformed.append(pending_overlap)
        for bad_count in (True, -1, "1"):
            corrupted = deepcopy(valid)
            corrupted["terminal_receipts_dropped"] = bad_count
            malformed.append(corrupted)
        for bad_receipts in ({}, [dict(row, reason="正文")], [dict(row, extra="secret")]):
            corrupted = deepcopy(valid)
            corrupted["terminal_receipts"] = bad_receipts
            malformed.append(corrupted)
        for state in malformed:
            self.path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            original = self.path.read_bytes()
            with self.subTest(state=state), self.assertRaises(ValueError):
                self.open()
            self.assertEqual(self.path.read_bytes(), original)

    def crash_archive(self, phase):
        job = self.claim()
        row = self.row(job)
        before = self.path.read_bytes()
        program = '''
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location("child_store", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
store = module.InitiativeStore(json.loads(sys.argv[3]), sys.argv[2], now=lambda: float(sys.argv[4]))
row = json.loads(sys.argv[5])
original_commit = store._commit
def stop_at_boundary(state):
    if sys.argv[6] == "before":
        os._exit(41)
    original_commit(state)
    os._exit(42)
store._commit = stop_at_boundary
store.archive_claim(row["job_id"], row)
raise AssertionError("child must stop at archive boundary")
'''
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", program,
                                 str(durable.SOURCE), str(self.path), json.dumps(self.config),
                                 str(self.clock[0]), json.dumps(row), phase],
                                capture_output=True, timeout=15, check=False)
        self.assertEqual(result.returncode, 41 if phase == "before" else 42,
                         result.stderr.decode("utf-8", errors="replace"))
        reopened = self.open()
        self.assertEqual(reopened.list_pending(), [])
        self.assertEqual(reopened.pop_due(), [])
        if phase == "before":
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(reopened.list_claimed()[0]["job"]["id"], job["id"])
            self.assertEqual(reopened.list_terminal_receipts(), [])
        else:
            self.assertEqual(reopened.list_claimed(), [])
            self.assertEqual(reopened.list_terminal_receipts(), [row])

    def test_hard_exit_before_archive_commit_preserves_claim(self):
        self.crash_archive("before")

    def test_hard_exit_after_archive_commit_preserves_receipt_without_claim(self):
        self.crash_archive("after")

    def test_full_claim_cap_rejects_new_schedule_without_overwriting_existing_pending(self):
        with patch.object(baseline.module, "MAX_RETAINED_CLAIMS", 1):
            first = self.plan(durable.A)
            future = self.plan(durable.B, delay=600)
            self.clock[0] += 300
            self.store.pop_due()
            self.store.release_claim_locally(first["id"])
            original = self.path.read_bytes()
            with self.assertRaisesRegex(ValueError, "claim_capacity_exhausted"):
                self.plan(durable.B)
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(self.store.list_pending(), [future])
            self.store.archive_claim(first["id"], self.row(first))
            self.assertTrue(self.plan(durable.B))

    def test_full_cap_cancellation_atomically_records_future_and_queued_pending(self):
        with patch.object(baseline.module, "MAX_RETAINED_CLAIMS", 1):
            first = self.plan(durable.A)
            queued = self.plan(durable.B)
            future = self.plan(durable.C, delay=600)
            self.clock[0] += 300
            self.store.pop_due()
            claims = self.store.list_claimed()
            self.assertEqual(self.store.cancel_unclaimable_pending(), 2)
            self.assertEqual(self.store.list_pending(), [])
            self.assertEqual(self.store.list_claimed(), claims)
            discarded = self.store.drain_discarded()
            self.assertEqual({row["job"]["id"] for row in discarded}, {queued["id"], future["id"]})
            self.assertEqual({row["reason"] for row in discarded}, {"claim_capacity_exhausted"})
            self.assertEqual(self.store.cancel_unclaimable_pending(), 0)
            self.assertEqual(self.store.drain_discarded(), [])
            self.assertEqual(claims[0]["job"]["id"], first["id"])

    def test_full_cap_cancellation_failure_preserves_file_pending_and_diagnostics(self):
        with patch.object(baseline.module, "MAX_RETAINED_CLAIMS", 1):
            self.plan(durable.A)
            future = self.plan(durable.B, delay=600)
            self.assertEqual(self.store.cancel_unclaimable_pending(), 0)
            self.clock[0] += 300
            self.store.pop_due()
            original = self.path.read_bytes()
            with patch.object(self.store, "_commit", side_effect=OSError("queue unavailable")):
                with self.assertRaises(OSError):
                    self.store.cancel_unclaimable_pending()
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(self.store.list_pending(), [future])
            self.assertEqual(self.store.drain_discarded(), [])


if __name__ == "__main__":
    unittest.main()
