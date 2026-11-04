"""Delivery acknowledgements and failures, without model or network calls."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_runtime as rt


class DeliveryOutcomeTests(unittest.IsolatedAsyncioTestCase):
    setUp = rt.RuntimeIntegrationTests.setUp

    async def job(self):
        key = await self.runtime.conversation_key(rt.UMO)
        self.runtime.store.schedule(rt.UMO, 300, "私有旧话题不应该进入投递日志", key)
        self.clock[0] += 300
        return self.runtime.store.pop_due()[0]

    def prepare(self, event, text="[[SPEAK]]这句生成的正文不应该进日志"):
        event.plugins_name = ["astrbot_plugin_turn_taking", "astrbot_plugin_iris_chat_memory"]
        self.runtime.request(event, rt.ProviderRequest())
        response = SimpleNamespace(completion_text=text, result_chain=None)
        self.runtime.response(event, response)
        event.result = SimpleNamespace(chain=[rt.Plain(response.completion_text)])
        return response

    async def execute(self, job, handler):
        captured = []

        async def run(event):
            captured.append(event)
            await handler(event)

        self.context.get_config = lambda **kw: {"plugin_set": ["*"]}
        self.runtime.pipeline = AsyncMock(return_value=SimpleNamespace(execute=run))
        await self.runtime.run_due(job)
        return captured

    def assert_no_assistant_memory(self):
        self.assertFalse(any(row["role"] == "assistant" for row in self.runtime.store.recent(rt.UMO)))
        self.iris_response.assert_not_awaited()

    async def test_native_no_ack_stops_history_and_records_failed(self):
        job = await self.job()
        native_history = []

        async def handler(event):
            self.prepare(event)
            with patch.object(rt.CronMessageEvent, "send", AsyncMock(return_value=None)):
                await event.send("生成正文")
            await self.turn.mark_replied(event)
            if not event.stopped:  # The native history gate follows RespondStage.
                native_history.append("must-not-save")

        [event] = await self.execute(job, handler)
        self.assertTrue(event.stopped)
        self.assertFalse(event._has_send_oper)
        self.assertEqual(native_history, [])
        self.assert_no_assistant_memory()
        row = self.runtime.outcome_records()[0]
        self.assertEqual((row["outcome"], row["reason"]), ("failed", "no_ack"))
        self.assertNotIn("正文", json.dumps(row, ensure_ascii=False))
        self.assertNotIn("私有旧话题", json.dumps(row, ensure_ascii=False))

    async def test_native_exception_is_metadata_only_without_public_error_or_retry(self):
        job = await self.job()

        async def handler(event):
            self.prepare(event)
            native = AsyncMock(side_effect=RuntimeError("sk-private-secret generated-user-text"))
            with patch.object(rt.CronMessageEvent, "send", native):
                await event.send("生成正文")
                await event.send("不可重试的第二次")
            self.assertEqual(native.await_count, 1)
            self.assertTrue(event.stopped)

        await self.execute(job, handler)
        self.assert_no_assistant_memory()
        record = self.runtime.outcome_records()[0]
        self.assertEqual(record["reason"], "send_exception_RuntimeError")
        self.assertNotIn("sk-private", json.dumps(record))

    async def test_partial_delivery_does_not_store_full_reply_or_retry_remaining_parts(self):
        job = await self.job()
        delivered = []

        async def native(event, message):
            if not delivered:
                delivered.append(message)
                event._has_send_oper = True
            # Second delivery returns without marking success, like no platform.

        async def handler(event):
            self.prepare(event, "[[SPEAK]]完整正文包含三个片段")
            with patch.object(rt.CronMessageEvent, "send", native):
                await event.send("first-part")
                await event.send("second-part")
                await event.send("third-part")
            await self.turn.mark_replied(event)

        [event] = await self.execute(job, handler)
        self.assertEqual(delivered, ["first-part"])
        self.assertEqual((event.delivery_attempts, event.delivered_parts, event.failed_parts), (2, 1, 1))
        self.assertTrue(event._has_send_oper)
        self.assertTrue(event.stopped)
        self.assert_no_assistant_memory()
        self.assertEqual(len(self.runtime.outcome_records()), 1)
        self.assertEqual(self.runtime.outcome_records()[0]["outcome"], "partial")
        self.assertEqual(self.runtime.outcome_records()[0]["delivered_parts"], 1)

    async def test_success_has_one_persistent_sent_result_and_records_iris_once(self):
        job = await self.job()

        async def handler(event):
            response = self.prepare(event, "[[SPEAK]]实际交付的正常正文")
            await event.send(response.completion_text)
            await self.turn.mark_replied(event)

        [event] = await self.execute(job, handler)
        self.assertFalse(event.stopped)
        self.assertEqual(self.runtime.outcome_records()[0]["outcome"], "sent")
        self.assertEqual(self.runtime.outcome_records()[0]["delivered_parts"], 1)
        self.assertEqual(len(self.runtime.outcome_records()), 1)
        self.iris_response.assert_awaited_once()
        self.assertEqual(self.runtime.store.recent(rt.UMO)[-1]["text"], "实际交付的正常正文")

    async def test_silence_is_a_final_result_without_assistant_memory(self):
        job = await self.job()

        async def handler(event):
            self.prepare(event, "[[SILENT]]")

        await self.execute(job, handler)
        self.assertEqual(self.runtime.outcome_records()[0]["outcome"], "silent")
        self.assert_no_assistant_memory()

    async def test_context_change_during_final_conversation_lookup_blocks_network(self):
        job = await self.job()
        event = rt.Runtime.InitiativeEvent(self.runtime, job, 0)
        event.started_at = self.clock[0]
        event.approved = True

        async def lookup(_umo):
            await asyncio.sleep(0)
            self.runtime.observe(rt.Event("消息在await期间换了话题"))
            return job["conversation_id"]

        self.runtime.conversation_key = lookup
        native = AsyncMock()
        with patch.object(rt.CronMessageEvent, "send", native):
            await event.send("不应再发出的旧回复")
        self.runtime.finish_event(event)
        native.assert_not_awaited()
        self.assertEqual(self.runtime.outcome_records()[0]["outcome"], "stale")

    async def test_late_claim_is_expired_before_model_or_pipeline(self):
        job = await self.job()
        self.clock[0] += 121
        self.runtime.pipeline = AsyncMock(side_effect=AssertionError("expired job must not generate"))
        await self.runtime.run_due(job)
        self.runtime.pipeline.assert_not_awaited()
        self.assertEqual(self.runtime.outcome_records()[0]["outcome"], "expired")

    async def test_journal_survives_restart_and_failure_notice_is_temporary_and_scoped(self):
        self.runtime.record_outcome(rt.UMO, "job-123", "failed", "no_ack")
        restarted = rt.Runtime.InitiativeRuntime(self.turn, Path(self.folder.name))
        self.assertEqual(restarted.status()["last_outcome"]["reason"], "no_ack")
        status = json.loads(await restarted.tool(rt.Event(), "status"))
        self.assertEqual(status["recent_outcomes"][0]["reason"], "no_ack")
        req = rt.ProviderRequest(contexts=[{"role": "user", "content": "真实历史"}])
        restarted.request(rt.Event(), req)
        self.assertIn("未确认完整送达", req.extra_user_content_parts[-1].text)
        self.assertTrue(req.extra_user_content_parts[-1].temporary)
        self.assertEqual(req.contexts, [{"role": "user", "content": "真实历史"}])
        self.turn.config["proactive_all_groups"] = True
        other = rt.ProviderRequest()
        restarted.request(rt.Event(umo=rt.OTHER_UMO), other)
        self.assertNotIn("未确认完整送达", other.extra_user_content_parts[-1].text)

    async def test_bad_or_unwritable_journal_cannot_stop_normal_chat(self):
        path = Path(self.folder.name) / "self-initiative-outcomes.json"
        path.write_text("{corrupt-evidence", encoding="utf-8")
        degraded = rt.Runtime.InitiativeRuntime(self.turn, Path(self.folder.name))
        degraded.record_outcome(rt.UMO, "job-123", "error", "execution_TimeoutError")
        self.assertFalse(degraded.status()["outcome_journal_persistent"])
        self.assertEqual(path.read_text(encoding="utf-8"), "{corrupt-evidence")
        event = rt.Event()
        event._has_send_oper = True
        await degraded.sent(event)
        self.assertEqual(degraded.store.recent(rt.UMO)[-1]["role"], "assistant")
        self.runtime.outcomes.record = lambda *a, **kw: (_ for _ in ()).throw(OSError("disk unavailable"))
        self.runtime.record_outcome(rt.UMO, "job-456", "failed", "no_ack")
        self.assertEqual(self.runtime.outcome_records()[0]["reason"], "no_ack")
        self.assertFalse(self.runtime.status()["outcome_journal_persistent"])

    async def test_discarded_store_jobs_are_recorded_without_generation(self):
        key = await self.runtime.conversation_key(rt.UMO)
        self.runtime.store.schedule(rt.UMO, 300, "不会进入诊断的旧内容", key)
        self.clock[0] += 421
        self.runtime.pipeline = AsyncMock(side_effect=AssertionError("discarded job must not run"))

        async def stop_after_one_tick(_seconds):
            self.runtime.closed = True

        with patch.object(rt.Runtime.asyncio, "sleep", stop_after_one_tick):
            await self.runtime.loop()
        self.runtime.pipeline.assert_not_awaited()
        self.assertEqual(self.runtime.outcome_records()[0]["outcome"], "expired")
        self.assertNotIn("旧内容", json.dumps(self.runtime.outcome_records(), ensure_ascii=False))

    async def test_shutdown_records_already_claimed_remaining_jobs_as_canceled(self):
        self.turn.config["proactive_all_groups"] = True
        for umo in (rt.UMO, rt.OTHER_UMO):
            key = await self.runtime.conversation_key(umo)
            self.runtime.store.schedule(umo, 300, "不存入诊断的私人意图", key)
        self.clock[0] += 300
        self.context.get_config = lambda **kw: {"plugin_set": ["*"]}

        async def canceled(_event):
            raise asyncio.CancelledError()

        self.runtime.pipeline = AsyncMock(return_value=SimpleNamespace(execute=canceled))
        # Jobs are now independently registered workers; canceling one worker
        # must not cancel the polling loop or leave another claim unaccounted.
        self.runtime.poll_once()
        await self.runtime.terminate()
        rows = self.runtime.outcome_records()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["outcome"] == "canceled" for row in rows))
        self.assertEqual({row["reason"] for row in rows}, {"worker_stopped_before_execution"})
        self.assertEqual(self.runtime.store.list_claimed(), [])


if __name__ == "__main__":
    unittest.main()
