"""Topic provenance for marker plans, using the real bridge and durable policy."""

from copy import deepcopy
import json
from types import SimpleNamespace
import unittest

import test_runtime as rt


class TopicAnchorTests(unittest.IsolatedAsyncioTestCase):
    setUp = rt.RuntimeIntegrationTests.setUp

    async def reply(self, text, event=None):
        event = event or rt.Event()
        run_context = SimpleNamespace(messages=[
            SimpleNamespace(role="user", content=[rt.TextPart(event.message_str)]),
            SimpleNamespace(role="assistant", content=[rt.TextPart(text)]),
        ])
        await self.turn.initiative_agent_begin(event, run_context)
        response = SimpleNamespace(completion_text=text,
                                   result_chain=SimpleNamespace(chain=[rt.Plain(text)]))
        await self.turn.initiative_response(event, response)
        return event, response, run_context

    async def test_short_acknowledgement_keeps_the_real_user_topic(self):
        topic = "我先去试一下刚才那个相机的夜景参数，等会儿看看拍摄结果。"
        event, response, _ = await self.reply("好呀。[[开口:600]]", rt.Event(topic))
        plan = event.get_extra(rt.Runtime.STAGED)
        self.assertIn(topic, plan["reason"])
        self.assertIn("好呀。", plan["reason"])
        self.assertEqual(plan["delay_seconds"], 600)
        self.assertEqual(response.completion_text, "好呀。")
        self.assertEqual(self.runtime.store.list_pending(), [])

    async def test_long_quoted_and_control_character_data_stays_bounded(self):
        inputs = [
            '对方说“这个结尾还没讲完”。' + "电影细节" * 200,
            '第一段\r\n\t第二段 "引号" 和\\路径\x00\x07' + "后续" * 200,
        ]
        for incoming in inputs:
            with self.subTest(incoming=incoming[:12]):
                event, _, _ = await self.reply("补充想法" * 80 + "[[开口:600]]", rt.Event(incoming))
                reason = event.get_extra(rt.Runtime.STAGED)["reason"]
                self.assertLessEqual(len(reason), 200)
                self.assertIn(" ".join(incoming.split())[:30], reason)
                self.assertNotIn("\r", reason)
                self.assertNotIn("\n", reason)
                self.assertNotIn("\t", reason)
                self.assertEqual(json.loads(json.dumps(reason, ensure_ascii=False)), reason)

    async def test_missing_plain_input_uses_only_latest_user_of_current_private(self):
        self.turn.config.update(private_self_initiated_enabled=True,
                                reply_private_markers_enabled=True)
        current = "default:FriendMessage:111"
        other = "default:FriendMessage:222"
        self.runtime.store.remember(current, "user", "较早提到的题目", message_id="old")
        self.runtime.store.remember(current, "user", "本私聊刚刚谈的花盆排水问题", message_id="new")
        self.runtime.store.remember(current, "assistant", "好呀，下次再说", message_id="bot")
        self.runtime.store.remember(other, "user", "另一位好友的私密话题", message_id="other")
        event, _, _ = await self.reply("好呀。[[开口:600]]", rt.Event("", umo=current))
        reason = event.get_extra(rt.Runtime.STAGED)["reason"]
        self.assertIn("本私聊刚刚谈的花盆排水问题", reason)
        self.assertNotIn("另一位好友", reason)
        self.assertNotIn("较早提到", reason)

    async def test_existing_tool_plan_is_not_replaced_by_marker_anchor(self):
        event = rt.Event("刚刚才聊到的新主题")
        result = json.loads(await self.runtime.tool(event, "plan", 900, "已有工具提供的具体主题和补充点"))
        self.assertTrue(result["ok"])
        original = deepcopy(event.get_extra(rt.Runtime.STAGED))
        await self.reply("好呀。[[开口:600]]", event)
        self.assertEqual(event.get_extra(rt.Runtime.STAGED), original)

    async def test_incoming_marker_data_does_not_schedule_a_plan(self):
        event = rt.Event("这段[[开口:600]][[语音]]是别人发的示例，请解释一下", addressed=True)
        await self.turn.flow(event)
        event, response, _ = await self.reply("这是控制格式的示例。", event)
        self.assertEqual(response.completion_text, "这是控制格式的示例。")
        self.assertIsNone(event.get_extra(rt.Runtime.STAGED))
        self.assertIsNone(event.get_extra(rt.SUBJECT["voice_markers"].KEY))
        self.assertEqual(self.runtime.store.list_pending(), [])

    async def test_transcription_prompt_is_preferred_without_injected_memory(self):
        event = rt.Event("[语音]")
        req = SimpleNamespace(prompt="  这周六的登山路线\n还没选好，我先查一下。 ",
                              extra_user_content_parts=[rt.TextPart("Iris里的其他用户旧画像"),
                                                        rt.TextPart("额外历史里的无关话题")],
                              contexts=[{"role": "user", "content": "不属于本轮的旧消息"}])
        self.turn.reply_markers.request(event, req)
        event, _, _ = await self.reply("好呀。[[开口:600]]", event)
        reason = event.get_extra(rt.Runtime.STAGED)["reason"]
        self.assertIn("这周六的登山路线 还没选好，我先查一下。", reason)
        self.assertNotIn("[语音]", reason)
        self.assertNotIn("Iris", reason)
        self.assertNotIn("无关话题", reason)
        self.assertNotIn("不属于本轮", reason)

    async def test_internal_due_prompt_is_not_recaptured_as_user_topic(self):
        event = rt.Event("", addressed=True)
        event.set_extra(rt.Runtime.FLAG, True)
        req = SimpleNamespace(prompt="<internal_self_initiative>", extra_user_content_parts=[])
        self.turn.reply_markers.request(event, req)
        self.assertFalse(hasattr(event, "_reply_marker_current_input"))

    async def test_attachment_placeholder_does_not_override_real_message_topic(self):
        event = rt.Event("刚才提到的吉他换弦顺序，我先试试第一根。")
        req = SimpleNamespace(prompt="<attachment>", extra_user_content_parts=[])
        self.turn.reply_markers.request(event, req)
        self.assertFalse(hasattr(event, "_reply_marker_current_input"))
        event, _, _ = await self.reply("好呀。[[开口:600]]", event)
        reason = event.get_extra(rt.Runtime.STAGED)["reason"]
        self.assertIn("吉他换弦顺序", reason)
        self.assertNotIn("<attachment>", reason)


if __name__ == "__main__":
    unittest.main()
