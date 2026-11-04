"""Production-size receipt retention and claim-cache failure boundaries."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_queue_runtime_recovery as queue_tests
import test_runtime as rt


class TerminalReceiptPressureTests(unittest.IsolatedAsyncioTestCase):
    setUp = rt.RuntimeIntegrationTests.setUp
    pending = queue_tests.QueueRuntimeRecoveryTests.pending

    async def test_actual_106_workers_keep_capacity_and_active_terminal_after_both_rings_evict(self):
        release = asyncio.Event()
        first_finished = asyncio.Event()
        executed = []
        first_umo = "default:GroupMessage:9900"

        async def handler(event):
            self.runtime.request(event, rt.ProviderRequest())
            executed.append(event.job["id"])
            self.runtime.response(event, SimpleNamespace(completion_text="[[SILENT]]", result_chain=None))
            if event.unified_msg_origin == first_umo:
                self.runtime.finish_event(event)
                first_finished.set()
                await release.wait()

        self.runtime.pipeline = AsyncMock(return_value=SimpleNamespace(execute=handler))
        self.addAsyncCleanup(self.runtime.terminate)
        with patch.object(rt.Runtime.OutcomeLog, "_commit", side_effect=OSError("journal unavailable")):
            [first] = await self.pending([first_umo])
            self.runtime.poll_once()
            await asyncio.wait_for(first_finished.wait(), 1)
            # The production bound is 100. Keep the first terminal worker alive
            # while 105 distinct sessions genuinely traverse the runtime.
            for number in range(105):
                umo = "default:GroupMessage:" + str(9901 + number)
                await self.pending([umo])
                self.runtime.poll_once()
                await asyncio.wait_for(self.runtime._workers[umo]["task"], 1)
                self.runtime.poll_once()
                self.assertEqual(self.runtime.store.list_claimed(), [])
            self.assertEqual(len(executed), 106)
            self.assertEqual(len(set(executed)), 106)
            self.assertEqual(len(self.runtime.store.list_terminal_receipts()), 100)
            self.assertEqual(self.runtime.store.terminal_receipts_dropped(), 6)
            self.assertFalse(any(row["job_id"] == first["id"]
                                 for row in self.runtime.store.list_terminal_receipts()))
            self.assertFalse(any(row["job_id"] == first["id"] for row in self.runtime._outcome_memory))
            self.assertEqual(self.runtime._claim_terminals, {})
            self.assertEqual(self.runtime._known_terminal(first["id"])["outcome"], "silent")
            release.set()
            await asyncio.wait_for(self.runtime._workers[first_umo]["task"], 1)
            self.runtime.poll_once()
            self.assertEqual(self.runtime.store.terminal_receipts_dropped(), 6)
            self.assertFalse(any(row["outcome"] == "error"
                                 for row in self.runtime.outcome_records(limit=100)))
            self.assertEqual(self.runtime.pipeline.await_count, 106)
        self.clock[0] += 30
        self.runtime.poll_once()
        self.assertEqual(self.runtime.store.list_terminal_receipts(), [])
        self.assertEqual(len(self.runtime.outcomes.records(limit=100)), 100)
        self.assertEqual(self.runtime.store.terminal_receipts_dropped(), 6)
        self.assertEqual(self.runtime.pipeline.await_count, 106)

    async def test_actual_100_claim_cache_prevents_diagnostic_cascade_and_preserves_original_rows(self):
        self.turn.config["proactive_all_groups"] = True
        jobs = [self.runtime.store.schedule("default:GroupMessage:" + str(12000 + number),
                                           300, "具体已谈话题", "same-conversation")
                for number in range(100)]
        self.clock[0] += 300
        self.assertEqual(len(self.runtime.store.pop_due()), 100)
        original_file = self.runtime.store.path.read_bytes()
        self.addAsyncCleanup(self.runtime.terminate)
        with patch.object(rt.Runtime.OutcomeLog, "_commit", side_effect=OSError("journal unavailable")):
            with patch.object(self.runtime.store, "archive_claim", side_effect=OSError("archive unavailable")):
                original_rows = {
                    job["id"]: self.runtime.record_outcome(job["umo"], job["id"],
                                                           "sent", "delivery_confirmed", 1)
                    for job in jobs
                }
                self.assertEqual(self.runtime._claim_terminals, original_rows)
                # One unrelated diagnostic used to evict a known result and
                # cascade into 100 newly fabricated unknown outcomes per retry.
                for number in range(3):
                    self.clock[0] += 1
                    self.runtime.record_outcome("default:GroupMessage:13001", "extra-" + str(number),
                                                "blocked", "claim_capacity_exhausted")
                    with patch.object(self.runtime, "record_outcome", wraps=self.runtime.record_outcome) as record:
                        self.runtime._retry_claim_acks()
                        record.assert_not_called()
                    self.assertEqual(self.runtime._claim_terminals, original_rows)
                    self.assertEqual(self.runtime._terminal_row(jobs[0]["id"]), original_rows[jobs[0]["id"]])
                self.assertFalse(any(row["job_id"] == jobs[0]["id"] for row in self.runtime._outcome_memory))
                self.assertEqual(self.runtime.store.path.read_bytes(), original_file)
                self.assertEqual(len(self.runtime.store.list_claimed()), 100)
            # Repair only the queue path first. Its atomic receipts preserve all
            # original IDs, times and sent acknowledgments, freeing every slot.
            self.runtime._retry_claim_acks()
            self.assertEqual(self.runtime.store.list_claimed(), [])
            self.assertEqual(self.runtime._claim_terminals, {})
            receipts = self.runtime.store.list_terminal_receipts()
            self.assertEqual({row["job_id"]: row for row in receipts}, original_rows)
            self.assertEqual(self.runtime.store.terminal_receipts_dropped(), 0)
        self.clock[0] += 30
        self.runtime.poll_once()
        self.assertEqual(self.runtime.store.list_terminal_receipts(), [])
        self.assertEqual(self.runtime._claim_terminals, {})
        persisted = self.runtime.outcomes.records(limit=100)
        self.assertEqual(len(persisted), 100)
        for row in persisted:
            if row["job_id"] in original_rows:
                self.assertEqual(row, original_rows[row["job_id"]])
        self.assertFalse(any(row["outcome"] == "interrupted" for row in persisted))


if __name__ == "__main__":
    unittest.main()
