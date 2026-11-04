"""Private marker/plan integration with real policy, durable state and hooks."""

import ast
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import AsyncMock, MagicMock

import test_runtime as rt
import test_marker_runtime as marker_tests


PRIVATE_A = "default:FriendMessage:111"
PRIVATE_B = "default:FriendMessage:222"
VOICE_KEY = rt.SUBJECT["reply_marker_runtime"].VOICE_KEY


class PrivateRuntimeTests(unittest.IsolatedAsyncioTestCase):
    agent_context = marker_tests.MarkerRuntimeTests.agent_context
    run_reply = marker_tests.MarkerRuntimeTests.run_reply

    def setUp(self):
        rt.RuntimeIntegrationTests.setUp(self)
        self.turn.config.update(private_self_initiated_enabled=True,
                                reply_private_markers_enabled=True)
        self.counter = 0
        self.context.get_config = lambda **kw: {"plugin_set": ["*"]}

    def private_event(self, text="刚才那个话题还有一点想聊", umo=PRIVATE_A):
        self.counter += 1
        event = rt.Event(text=text, umo=umo, sender=umo.split(":")[-1])
        event.message_obj.group_id = ""
        event.message_obj.message_id = "private-" + str(self.counter)
        return event

    async def private_due(self, umo=PRIVATE_A):
        key = await self.runtime.conversation_key(umo)
        job = self.runtime.store.schedule(umo, 300, "有新内容时再接一句", key)
        event = rt.Runtime.InitiativeEvent(self.runtime, job, self.runtime.revisions.get(umo, 0))
        event.started_at = self.clock[0]
        return event, job

    async def test_private_voice_and_text_controls_are_clean_before_memory_and_audio(self):
        for suffix, expected in (("[[语音]]", "voice"), ("[[文字]]", "text")):
            event, response, rc = await self.run_reply("我在这里，慢慢说。" + suffix,
                                                      self.private_event())
            self.assertEqual(event.get_extra(VOICE_KEY), expected)
            self.assertEqual(response.completion_text, "我在这里，慢慢说。")
            self.assertEqual(response.result_chain.chain[0].text, "我在这里，慢慢说。")
            self.assertEqual(rc.messages[-1].content[0].text, "我在这里，慢慢说。")

    async def test_private_marker_plan_clamps_and_only_arms_after_successful_reply(self):
        event, response, _ = await self.run_reply("想到一点，晚点再看看。[[开口:120]]",
                                                 self.private_event())
        self.assertEqual(event.get_extra(rt.Runtime.STAGED)["delay_seconds"], 300)
        await self.turn.mark_replied(event)
        self.assertEqual(self.runtime.store.list_pending(PRIVATE_A), [])
        event.result = response.result_chain
        event._has_send_oper = True
        await self.turn.mark_replied(event)
        jobs = self.runtime.store.list_pending(PRIVATE_A)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["umo"], PRIVATE_A)
        self.assertEqual(jobs[0]["due_at"], rt.NOW + 300)
        self.assertNotIn("[[", self.runtime.store.recent(PRIVATE_A)[-1]["text"])

    async def test_disabled_private_controls_are_hidden_without_actions(self):
        self.turn.config.update(private_self_initiated_enabled=False,
                                reply_private_markers_enabled=False)
        event, response, rc = await self.run_reply("这句话保留。[[语音]][[开口:60]]",
                                                 self.private_event())
        self.assertEqual(response.completion_text, "这句话保留。")
        self.assertEqual(response.result_chain.chain[0].text, "这句话保留。")
        self.assertEqual(rc.messages[-1].content[0].text, "这句话保留。")
        self.assertIsNone(event.get_extra(VOICE_KEY))
        self.assertIsNone(event.get_extra(rt.Runtime.STAGED))
        self.assertEqual(self.runtime.store.list_pending(), [])

    async def test_private_quoted_and_code_examples_are_not_executed_or_removed(self):
        for text in ("这个写法是 `[[语音]]`", "比如“[[开口:60]]”", "```\n[[文字]]\n```",
                     "> [[语音]]"):
            with self.subTest(text=text):
                event, response, _ = await self.run_reply(text, self.private_event())
                self.assertEqual(response.completion_text, text)
                self.assertIsNone(event.get_extra(VOICE_KEY))
                self.assertIsNone(event.get_extra(rt.Runtime.STAGED))

    async def test_private_observation_does_not_apply_group_turn_or_random_gate(self):
        self.turn._decide = MagicMock(side_effect=AssertionError("private must not use group turns"))
        self.turn.proactive_policy.select = MagicMock(side_effect=AssertionError("no private random"))
        for already_blocked in (False, True):
            event = self.private_event("用户自己提到[[语音]][[开口:60]]")
            event.call_llm = already_blocked
            before = (event.is_at_or_wake_command, event.is_wake, event.call_llm)
            await self.turn.observe_private(event)
            self.assertEqual((event.is_at_or_wake_command, event.is_wake, event.call_llm), before)
            self.assertIs(event.get_extra("enable_streaming"), False)
            self.assertIsNone(event.get_extra(VOICE_KEY))
            self.assertIsNone(event.get_extra(rt.Runtime.STAGED))
        self.assertEqual(len(self.runtime.store.recent(PRIVATE_A)), 2)
        self.turn._decide.assert_not_called()
        self.turn.proactive_policy.select.assert_not_called()

    async def test_private_recent_messages_and_same_id_group_remain_isolated(self):
        await self.turn.observe_private(self.private_event("好友甲的私聊原话", PRIVATE_A))
        await self.turn.observe_private(self.private_event("好友乙的私聊原话", PRIVATE_B))
        self.turn.config.update(proactive_all_groups=True, proactive_group_whitelist=[])
        same_id_group = "default:GroupMessage:111"
        self.runtime.observe(rt.Event("群里的另一个话题", umo=same_id_group))
        self.assertEqual([row["text"] for row in self.runtime.store.recent(PRIVATE_A)], ["好友甲的私聊原话"])
        self.assertEqual([row["text"] for row in self.runtime.store.recent(PRIVATE_B)], ["好友乙的私聊原话"])
        self.assertEqual([row["text"] for row in self.runtime.store.recent(same_id_group)], ["群里的另一个话题"])

    async def test_another_private_user_does_not_invalidate_but_same_user_does(self):
        event, _ = await self.private_due()
        await self.turn.observe_private(self.private_event("另一位好友的新话", PRIVATE_B))
        self.assertTrue(self.runtime.still_current(event))
        await self.turn.observe_private(self.private_event("当前好友换了新话题", PRIVATE_A))
        response = SimpleNamespace(completion_text="[[SPEAK]]过时的旧话题", result_chain=None)
        self.runtime.response(event, response)
        self.assertFalse(event.approved)
        self.assertTrue(event.stopped)
        self.assertEqual(response.completion_text, "")

    async def test_private_cron_session_and_real_iris_adapter_keep_user_not_group(self):
        event, _ = await self.private_due()
        self.assertEqual(event.session.message_type, "FriendMessage")
        self.assertEqual(event.session.session_id, "111")
        self.assertEqual(event.message_obj.group_id, "")
        self.assertEqual(event.message_str, "")
        # Execute actual Iris adapter scope methods, not a duplicate of the logic.
        source = rt.ROOT / "fixtures/iris_cron.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "CronAdapter")
        names = {"_get_session_info", "_is_group_session", "get_user_id", "get_group_id"}
        methods = [node for node in original.body if isinstance(node, ast.FunctionDef) and node.name in names]
        cls = ast.ClassDef(name="Adapter", bases=[], keywords=[], body=methods, decorator_list=[])
        namespace = {"Any": Any, "logger": MagicMock()}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), str(source), "exec"), namespace)
        adapter = namespace["Adapter"]()
        self.assertEqual(adapter.get_user_id(event), "111")
        self.assertEqual(adapter.get_group_id(event), "")

    async def test_private_request_adds_temp_guidance_without_changing_persona_or_history(self):
        event = self.private_event()
        req = rt.ProviderRequest(conversation=self.conv, contexts=[{"role": "user", "content": "原私聊历史"}],
                                 func_tool=rt.ToolSet([rt.Runtime.TOOL, "other"]))
        original = deepcopy((req.prompt, req.system_prompt, req.contexts, self.conv.__dict__))
        await self.turn.initiative_request(event, req)
        self.assertEqual((req.prompt, req.system_prompt, req.contexts, self.conv.__dict__), original)
        self.assertEqual(req.func_tool.names, [rt.Runtime.TOOL, "other"])
        self.assertTrue(all(part.temporary for part in req.extra_user_content_parts))
        self.assertEqual(len(req.extra_user_content_parts), 3)
        self.assertIn("私聊", req.extra_user_content_parts[0].text)
        self.assertNotIn("群友", req.extra_user_content_parts[-1].text)
        self.assertIn("未完话题", req.extra_user_content_parts[-1].text)

    async def test_due_private_uses_only_fresh_private_context_and_no_group_target(self):
        await self.turn.observe_private(self.private_event("甲最近刚聊的新内容", PRIVATE_A))
        await self.turn.observe_private(self.private_event("乙的内容不能带过去", PRIVATE_B))
        key = await self.runtime.conversation_key(PRIVATE_A)
        self.runtime.store.schedule(PRIVATE_A, 300, "重新考虑当前话题", key)
        self.clock[0] += 300
        [job] = self.runtime.store.pop_due()
        captured = []

        async def execute(event):
            captured.append(event)

        self.runtime.pipeline = AsyncMock(return_value=SimpleNamespace(execute=execute))
        await self.runtime.run_due(job)
        self.assertEqual(len(captured), 1)
        event = captured[0]
        self.assertEqual(event.unified_msg_origin, PRIVATE_A)
        self.assertEqual(event.message_obj.group_id, "")
        req = event.get_extra("provider_request")
        self.assertIs(req.conversation, self.conv)
        note = req.extra_user_content_parts[0].text
        self.assertIn("甲最近刚聊的新内容", note)
        self.assertNotIn("乙的内容不能带过去", note)
        self.assertIn("本私聊", note)
        self.assertNotIn("发群", note)
        self.assertNotIn("群友", note)

    async def test_private_due_delivery_records_iris_only_after_success_and_cannot_chain(self):
        event, _ = await self.private_due()
        event.plugins_name = ["astrbot_plugin_turn_taking", "astrbot_plugin_iris_chat_memory"]
        self.runtime.request(event, rt.ProviderRequest())
        event, response, _ = await self.run_reply("[[SPEAK]]想起刚才还没聊完的那个点。[[语音]][[开口:60]]", event)
        self.assertTrue(event.approved)
        self.assertEqual(event.get_extra(VOICE_KEY), "voice")
        self.assertIsNone(event.get_extra(rt.Runtime.STAGED))
        self.iris_response.assert_not_awaited()
        await self.turn.mark_replied(event)
        self.iris_response.assert_not_awaited()
        event.result = SimpleNamespace(chain=[])
        event._has_send_oper = True
        await self.turn.mark_replied(event)
        await self.turn.mark_replied(event)
        self.iris_response.assert_awaited_once_with(event, response)
        self.assertEqual(self.runtime.store.recent(PRIVATE_A)[-1]["text"], "想起刚才还没聊完的那个点。")

    async def test_private_runtime_respects_cooldown_quiet_and_hour_cap(self):
        key = await self.runtime.conversation_key(PRIVATE_A)
        job = self.runtime.store.schedule(PRIVATE_A, 300, "有新内容才接话", key)
        self.runtime.pipeline = AsyncMock(side_effect=AssertionError("policy rejection must not generate"))
        policy = self.turn.proactive_policy
        self.turn._sess(PRIVATE_A)["last_bot_ts"] = self.clock[0] - 10
        await self.runtime.run_due(job)
        self.assertEqual(self.runtime.last_outcome["outcome"], "blocked")
        self.assertEqual(self.runtime.last_outcome["reason"], "proactive_cooldown")
        self.turn._sess(PRIVATE_A)["last_bot_ts"] = 0
        policy.history[PRIVATE_A] = [self.clock[0] - 1200, self.clock[0] - 800, self.clock[0] - 400]
        await self.runtime.run_due(job)
        self.assertEqual(self.runtime.last_outcome["reason"], "proactive_hour_cap")
        policy.history.clear()
        self.clock[0] = datetime(2026, 10, 31, 23, 0, tzinfo=rt.TZ).timestamp()
        job["due_at"] = self.clock[0]
        await self.runtime.run_due(job)
        self.assertEqual(self.runtime.last_outcome["reason"], "quiet_hours")
        self.runtime.pipeline.assert_not_awaited()

    async def test_new_successful_private_reply_cancels_pending_old_plan(self):
        key = await self.runtime.conversation_key(PRIVATE_A)
        self.runtime.store.schedule(PRIVATE_A, 300, "旧话题待考虑", key)
        fresh = self.private_event("已经换了个话题")
        fresh._has_send_oper = True
        await self.turn.mark_replied(fresh)
        self.assertEqual(self.runtime.store.list_pending(PRIVATE_A), [])

    async def test_private_persona_switch_prevents_staged_plan_activation(self):
        event, _, _ = await self.run_reply("回头再看看。[[开口:600]]", self.private_event())
        self.conv.persona_id = "切换后的人格"
        event._has_send_oper = True
        await self.turn.mark_replied(event)
        self.assertEqual(self.runtime.store.list_pending(PRIVATE_A), [])


if __name__ == "__main__":
    unittest.main()
