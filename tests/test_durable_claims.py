"""Durable claiming: real filesystem/process boundaries, no framework or network."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_initiative as baseline


Store = baseline.Store
SOURCE = Path(__file__).resolve().parents[1] / "initiative.py"
A = "default:FriendMessage:111"
B = "default:FriendMessage:222"
C = "default:GroupMessage:111"


class DurableClaimTests(unittest.TestCase):
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

    def plan(self, umo=A, delay=300):
        return self.store.schedule(umo, delay, "具体话题待考虑", "same-persona-conversation")

    def test_atomic_claim_failure_preserves_pending_and_original_file(self):
        job = self.plan()
        original = self.path.read_bytes()
        self.clock[0] += 300
        with patch.object(self.store, "_commit", side_effect=OSError("disk write failed")):
            with self.assertRaises(OSError):
                self.store.pop_due()
        self.assertEqual(self.store.list_pending(), [job])
        self.assertEqual(self.store.list_claimed(), [])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.open().list_pending(), [job])

    def test_old_v1_file_is_read_without_rewrite_and_migrates_on_claim(self):
        job = self.plan()
        self.store.remember(A, "user", "保留已有的真实聊天", message_id="m1")
        state = json.loads(self.path.read_text(encoding="utf-8"))
        state.pop("claimed", None)
        self.path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        original = self.path.read_bytes()
        reopened = self.open()
        self.assertEqual(reopened.list_claimed(), [])
        self.assertEqual(reopened.list_pending(), [job])
        self.assertEqual(self.path.read_bytes(), original)
        self.clock[0] += 300
        self.assertEqual(reopened.pop_due(), [job])
        disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(disk["version"], 1)
        self.assertEqual(disk["jobs"], {})
        self.assertEqual(disk["claimed"][job["id"]]["job"], job)
        self.assertEqual(disk["recent"], state["recent"])

    def crash_claim(self, before_return=False):
        job = self.plan()
        self.store.remember(A, "user", "真实消息应保留", message_id="m1")
        self.clock[0] += 300
        program = '''
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location("child_store", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
store = module.InitiativeStore(json.loads(sys.argv[3]), sys.argv[2], now=lambda: float(sys.argv[4]))
if sys.argv[5] == "before_return":
    original_commit = store._commit
    def committed_then_crash(state):
        original_commit(state)
        if state.get("claimed"):
            os._exit(24)
    store._commit = committed_then_crash
store.pop_due()
os._exit(23)
'''
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", program,
                                 str(SOURCE), str(self.path), json.dumps(self.config),
                                 str(self.clock[0]), "before_return" if before_return else "after_return"],
                                capture_output=True, timeout=15, check=False)
        self.assertEqual(result.returncode, 24 if before_return else 23,
                         result.stderr.decode("utf-8", errors="replace"))
        restarted = self.open()
        self.assertEqual(restarted.list_pending(), [])
        self.assertEqual(restarted.list_claimed(), [{"job": job, "claimed_at": self.clock[0]}])
        self.assertEqual(restarted.pop_due(), [])
        self.assertEqual(restarted.recent(A)[0]["text"], "真实消息应保留")

    def test_child_process_hard_exit_after_pop_keeps_claim(self):
        self.crash_claim()

    def test_child_process_hard_exit_after_commit_before_return_keeps_claim(self):
        self.crash_claim(before_return=True)

    def test_due_order_capacity_exclusions_and_independent_sessions(self):
        a = self.plan(A, 350)
        b = self.plan(B, 300)
        c = self.plan(C, 330)
        self.clock[0] += 350
        self.assertEqual(self.store.pop_due(max_jobs=1), [b])
        queued_a = dict(a, capacity_queued_at=self.clock[0], capacity_wait_seconds=0)
        queued_c = dict(c, capacity_queued_at=self.clock[0])
        self.assertEqual(self.store.pop_due(max_jobs=1, exclude_umos={C}), [queued_a])
        self.assertEqual(self.store.list_pending(), [queued_c])
        self.assertEqual(self.store.pop_due(max_jobs=0), [])
        self.assertEqual(self.store.pop_due(max_jobs=1), [dict(queued_c, capacity_wait_seconds=0)])
        self.assertEqual({x["job"]["id"] for x in self.open().list_claimed()},
                         {a["id"], b["id"], c["id"]})

    def test_same_session_waits_for_finish_without_losing_new_pending(self):
        old = self.plan()
        self.clock[0] += 300
        self.assertEqual(self.store.pop_due(), [old])
        new = self.plan()
        self.clock[0] += 300
        self.assertEqual(self.store.pop_due(), [])
        queued = dict(new, capacity_queued_at=self.clock[0])
        self.assertEqual(self.store.list_pending(A), [queued])
        self.assertTrue(self.store.finish_claim(old["id"]))
        self.assertFalse(self.store.finish_claim(old["id"]))
        claimed = dict(queued, capacity_wait_seconds=0)
        self.assertEqual(self.store.pop_due(), [claimed])
        self.assertEqual(self.open().list_claimed(A)[0]["job"], claimed)

    def test_cancel_only_cancels_pending_and_never_forgets_claim(self):
        old = self.plan()
        self.clock[0] += 300
        self.store.pop_due()
        pending = self.plan()
        self.assertTrue(self.store.cancel(A, pending["id"]))
        self.assertFalse(self.store.cancel(A, old["id"]))
        self.assertEqual(self.open().list_claimed(A)[0]["job"], old)

    def test_finish_write_failure_retains_durable_claim(self):
        job = self.plan()
        self.clock[0] += 300
        self.store.pop_due()
        original = self.path.read_bytes()
        with patch.object(self.store, "_commit", side_effect=OSError("disk write failed")):
            with self.assertRaises(OSError):
                self.store.finish_claim(job["id"])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.open().list_claimed()[0]["job"], job)

    def test_claim_listing_is_copied_and_filtered(self):
        a, b = self.plan(A), self.plan(B)
        self.clock[0] += 300
        self.store.pop_due()
        rows = self.store.list_claimed(A)
        rows[0]["job"]["reason"] = "外部修改不应写进状态"
        rows.clear()
        self.assertEqual(self.store.list_claimed(A)[0]["job"], a)
        self.assertEqual(self.store.list_claimed(B)[0]["job"], b)

    def test_zero_capacity_keeps_eligible_but_still_discards_expired(self):
        expired = self.plan(A, 300)
        eligible = self.plan(B, 420)
        self.clock[0] += 421
        self.assertEqual(self.store.pop_due(max_jobs=0), [])
        self.assertEqual(self.store.list_pending(),
                         [dict(eligible, capacity_queued_at=self.clock[0])])
        self.assertEqual(self.store.list_claimed(), [])
        self.assertEqual(self.store.drain_discarded(), [{"job": expired, "reason": "expired"}])

    def test_invalid_claim_arguments_do_not_mutate_state(self):
        self.plan()
        original = self.path.read_bytes()
        for capacity in (-1, True, "1", 1.2):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                self.store.pop_due(max_jobs=capacity)
        with self.assertRaises(ValueError):
            self.store.pop_due(exclude_umos=A)
        self.assertEqual(self.path.read_bytes(), original)

    def test_corrupt_claim_is_rejected_without_repairing_file(self):
        job = self.plan()
        self.clock[0] += 300
        self.store.pop_due()
        state = json.loads(self.path.read_text(encoding="utf-8"))
        state["claimed"][job["id"]]["claimed_at"] = "not a timestamp"
        self.path.write_text(json.dumps(state), encoding="utf-8")
        original = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.open()
        self.assertEqual(self.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
