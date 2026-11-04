"""Capacity wait and recoverable metadata faults; no network/model/delivery."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_runtime as rt


class QueueRuntimeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        rt.RuntimeIntegrationTests.setUp(self)
        # These regressions target the last-resort local release: both the
        # canonical journal and the atomic receipt write are unavailable.
        archived = patch.object(self.runtime.store, "archive_claim",
                                side_effect=OSError("receipt write unavailable"))
        archived.start()
        self.addCleanup(archived.stop)

    async def pending(self, umos):
        self.turn.config["proactive_all_groups"] = True
        jobs = []
        for umo in umos:
            key = await self.runtime.conversation_key(umo)
            jobs.append(self.runtime.store.schedule(umo, 300, "有界的真实话题", key))
        self.clock[0] += 300
        self.context.get_config = lambda **kw: {"plugin_set": ["*"]}
        return jobs

    def use_pipeline(self, handler):
        self.runtime.pipeline = AsyncMock(return_value=SimpleNamespace(execute=handler))

    def silent(self, event):
        self.runtime.response(event, SimpleNamespace(completion_text="[[SILENT]]", result_chain=None))

    async def settle(self, runtime=None):
        runtime = runtime or self.runtime
        tasks = [worker["task"] for worker in runtime._workers.values()]
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)

    async def test_fifth_session_waits_180_seconds_then_enters_provider_with_credit(self):
        umos = ["default:GroupMessage:" + str(7000 + index) for index in range(5)]
        jobs = await self.pending(umos)
        release = asyncio.Event()
        started = []

        async def handler(event):
            self.runtime.request(event, rt.ProviderRequest())
            if event.stopped:
                return
            started.append(event.unified_msg_origin)
            if event.unified_msg_origin != umos[-1]:
                await release.wait()
            self.silent(event)

        self.use_pipeline(handler)
        self.runtime.poll_once()
        self.addAsyncCleanup(self.runtime.terminate)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(started, umos[:4])
        self.clock[0] += 180
        self.runtime.poll_once()
        status = json.loads(await self.runtime.tool(rt.Event(umo=umos[-1]), "status"))
        self.assertEqual(status["capacity_wait_limit_seconds"], 600)
        self.assertEqual(status["pending"][0]["capacity_wait_seconds"], 180)
        self.assertTrue(all(row["umo"] == umos[-1] for row in status["pending"]))
        release.set()
        await self.settle()
        self.runtime.poll_once()
        await self.settle()
        self.assertEqual(started, umos)
        row = next(row for row in self.runtime.outcome_records() if row["job_id"] == jobs[-1]["id"])
        self.assertEqual((row["outcome"], row["reason"]), ("silent", "model_chose_silence"))

    async def test_queue_timeout_is_recorded_as_expired_without_model(self):
        [job] = await self.pending((rt.UMO,))
        self.runtime.store.pop_due(max_jobs=0)
        self.clock[0] += 601
        self.use_pipeline(AsyncMock(side_effect=AssertionError("expired queue must not execute")))
        self.runtime.poll_once()
        self.runtime.pipeline.assert_not_awaited()
        row = self.runtime.outcome_records()[0]
        self.assertEqual((row["job_id"], row["outcome"], row["reason"]),
                         (job["id"], "expired", "queue_timeout"))

    async def test_failed_journal_releases_only_local_barrier_and_allows_next_plan(self):
        [first] = await self.pending((rt.UMO,))

        async def handler(event):
            self.silent(event)

        self.use_pipeline(handler)
        with patch.object(rt.Runtime.OutcomeLog, "_commit", side_effect=OSError("journal unavailable")):
            self.runtime.poll_once()
            self.addAsyncCleanup(self.runtime.terminate)
            await self.settle()
            self.runtime.poll_once()
            self.assertFalse(self.runtime.has_pending(rt.UMO))
            self.assertTrue(self.runtime.store.is_claim_locally_released(first["id"]))
            self.assertEqual(len(self.runtime.store.list_claimed()), 1)
            self.assertEqual(self.runtime.store.list_blocking_claimed(), [])
            disk = json.loads(self.runtime.store.path.read_text(encoding="utf-8"))
            self.assertIn(first["id"], disk["claimed"])
            second = self.runtime.store.schedule(rt.UMO, 300, "新的本轮话题", first["conversation_id"])
            self.clock[0] += 300
            self.runtime.poll_once()
            await self.settle()
            self.runtime.poll_once()
            self.assertEqual(self.runtime.pipeline.await_count, 2)
            self.assertEqual({row["job_id"] for row in self.runtime.outcome_records()},
                             {first["id"], second["id"]})
            self.assertEqual(self.runtime.status()["awaiting_journal_count"], 2)

    async def test_repair_flushes_original_ids_times_then_acks_without_replay(self):
        [job] = await self.pending((rt.UMO,))
        self.use_pipeline(lambda event: None)

        async def handler(event):
            self.silent(event)

        self.use_pipeline(handler)
        with patch.object(rt.Runtime.OutcomeLog, "_commit", side_effect=OSError("journal unavailable")):
            self.runtime.poll_once()
            self.addAsyncCleanup(self.runtime.terminate)
            await self.settle()
        original = dict(self.runtime.outcome_records()[0])
        self.clock[0] += 29
        self.runtime.poll_once()
        self.assertFalse(self.runtime.status()["outcome_journal_persistent"])
        self.assertEqual(len(self.runtime.store.list_claimed()), 1)
        self.clock[0] += 1
        self.runtime.poll_once()
        self.assertTrue(self.runtime.status()["outcome_journal_persistent"])
        self.assertEqual(self.runtime.store.list_claimed(), [])
        self.assertEqual(self.runtime.outcomes.last(), original)
        self.clock[0] += 30
        self.runtime.poll_once()
        self.assertEqual(len(self.runtime.outcomes.records()), 1)
        self.assertEqual(self.runtime.pipeline.await_count, 1)
        self.assertEqual(original["job_id"], job["id"])

    async def test_corrupt_journal_is_not_overwritten_and_restart_never_replays(self):
        [job] = await self.pending((rt.UMO,))
        self.runtime.store.pop_due()
        path = Path(self.folder.name) / "self-initiative-outcomes.json"
        path.write_bytes(b"{corrupt original evidence")
        restarted = rt.Runtime.InitiativeRuntime(self.turn, Path(self.folder.name))
        restarted.store.now = lambda: self.clock[0]
        restarted.pipeline = AsyncMock(side_effect=AssertionError("unknown delivery must not replay"))
        receipt_fault = patch.object(restarted.store, "archive_claim",
                                     side_effect=OSError("receipt write unavailable"))
        receipt_fault.start()
        self.addCleanup(receipt_fault.stop)
        restarted.recover_claims()
        self.clock[0] += 30
        restarted.poll_once()
        self.assertEqual(path.read_bytes(), b"{corrupt original evidence")
        self.assertEqual(len(restarted.store.list_claimed()), 1)
        self.assertFalse(restarted.has_pending(rt.UMO))
        self.assertEqual(restarted.outcome_records()[0]["outcome"], "interrupted")
        self.assertEqual(restarted.outcome_records()[0]["job_id"], job["id"])
        path.write_text('{"version":1,"records":[]}', encoding="utf-8")
        self.clock[0] += 30
        restarted.poll_once()
        restarted.pipeline.assert_not_awaited()
        self.assertTrue(restarted.status()["outcome_journal_persistent"])
        self.assertEqual(restarted.store.list_claimed(), [])
        self.assertEqual(restarted.outcomes.last()["outcome"], "interrupted")

    async def test_local_release_does_not_allow_same_session_while_worker_is_active(self):
        [first] = await self.pending((rt.UMO,))
        release = asyncio.Event()
        finished_metadata = asyncio.Event()
        order = []

        async def handler(event):
            order.append(event.job["id"])
            self.silent(event)
            if event.job["id"] == first["id"]:
                self.runtime.finish_event(event)
                finished_metadata.set()
                await release.wait()

        self.use_pipeline(handler)
        with patch.object(rt.Runtime.OutcomeLog, "_commit", side_effect=OSError("journal unavailable")):
            self.runtime.poll_once()
            self.addAsyncCleanup(self.runtime.terminate)
            await asyncio.wait_for(finished_metadata.wait(), 1)
            self.assertTrue(self.runtime.store.is_claim_locally_released(first["id"]))
            self.assertTrue(self.runtime.has_pending(rt.UMO))
            second = self.runtime.store.schedule(rt.UMO, 300, "第二个本会话话题", first["conversation_id"])
            self.clock[0] += 300
            self.runtime.poll_once()
            self.assertEqual(order, [first["id"]])
            self.assertEqual(len(self.runtime._workers), 1)
            release.set()
            await self.settle()
            self.runtime.poll_once()
            await self.settle()
            self.assertEqual(order, [first["id"], second["id"]])

    async def test_evicted_memory_terminal_remains_traceable_after_journal_repair(self):
        [job] = await self.pending((rt.UMO,))

        async def handler(event):
            self.silent(event)

        self.use_pipeline(handler)
        with patch.object(rt.Runtime.OutcomeLog, "_commit", side_effect=OSError("journal unavailable")):
            self.runtime.poll_once()
            self.addAsyncCleanup(self.runtime.terminate)
            await self.settle()
            original = dict(self.runtime.outcome_records(umo=rt.UMO, limit=1)[0])
            for index in range(101):
                self.runtime.record_outcome(rt.OTHER_UMO, "other-" + str(index), "silent", "model_chose_silence")
        self.assertFalse(any(row["job_id"] == job["id"] for row in self.runtime._outcome_memory))
        self.assertEqual(self.runtime.outcome_records(umo=rt.UMO, limit=1), [original])
        self.assertTrue(self.runtime.store.is_claim_locally_released(job["id"]))
        self.clock[0] += 30
        self.runtime.poll_once()
        self.assertEqual(self.runtime.store.list_claimed(), [])
        row = next(row for row in self.runtime.outcomes.records(limit=100) if row["job_id"] == job["id"])
        self.assertEqual(row, original)
        self.assertNotIn(job["id"], self.runtime._claim_terminals)
        self.assertEqual(self.runtime.pipeline.await_count, 1)

    async def test_evicted_durable_terminal_is_acked_after_claim_store_repair_without_replay(self):
        [job] = await self.pending((rt.UMO,))

        async def handler(event):
            self.silent(event)

        self.use_pipeline(handler)
        with patch.object(self.runtime.store, "finish_claim", side_effect=OSError("claim ack unavailable")):
            self.runtime.poll_once()
            self.addAsyncCleanup(self.runtime.terminate)
            await self.settle()
            original = dict(self.runtime.outcome_records(umo=rt.UMO, limit=1)[0])
            for index in range(101):
                self.runtime.record_outcome(rt.OTHER_UMO, "other-" + str(index), "silent", "model_chose_silence")
        self.assertIsNone(self.runtime._outcome_memory)
        self.assertIsNone(self.runtime._durable_terminal(job["id"]))
        self.assertTrue(self.runtime.store.is_claim_locally_released(job["id"]))
        disk = json.loads(self.runtime.store.path.read_text(encoding="utf-8"))
        self.assertIn(job["id"], disk["claimed"])
        self.runtime.poll_once()
        self.assertEqual(self.runtime.store.list_claimed(), [])
        row = self.runtime.outcomes.last()
        self.assertEqual(row, original)
        self.assertNotIn(job["id"], self.runtime._claim_terminals)
        self.runtime.poll_once()
        self.assertEqual(self.runtime.outcomes.last(), row)
        self.assertEqual(self.runtime.pipeline.await_count, 1)

    async def test_evicted_durable_terminal_is_not_replaced_while_worker_active(self):
        [job] = await self.pending((rt.UMO,))
        release = asyncio.Event()
        finished_metadata = asyncio.Event()

        async def handler(event):
            self.silent(event)
            self.runtime.finish_event(event)
            finished_metadata.set()
            await release.wait()

        self.use_pipeline(handler)
        with patch.object(self.runtime.store, "finish_claim", side_effect=OSError("claim ack unavailable")):
            self.runtime.poll_once()
            self.addAsyncCleanup(self.runtime.terminate)
            await asyncio.wait_for(finished_metadata.wait(), 1)
            for index in range(101):
                self.runtime.record_outcome(rt.OTHER_UMO, "other-" + str(index), "silent", "model_chose_silence")
        self.assertIsNone(self.runtime._outcome_memory)
        self.assertIsNone(self.runtime._durable_terminal(job["id"]))
        self.assertTrue(self.runtime.store.is_claim_locally_released(job["id"]))
        original = dict(self.runtime._claim_terminals[job["id"]])
        self.runtime.poll_once()
        # The known terminal may be durably restored and acked while the
        # coroutine winds down. Its live worker still serializes the session.
        self.assertEqual(self.runtime.store.list_claimed(), [])
        self.assertEqual(self.runtime._durable_terminal(job["id"]), original)
        self.assertTrue(self.runtime.has_pending(rt.UMO))
        release.set()
        await self.settle()
        self.runtime.poll_once()
        self.assertEqual(self.runtime.store.list_claimed(), [])
        row = self.runtime.outcomes.last()
        self.assertEqual(row, original)
        self.assertEqual(self.runtime.pipeline.await_count, 1)


if __name__ == "__main__":
    unittest.main()
