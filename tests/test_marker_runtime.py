"""Final response marker integration against the actual plan/turn runtime."""

from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import test_runtime as rt


Markers = rt.SUBJECT["reply_marker_runtime"]
VOICE_KEY = Markers.VOICE_KEY


class MarkerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    setUp = rt.RuntimeIntegrationTests.setUp

    def agent_context(self, text):
        return SimpleNamespace(messages=[
            SimpleNamespace(role="user", content=[rt.TextPart("群友真实原话")]),
            SimpleNamespace(role="assistant", content=[rt.TextPart(text)]),
        ])

    async def run_reply(self, text, event=None):
        event = event or rt.Event()
        rc = self.agent_context(text)
        await self.turn.initiative_agent_begin(event, rc)
        response = SimpleNamespace(completion_text=text,
                                   result_chain=SimpleNamespace(chain=[rt.Plain(text)]))
        await self.turn.initiative_response(event, response)
        return event, response, rc

    async def test_voice_and_text_markers_are_clean_before_downstream_memory_hooks(self):
        for marker, mode in (("[[语音]]", "voice"), ("[[文字]]", "text")):
            with self.subTest(marker=marker):
                event, response, rc = await self.run_reply("我在呢，慢慢说。" + marker)
                # This is the state the later EmotionAI and Iris hooks receive.
                self.assertEqual(response.completion_text, "我在呢，慢慢说。")
                self.assertEqual(response.result_chain.chain[0].text, "我在呢，慢慢说。")
                self.assertEqual(rc.messages[-1].content[0].text, "我在呢，慢慢说。")
                self.assertEqual(event.get_extra(VOICE_KEY), mode)
                self.assertEqual(rc.messages[0].content[0].text, "群友真实原话")

    async def test_user_input_control_text_never_executes(self):
        event = rt.Event("请解释[[语音]][[开口:120]]是什么意思", addressed=True)
        await self.turn.flow(event)
        event, response, _ = await self.run_reply("这只是你问到的标记格式。", event)
        self.assertIsNone(event.get_extra(VOICE_KEY))
        self.assertIsNone(event.get_extra(rt.Runtime.STAGED))
        self.assertEqual(self.runtime.store.list_pending(), [])
        self.assertEqual(response.completion_text, "这只是你问到的标记格式。")

    async def test_followup_marker_clamps_120_and_arms_only_after_successful_send(self):
        event, response, _ = await self.run_reply("我刚想到一点，待会儿再看看。[[开口:120]]")
        staged = event.get_extra(rt.Runtime.STAGED)
        self.assertEqual(staged["delay_seconds"], 300)
        self.assertEqual(self.runtime.store.list_pending(), [])
        await self.turn.mark_replied(event)
        self.assertEqual(self.runtime.store.list_pending(), [])
        event.result = response.result_chain
        event._has_send_oper = True
        await self.turn.mark_replied(event)
        jobs = self.runtime.store.list_pending()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["due_at"], rt.NOW + 300)
        self.assertNotIn("[[", self.runtime.store.recent(rt.UMO)[-1]["text"])

    async def test_existing_tool_plan_takes_precedence_over_marker(self):
        event = rt.Event()
        tool_result = json.loads(await self.runtime.tool(event, "plan", 900, "保留工具选择的具体意图"))
        self.assertTrue(tool_result["ok"])
        old = deepcopy(event.get_extra(rt.Runtime.STAGED))
        await self.run_reply("好，那就先这样。[[开口:120]]", event)
        self.assertEqual(event.get_extra(rt.Runtime.STAGED), old)
        self.assertEqual(self.turn.reply_markers.last_result["followup_outcome"], "existing_tool_plan_kept")

    async def test_due_reply_can_choose_voice_but_cannot_plan_recursively(self):
        key = await self.runtime.conversation_key(rt.UMO)
        job = self.runtime.store.schedule(rt.UMO, 300, "稍后再考虑", key)
        event = rt.Runtime.InitiativeEvent(self.runtime, job, 0)
        event.started_at = self.clock[0]
        event, response, rc = await self.run_reply("[[SPEAK]]对了，我刚想到另一个角度。[[语音]][[开口:300]]", event)
        self.assertTrue(event.approved)
        self.assertEqual(response.completion_text, "对了，我刚想到另一个角度。")
        self.assertEqual(event.get_extra(VOICE_KEY), "voice")
        self.assertIsNone(event.get_extra(rt.Runtime.STAGED))
        self.assertEqual(self.turn.reply_markers.last_result["followup_outcome"], "ignored_recursive_plan")
        self.assertEqual(rc.messages[-1].content[0].text, "对了，我刚想到另一个角度。")

    async def test_all_groups_but_private_not_acted_on(self):
        self.turn.config.update(proactive_all_groups=True, proactive_group_whitelist=[])
        for umo in (rt.UMO, rt.OTHER_UMO, "other:GroupMessage:333"):
            event, response, _ = await self.run_reply("一会儿再聊。[[文字]][[开口:600]]", rt.Event(umo=umo))
            self.assertEqual(response.completion_text, "一会儿再聊。")
            self.assertEqual(event.get_extra(VOICE_KEY), "text")
            self.assertEqual(event.get_extra(rt.Runtime.STAGED)["delay_seconds"], 600)
        private = rt.Event(umo="default:FriendMessage:999")
        event, response, _ = await self.run_reply("这条是私聊。[[语音]][[开口:600]]", private)
        self.assertIsNone(event.get_extra(VOICE_KEY))
        self.assertIsNone(event.get_extra(rt.Runtime.STAGED))

    async def test_unmarked_reply_keeps_native_probability_and_no_override(self):
        event, response, rc = await self.run_reply("普通的一句回复。")
        self.assertIsNone(event.get_extra(VOICE_KEY))
        self.assertIsNone(event.get_extra(rt.Runtime.STAGED))
        self.assertEqual(response.completion_text, "普通的一句回复。")
        self.assertEqual(rc.messages[-1].content[0].text, "普通的一句回复。")

    async def test_control_only_reply_is_empty_without_voice_or_plan(self):
        event, response, rc = await self.run_reply("[[语音]][[开口:120]]")
        self.assertEqual(response.completion_text, "")
        self.assertEqual(response.result_chain.chain[0].text, "")
        self.assertEqual(rc.messages[-1].content[0].text, "")
        self.assertIsNone(event.get_extra(VOICE_KEY))
        self.assertIsNone(event.get_extra(rt.Runtime.STAGED))
        self.assertEqual(self.runtime.store.list_pending(), [])

    async def test_request_adds_only_temporary_guidance_and_masks_streaming(self):
        request = rt.ProviderRequest(contexts=[{"role": "user", "content": "真实历史"}],
                                     conversation=self.conv)
        before = deepcopy((request.system_prompt, request.prompt, request.contexts, self.conv.__dict__))
        event = rt.Event()
        await self.turn.flow(event)
        self.assertIs(event.get_extra("enable_streaming"), False)
        await self.turn.initiative_request(event, request)
        self.assertEqual((request.system_prompt, request.prompt, request.contexts, self.conv.__dict__), before)
        self.assertEqual(len(request.extra_user_content_parts), 2)
        self.assertTrue(all(part.temporary for part in request.extra_user_content_parts))
        self.assertIn("[[语音]]", request.extra_user_content_parts[-1].text)

    async def test_agent_done_preserves_later_emotion_metadata_cleanup_in_history(self):
        event, response, rc = await self.run_reply("我听着呢。[需要情感评估][[语音]]")
        self.assertEqual(event.get_extra(VOICE_KEY), "voice")
        # Simulate the existing later emotion hook stripping its assessment tag.
        response.completion_text = response.completion_text.replace("[需要情感评估]", "")
        await self.turn.initiative_agent_done(event, rc, response)
        self.assertEqual(rc.messages[-1].content[0].text, "我听着呢。")

    async def test_emotion_tag_after_control_does_not_prevent_control_cleaning(self):
        event, response, rc = await self.run_reply("我听着呢。[[语音]][需要情感评估]")
        self.assertEqual(event.get_extra(VOICE_KEY), "voice")
        self.assertNotIn("[[语音]]", response.completion_text)
        self.assertNotIn("[[语音]]", response.result_chain.chain[0].text)
        self.assertNotIn("[[语音]]", rc.messages[-1].content[0].text)
        self.assertIn("[需要情感评估]", response.completion_text)

    async def test_emotion_flag_before_control_is_preserved_for_emotion_but_not_saved(self):
        event, response, rc = await self.run_reply("我听着呢。[需要情感评估][[语音]]")
        self.assertEqual(event.get_extra(VOICE_KEY), "voice")
        self.assertEqual(response.completion_text, "我听着呢。[需要情感评估]")
        self.assertEqual(response.result_chain.chain[0].text, "我听着呢。")
        self.assertEqual(rc.messages[-1].content[0].text, "我听着呢。")

    async def test_only_emotion_flag_and_controls_is_not_a_real_reply(self):
        for text in ("[需要情感评估][[语音]][[开口:120]]",
                     "[[文字]][[开口:600]][需要情感评估]"):
            with self.subTest(text=text):
                event, response, rc = await self.run_reply(text)
                self.assertIsNone(event.get_extra(VOICE_KEY))
                self.assertIsNone(event.get_extra(rt.Runtime.STAGED))
                self.assertEqual(response.completion_text.replace("[需要情感评估]", ""), "")
                self.assertEqual(response.result_chain.chain[0].text, "")
                self.assertEqual(rc.messages[-1].content[0].text, "")
                self.assertEqual(self.runtime.store.list_pending(), [])

    async def test_failed_plan_storage_cannot_erase_successful_send_timestamp(self):
        event = rt.Event()
        event._has_send_oper = True
        self.runtime.sent = AsyncMock(side_effect=OSError("disk full"))
        # This one intentionally exercises the outer error logger.
        rt.LOGGER.exception.reset_mock()
        await self.turn.mark_replied(event)
        self.assertEqual(self.turn._sess(rt.UMO)["last_bot_ts"], self.clock[0])
        self.runtime.sent.assert_awaited_once_with(event)
        rt.LOGGER.exception.assert_called_once()
        rt.LOGGER.exception.reset_mock()


if __name__ == "__main__":
    unittest.main()
