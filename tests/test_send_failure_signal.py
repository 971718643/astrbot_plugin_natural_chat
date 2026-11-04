"""Failure observation preserves real send behaviour and voice fallback."""

import asyncio
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_runtime as rt


class ActionFailed(Exception):
    __module__ = "aiocqhttp.exceptions"

    def __init__(self, code=100, status="failed"):
        self.result = {"status": status, "retcode": code,
                       "message": "sk-secret private rejected text"}

    @property
    def retcode(self):
        return self.result["retcode"]


class MessageChain:
    def __init__(self, chain):
        self.chain = chain


class Record:
    def __init__(self, text="existing transcript"):
        self.text = text


class SendFailureSignalTests(unittest.IsolatedAsyncioTestCase):
    setUp = rt.RuntimeIntegrationTests.setUp

    def ordinary(self, native):
        event = rt.Event("不要把原文sk-private放进投递记录", addressed=True)
        event.send = native
        self.runtime.waiting(event)
        self.runtime.request(event, rt.ProviderRequest())
        return event

    def current(self):
        return self.runtime.delivery_status(rt.UMO)["current"]

    async def test_structured_atomic_rejection_is_immediate_without_retry_or_raw_error(self):
        error = ActionFailed()
        native = AsyncMock(side_effect=error)
        quota = deepcopy(self.turn.proactive_policy.history)
        event = self.ordinary(native)
        with self.assertRaises(ActionFailed) as caught:
            await event.send(MessageChain([rt.Plain("never retain rejected body")]))
        self.assertIs(caught.exception, error)
        native.assert_awaited_once()
        self.assertEqual(self.current()["phase"], "rejected")
        self.assertEqual(self.current()["reason"], "platform_rejected")
        self.assertEqual(self.current()["send_attempts"], 1)
        self.assertEqual(self.runtime.delivery_status(rt.UMO)["read"], "unknown")
        text = json.dumps(self.runtime.delivery_status(), ensure_ascii=False)
        for secret in ("sk-private", "sk-secret", "rejected body", "private rejected text"):
            self.assertNotIn(secret, text)
        self.assertEqual(self.runtime.outcome_records(), [])
        self.assertEqual(self.turn.proactive_policy.history, quota)
        self.assertEqual(self.runtime.store.list_pending(), [])
        self.assertEqual(list(Path(self.folder.name).iterdir()), [])
        # A native after-sent hook without success cannot erase the problem.
        await self.turn.mark_replied(event)
        self.assertEqual(self.current()["phase"], "rejected")

    async def test_timeout_cancel_and_generic_errors_remain_unconfirmed_immediately(self):
        for error in (TimeoutError("private body"), RuntimeError("private body"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                event = self.ordinary(AsyncMock(side_effect=error))
                with self.assertRaises(type(error)) as caught:
                    await event.send(MessageChain([rt.Plain("body")]))
                self.assertIs(caught.exception, error)
                self.assertEqual(self.current()["phase"], "unconfirmed")
                self.assertEqual(self.current()["phase_at"], self.clock[0])
                self.clock[0] += 1

    async def test_malformed_negative_proof_never_claims_platform_rejection(self):
        for error in (ActionFailed(0), ActionFailed(True), ActionFailed("100"),
                      ActionFailed(100, "async"), type("ActionFailed", (Exception,), {})("not OneBot")):
            event = self.ordinary(AsyncMock(side_effect=error))
            with self.assertRaises(type(error)):
                await event.send(MessageChain([rt.Plain("body")]))
            self.assertEqual(self.current()["phase"], "unconfirmed")
            self.clock[0] += 1

    async def test_compound_forward_may_be_partial_and_is_not_called_wholly_rejected(self):
        for name in ("Node", "Nodes", "File"):
            event = self.ordinary(AsyncMock(side_effect=ActionFailed()))
            component = type(name, (), {})()
            with self.assertRaises(ActionFailed):
                await event.send(MessageChain([rt.Plain("possibly sent"), component]))
            self.assertEqual(self.current()["phase"], "unconfirmed")
            self.clock[0] += 1

    async def test_segment_success_then_rejection_cannot_be_overwritten_by_final_hook(self):
        event = None
        calls = []

        async def native(message):
            calls.append(message)
            if len(calls) == 1:
                event._has_send_oper = True
            else:
                raise ActionFailed()

        event = self.ordinary(native)
        await event.send(MessageChain([rt.Plain("first")]))
        self.assertEqual(self.current()["phase"], "api_accepted")
        with self.assertRaises(ActionFailed):
            await event.send(MessageChain([rt.Plain("second")]))
        self.assertEqual(self.current()["phase"], "partially_accepted")
        self.assertEqual(self.current()["last_attempt"], "rejected")
        await self.turn.mark_replied(event)
        self.assertEqual(self.current()["phase"], "partially_accepted")
        self.assertEqual(self.current()["send_attempts"], 2)
        self.assertEqual(len(calls), 2)

    async def test_rejected_segment_then_success_still_reports_partial_confirmation(self):
        event = None
        calls = 0

        async def native(_message):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ActionFailed()
            event._has_send_oper = True

        event = self.ordinary(native)
        with self.assertRaises(ActionFailed):
            await event.send(MessageChain([rt.Plain("first")]))
        await event.send(MessageChain([rt.Plain("second")]))
        await self.turn.mark_replied(event)
        self.assertEqual(self.current()["phase"], "partially_accepted")
        self.assertEqual(self.current()["last_attempt"], "rejected")

    async def test_observer_forwards_arguments_return_and_has_no_duplicate_wrapper(self):
        event = None
        result = object()

        async def native(*args, **kwargs):
            self.assertEqual(args, ("same-message",))
            self.assertEqual(kwargs, {"mode": "same-value"})
            event._has_send_oper = True
            return result

        mock = AsyncMock(side_effect=native)
        event = self.ordinary(mock)
        wrapper = event.send
        self.runtime.waiting(event)
        self.runtime.request(event, rt.ProviderRequest())
        self.assertIs(event.send, wrapper)
        self.assertIs(await event.send("same-message", mode="same-value"), result)
        mock.assert_awaited_once()
        self.assertEqual(self.current()["send_attempts"], 1)

    async def test_sending_state_is_visible_during_native_await(self):
        entered, release = asyncio.Event(), asyncio.Event()
        event = None

        async def native(_message):
            entered.set()
            await release.wait()
            event._has_send_oper = True

        event = self.ordinary(native)
        task = asyncio.create_task(event.send(MessageChain([rt.Plain("body")])))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(self.current()["phase"], "sending")
            release.set()
            await asyncio.wait_for(task, 1)
            self.assertEqual(self.current()["phase"], "api_accepted")
        finally:
            release.set()
            await task

    async def test_new_request_sees_failure_but_other_session_does_not(self):
        event = self.ordinary(AsyncMock(side_effect=ActionFailed()))
        with self.assertRaises(ActionFailed):
            await event.send(MessageChain([rt.Plain("body")]))
        later = rt.Event("上条是不是没发出去", addressed=True)
        req = rt.ProviderRequest()
        self.runtime.request(later, req)
        self.assertIn("发送接口明确返回执行失败", req.extra_user_content_parts[-1].text)
        self.assertIn("不当作已经", req.extra_user_content_parts[-1].text)
        own_status = json.loads(await self.runtime.tool(later, "status"))["delivery"]
        self.assertEqual(own_status["recent_problems"][0]["phase"], "rejected")
        self.turn.config["proactive_all_groups"] = True
        other = rt.Event(umo=rt.OTHER_UMO)
        status = json.loads(await self.runtime.tool(other, "status"))["delivery"]
        self.assertEqual(status["recent_problems"], [])
        self.assertNotIn(rt.UMO, json.dumps(status))

    async def test_success_without_flag_is_unconfirmed_and_hook_cannot_fake_complete_reply(self):
        event = self.ordinary(AsyncMock(return_value=None))
        await event.send(MessageChain([rt.Plain("body")]))
        self.assertEqual(self.current()["phase"], "unconfirmed")
        event._has_send_oper = True
        await self.turn.mark_replied(event)
        self.assertEqual(self.current()["phase"], "partially_accepted")

    async def test_diagnostic_clock_failure_never_blocks_native_send_or_masks_exception(self):
        native = AsyncMock(return_value="unchanged")
        event = self.ordinary(native)
        self.runtime.delivery_observations.now = lambda: (_ for _ in ()).throw(ValueError("clock"))
        self.assertEqual(await event.send(MessageChain([rt.Plain("body")])), "unchanged")
        native.assert_awaited_once()
        error = ActionFailed()
        native.side_effect = error
        with self.assertRaises(ActionFailed) as caught:
            await event.send(MessageChain([rt.Plain("body")]))
        self.assertIs(caught.exception, error)

    async def test_unknown_or_unreadable_chain_does_not_mask_original_error(self):
        class Unreadable:
            @property
            def chain(self):
                raise ValueError("bad chain")
        error = ActionFailed()
        event = self.ordinary(AsyncMock(side_effect=error))
        with self.assertRaises(ActionFailed) as caught:
            await event.send(Unreadable())
        self.assertIs(caught.exception, error)
        self.assertEqual(self.current()["phase"], "unconfirmed")

    async def voice_case(self, fallback_succeeds):
        source = Path(__file__).resolve().parent / "fixtures/voice_fallback.py"
        spec = importlib.util.spec_from_file_location("existing_voice_fallback", source)
        voice = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(voice)
        event = None
        calls = []

        async def original(_event, message):
            calls.append(message)
            if len(calls) == 1 or not fallback_succeeds:
                raise ActionFailed()

        fallback = voice.wrap_voice_send(original, Record, rt.Plain, MessageChain,
                                         SimpleNamespace(metric=lambda *a: None),
                                         SimpleNamespace(info=lambda *a: None, warning=lambda *a: None))

        async def native(message):
            result = await fallback(event, message)
            event._has_send_oper = True
            return result

        event = self.ordinary(native)
        chain = MessageChain([Record(), rt.Plain("already existing transcript")])
        if fallback_succeeds:
            await event.send(chain)
            self.assertEqual(self.current()["phase"], "api_accepted")
            self.assertEqual(self.current().get("send_problems", 0), 0)
        else:
            with self.assertRaises(ActionFailed):
                await event.send(chain)
            self.assertEqual(self.current()["phase"], "rejected")
        self.assertEqual(len(calls), 2)  # The pre-existing one text fallback only.
        self.assertEqual(self.current()["send_attempts"], 1)

    async def test_existing_voice_rejection_text_fallback_success_is_not_misreported_failed(self):
        await self.voice_case(True)

    async def test_existing_voice_and_text_fallback_failure_has_one_negative_signal(self):
        await self.voice_case(False)

    async def test_autonomous_explicit_rejection_maps_to_negative_status(self):
        key = await self.runtime.conversation_key(rt.UMO)
        job = self.runtime.store.schedule(rt.UMO, 300, "电影结尾的新补充", key)
        event = rt.Runtime.InitiativeEvent(self.runtime, job, 0)
        event.started_at = self.clock[0]
        event.approved = True
        with patch.object(rt.CronMessageEvent, "send", AsyncMock(side_effect=ActionFailed())):
            await event.send(MessageChain([rt.Plain("body")]))
        self.runtime.finish_event(event)
        row = self.runtime.outcome_records()[0]
        self.assertEqual((row["outcome"], row["reason"]), ("failed", "platform_rejected"))
        self.assertEqual(self.current()["phase"], "rejected")


if __name__ == "__main__":
    unittest.main()
