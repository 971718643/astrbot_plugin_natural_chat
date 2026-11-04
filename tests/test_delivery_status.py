"""Real normal hooks and autonomous workers; observations never send anything."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_runtime as rt


class DeliveryStatusTests(unittest.IsolatedAsyncioTestCase):
    setUp = rt.RuntimeIntegrationTests.setUp

    @staticmethod
    def text(req):
        return "".join(part.text for part in req.extra_user_content_parts)

    def ordinary(self, text="普通聊天", umo=rt.UMO):
        event = rt.Event(text, umo=umo, addressed=True)
        self.runtime.waiting(event)
        req = rt.ProviderRequest(prompt=text)
        self.runtime.request(event, req)
        return event, req

    async def test_ordinary_hooks_processing_preparing_and_api_success_without_read_claim(self):
        quota = deepcopy(self.turn.proactive_policy.history)
        files = list(Path(self.folder.name).iterdir())
        event, req = self.ordinary("私人原文sk-secret")
        current = self.runtime.delivery_status(rt.UMO)["current"]
        self.assertEqual(current["phase"], "processing")
        self.assertNotIn("私人原文", json.dumps(self.runtime.delivery_status(), ensure_ascii=False))
        self.assertNotIn("【内部投递状态】", self.text(req))  # Own processing is excluded.
        self.runtime.response(event, SimpleNamespace(completion_text="回复中的秘密正文"))
        self.assertEqual(self.runtime.delivery_status(rt.UMO)["current"]["phase"], "preparing")
        self.assertEqual(list(Path(self.folder.name).iterdir()), files)
        self.assertEqual(self.runtime.last_outcome, None)
        event._has_send_oper = True
        await self.turn.mark_replied(event)
        status = self.runtime.delivery_status(rt.UMO)
        self.assertEqual(status["current"]["phase"], "api_accepted")
        self.assertEqual(status["active"], [])
        self.assertEqual((status["remote_delivery"], status["read"]), ("unknown", "unknown"))
        self.assertFalse(status["ordinary_full_reply_confirmation"])
        self.assertEqual(self.turn.proactive_policy.history, quota)
        self.assertNotIn("秘密正文", json.dumps(status, ensure_ascii=False))

    async def test_no_success_hook_ages_to_unconfirmed_not_failed_or_blocked(self):
        event, _ = self.ordinary()
        self.runtime.response(event, SimpleNamespace(completion_text="转语音中"))
        await self.turn.mark_replied(event)  # No native send acknowledgement.
        self.clock[0] += 600
        self.assertEqual(self.runtime.delivery_status(rt.UMO)["current"]["phase"], "preparing")
        self.clock[0] += 1
        current = self.runtime.delivery_status(rt.UMO)["current"]
        self.assertEqual((current["phase"], current["reason"]), ("unconfirmed", "success_hook_not_observed"))
        self.assertEqual(self.runtime.outcome_records(), [])
        event._has_send_oper = True  # A late real hook may settle a prior uncertainty.
        await self.turn.mark_replied(event)
        self.assertEqual(self.runtime.delivery_status(rt.UMO)["current"]["phase"], "api_accepted")

    async def test_timeout_does_not_hide_more_recent_confirmed_reply(self):
        self.ordinary()
        self.clock[0] += 500
        recent, _ = self.ordinary()
        recent._has_send_oper = True
        await self.turn.mark_replied(recent)
        self.clock[0] += 101
        status = self.runtime.delivery_status(rt.UMO)
        self.assertEqual(status["current"]["phase"], "api_accepted")
        self.assertEqual(len(self.runtime.delivery_observations.rows), 2)

    async def test_tool_round_and_repeated_hooks_do_not_rewind_success(self):
        event, _ = self.ordinary()
        self.runtime.response(event, SimpleNamespace(completion_text="先查一下", tool_calls=[{}]))
        self.assertEqual(self.runtime.delivery_status(rt.UMO)["current"]["phase"], "processing")
        self.runtime.response(event, SimpleNamespace(completion_text="最终回复"))
        event._has_send_oper = True
        await self.turn.mark_replied(event)
        before = self.runtime.delivery_status(rt.UMO)
        self.clock[0] += 5
        self.runtime.request(event, rt.ProviderRequest())
        self.runtime.response(event, SimpleNamespace(completion_text="重复响应"))
        await self.turn.mark_replied(event)
        self.assertEqual(self.runtime.delivery_status(rt.UMO), before)

    async def test_ring_is_bounded_process_local_and_does_not_reuse_message_ids(self):
        for _ in range(105):
            self.ordinary()
        status = self.runtime.delivery_status()
        self.assertEqual(len(status["active"]), 100)
        self.assertEqual(status["ordinary_observations_dropped"], 5)
        self.assertEqual(len({row["id"] for row in status["active"]}), 100)
        status["active"][0]["phase"] = "external mutation"
        self.assertTrue(all(row["phase"] == "processing" for row in self.runtime.delivery_status()["active"]))
        restart = rt.Runtime.InitiativeRuntime(self.turn, Path(self.folder.name))
        self.assertIsNone(restart.delivery_status()["current"])

    async def test_status_tool_request_hints_are_scoped_and_prefer_existing_inflight_reply(self):
        self.turn.config["proactive_all_groups"] = True
        self.runtime.record_outcome(rt.UMO, "old-result", "sent", "delivery_confirmed")
        self.clock[0] += 1
        prior, _ = self.ordinary()
        self.runtime.response(prior, SimpleNamespace(completion_text="正在准备语音"))
        self.clock[0] += 1
        current, req = self.ordinary()
        self.assertIn("先前普通回复：准备回复", self.text(req))
        status = json.loads(await self.runtime.tool(current, "status"))["delivery"]
        self.assertEqual(status["current"]["phase"], "preparing")
        other, req = self.ordinary(umo=rt.OTHER_UMO)
        status = json.loads(await self.runtime.tool(other, "status"))["delivery"]
        self.assertIsNone(status["current"])
        self.assertNotIn(rt.UMO, json.dumps(status))
        self.assertNotIn("【内部投递状态】", self.text(req))

    async def test_final_outcome_mapping_preserves_canonical_schema_and_uncertainty(self):
        cases = (("sent", "delivery_confirmed", "api_accepted"),
                 ("failed", "no_ack", "unconfirmed"),
                 ("failed", "send_exception_RuntimeError", "unconfirmed"),
                 ("error", "execution_TimeoutError", "unconfirmed"),
                 ("partial", "no_ack", "partially_accepted"),
                 ("interrupted", "restart_after_claim", "unconfirmed"),
                 ("canceled", "send_canceled", "unconfirmed"),
                 ("blocked", "quiet_hours", "locally_blocked"),
                 ("silent", "model_chose_silence", "silent"))
        for index, (outcome, reason, expected) in enumerate(cases):
            self.clock[0] += 1
            row = self.runtime.record_outcome(rt.UMO, "job-" + str(index), outcome, reason)
            self.assertEqual(set(row), {"id", "umo", "job_id", "at", "outcome", "reason", "delivered_parts"})
            self.assertEqual(self.runtime.delivery_status(rt.UMO)["current"]["phase"], expected)
        self.assertEqual(len(self.runtime.outcome_records()), len(cases))

    async def test_real_autonomous_worker_exposes_all_phases_and_keeps_last_outcome_terminal(self):
        self.context.get_config = lambda **kw: {"plugin_set": ["*"]}
        key = await self.runtime.conversation_key(rt.UMO)
        self.runtime.store.schedule(rt.UMO, 300, "电影结尾有可用的新想法", key)
        self.clock[0] += 300
        generation = asyncio.Event()
        permit_response = asyncio.Event()
        sending = asyncio.Event()
        permit_send = asyncio.Event()

        async def native(event, _message):
            self.assertEqual(self.runtime.delivery_status(rt.UMO)["current"]["phase"], "sending")
            sending.set()
            await permit_send.wait()
            event._has_send_oper = True

        async def handler(event):
            self.assertEqual(self.runtime.delivery_status(rt.UMO)["current"]["phase"], "generating")
            generation.set()
            await permit_response.wait()
            resp = SimpleNamespace(completion_text="[[SPEAK]]电影结尾有个新的解释。", result_chain=None)
            self.runtime.response(event, resp)
            self.assertEqual(self.runtime.delivery_status(rt.UMO)["current"]["phase"], "preparing")
            self.assertIsNone(self.runtime.last_outcome)
            await event.send(resp.completion_text)

        self.runtime.pipeline = AsyncMock(return_value=SimpleNamespace(execute=handler))
        with patch.object(rt.CronMessageEvent, "send", native):
            self.runtime.poll_once()
            self.addAsyncCleanup(self.runtime.terminate)
            self.assertEqual(self.runtime.delivery_status(rt.UMO)["current"]["phase"], "processing")
            await asyncio.wait_for(generation.wait(), 1)
            permit_response.set()
            await asyncio.wait_for(sending.wait(), 1)
            self.assertIsNone(self.runtime.last_outcome)
            permit_send.set()
            await asyncio.wait_for(asyncio.gather(*(worker["task"] for worker in self.runtime._workers.values())), 1)
        status = self.runtime.delivery_status(rt.UMO)
        self.assertEqual(status["active"], [])
        self.assertEqual(status["current"]["phase"], "api_accepted")
        self.assertEqual(self.runtime.last_outcome["outcome"], "sent")
        self.assertEqual(status["read"], "unknown")
        self.assertEqual(self.runtime.store.list_claimed(), [])


if __name__ == "__main__":
    unittest.main()
