"""Real durable claims and runtime workers; no model, network or QQ messages."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import test_runtime as rt


class ConcurrentSchedulerTests(unittest.IsolatedAsyncioTestCase):
    setUp = rt.RuntimeIntegrationTests.setUp

    async def pending(self, umos):
        self.turn.config["proactive_all_groups"] = True
        jobs = []
        for umo in umos:
            key = await self.runtime.conversation_key(umo)
            jobs.append(self.runtime.store.schedule(umo, 300, "不会进入结果日志的具体话题", key))
        self.clock[0] += 300
        self.context.get_config = lambda **kw: {"plugin_set": ["*"]}
        return jobs

    def pipeline(self, handler):
        self.runtime.pipeline = AsyncMock(return_value=SimpleNamespace(execute=handler))

    def silent(self, event):
        self.runtime.response(event, SimpleNamespace(completion_text="[[SILENT]]", result_chain=None))

    async def settle(self):
        tasks = [worker["task"] for worker in self.runtime._workers.values()]
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)

    async def test_slow_session_does_not_block_second_session_or_make_it_expire(self):
        jobs = await self.pending((rt.UMO, rt.OTHER_UMO))
        release = asyncio.Event()
        second_started = asyncio.Event()

        async def handler(event):
            if event.unified_msg_origin == rt.UMO:
                await release.wait()
            else:
                second_started.set()
            self.silent(event)

        self.pipeline(handler)
        self.runtime.poll_once()
        self.addAsyncCleanup(self.runtime.terminate)
        await asyncio.wait_for(second_started.wait(), 1)
        self.assertFalse(release.is_set())
        self.clock[0] += 130  # A still has not completed; B already ran on time.
        release.set()
        await self.settle()
        rows = {row["job_id"]: row for row in self.runtime.outcome_records()}
        self.assertEqual(rows[jobs[1]["id"]]["outcome"], "silent")
        self.assertNotEqual(rows[jobs[1]["id"]]["reason"], "late_due_job")

    async def test_parallelism_is_bounded_and_extra_jobs_remain_pending(self):
        umos = ["default:GroupMessage:" + str(1000 + i) for i in range(6)]
        await self.pending(umos)
        release = asyncio.Event()
        started = []

        async def handler(event):
            started.append(event.unified_msg_origin)
            await release.wait()
            self.silent(event)

        self.pipeline(handler)
        self.runtime.poll_once()
        self.addAsyncCleanup(self.runtime.terminate)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(len(self.runtime._workers), 4)
        self.assertEqual(len(started), 4)
        self.assertEqual(len(self.runtime.store.list_claimed()), 4)
        self.assertEqual(len(self.runtime.store.list_pending()), 2)
        release.set()
        await self.settle()
        self.runtime.poll_once()
        await self.settle()
        self.assertEqual(len(started), 6)
        self.assertEqual(self.runtime.store.list_claimed(), [])

    async def test_same_session_waits_for_existing_worker_to_finish(self):
        [first] = await self.pending((rt.UMO,))
        release = asyncio.Event()
        order = []

        async def handler(event):
            order.append(("start", event.job["id"]))
            if event.job["id"] == first["id"]:
                await release.wait()
            order.append(("end", event.job["id"]))
            self.silent(event)

        self.pipeline(handler)
        self.runtime.poll_once()
        self.addAsyncCleanup(self.runtime.terminate)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        second = self.runtime.store.schedule(rt.UMO, 300, "新的具体话题", first["conversation_id"])
        self.clock[0] += 300
        self.runtime.poll_once()
        self.assertEqual([job["id"] for job in self.runtime.store.list_pending()], [second["id"]])
        self.assertEqual(len(self.runtime._workers), 1)
        release.set()
        await self.settle()
        self.runtime.poll_once()
        await self.settle()
        self.assertEqual(order, [("start", first["id"]), ("end", first["id"]),
                                 ("start", second["id"]), ("end", second["id"])])

    async def test_shutdown_accounts_for_claimed_workers_before_they_start(self):
        jobs = await self.pending((rt.UMO, rt.OTHER_UMO))
        self.pipeline(AsyncMock(side_effect=AssertionError("must not start")))
        self.runtime.poll_once()
        await self.runtime.terminate()
        self.runtime.pipeline.assert_not_awaited()
        rows = self.runtime.outcome_records()
        self.assertEqual({row["job_id"] for row in rows}, {job["id"] for job in jobs})
        self.assertTrue(all(row["outcome"] == "canceled" for row in rows))
        self.assertEqual(self.runtime.store.list_claimed(), [])
        self.assertEqual(self.runtime._workers, {})

    async def test_old_claim_recovers_as_unknown_interruption_without_replay(self):
        [job] = await self.pending((rt.UMO,))
        self.runtime.store.pop_due()
        restarted = rt.Runtime.InitiativeRuntime(self.turn, Path(self.folder.name))
        restarted.pipeline = AsyncMock(side_effect=AssertionError("must not replay"))
        restarted.recover_claims()
        restarted.poll_once()
        restarted.pipeline.assert_not_awaited()
        [row] = restarted.outcome_records()
        self.assertEqual((row["job_id"], row["outcome"], row["reason"]),
                         (job["id"], "interrupted", "restart_after_claim"))
        self.assertEqual(restarted.store.list_claimed(), [])
        status = json.loads(await restarted.tool(rt.Event(), "status"))
        self.assertEqual(status["recent_outcomes"][0]["outcome"], "interrupted")
        req = rt.ProviderRequest()
        restarted.request(rt.Event(), req)
        self.assertIn("未确认完整送达", req.extra_user_content_parts[-1].text)
        self.assertNotIn("具体话题", json.dumps(row, ensure_ascii=False))

    async def test_existing_durable_terminal_only_acks_after_restart(self):
        [job] = await self.pending((rt.UMO,))
        self.runtime.store.pop_due()
        self.runtime.store.finish_claim = lambda *_: (_ for _ in ()).throw(OSError("ack unavailable"))
        self.runtime.record_outcome(rt.UMO, job["id"], "silent", "model_chose_silence")
        self.assertEqual(len(self.runtime.store.list_claimed()), 1)
        restarted = rt.Runtime.InitiativeRuntime(self.turn, Path(self.folder.name))
        restarted.recover_claims()
        self.assertEqual(len(restarted.outcome_records()), 1)
        self.assertEqual(restarted.outcome_records()[0]["outcome"], "silent")
        self.assertEqual(restarted.store.list_claimed(), [])

    async def test_failed_outcome_write_keeps_claim_and_never_repeats_model(self):
        await self.pending((rt.UMO,))

        async def handler(event):
            self.silent(event)

        self.pipeline(handler)
        self.runtime.outcomes.record = lambda *a, **kw: (_ for _ in ()).throw(OSError("journal unavailable"))
        self.runtime.store.archive_claim = lambda *a, **kw: (_ for _ in ()).throw(OSError("receipt write unavailable"))
        self.runtime.poll_once()
        self.addAsyncCleanup(self.runtime.terminate)
        await self.settle()
        self.runtime.poll_once()
        self.assertEqual(self.runtime.pipeline.await_count, 1)
        self.assertEqual(len(self.runtime.store.list_claimed()), 1)
        self.assertFalse(self.runtime.status()["outcome_journal_persistent"])
        self.assertEqual(self.runtime.outcome_records()[0]["outcome"], "silent")

    async def test_ack_failure_retries_ack_without_duplicate_result_or_generation(self):
        await self.pending((rt.UMO,))

        async def handler(event):
            self.silent(event)

        self.pipeline(handler)
        original = self.runtime.store.finish_claim
        self.runtime.store.finish_claim = lambda *_: (_ for _ in ()).throw(OSError("ack unavailable"))
        self.runtime.poll_once()
        self.addAsyncCleanup(self.runtime.terminate)
        await self.settle()
        self.assertEqual(len(self.runtime.store.list_claimed()), 1)
        self.runtime.store.finish_claim = original
        self.runtime.poll_once()
        self.assertEqual(self.runtime.store.list_claimed(), [])
        self.assertEqual(self.runtime.pipeline.await_count, 1)
        self.assertEqual(len(self.runtime.outcome_records()), 1)


if __name__ == "__main__":
    unittest.main()
