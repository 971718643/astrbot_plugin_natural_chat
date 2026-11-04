"""Topic metadata stays bounded, inert in examples, and out of spoken/history text."""

import json
from types import SimpleNamespace
import unittest

import test_reply_markers as pure
import test_runtime as rt


ID = "a" * 32
OPEN = {"kind": "deferred", "topic": "电影结局", "point": "还没讲结局的想法",
        "user_quote": "电影结局我晚点接着说", "assistant_quote": "好，等你有空再聊。"}


def marker(data, **kwargs):
    return "[[话题:" + json.dumps(data, ensure_ascii=False, **kwargs) + "]]"


class TopicControlTests(unittest.TestCase):
    parse = staticmethod(pure.parse_reply)

    def test_open_schema_and_missing_arrays_normalize_without_changing_old_positions(self):
        parsed = self.parse("好呀。" + marker({"open": [OPEN]}))
        self.assertEqual(parsed.text, "好呀。")
        self.assertEqual(parsed.topic_control,
                         {"open": [OPEN], "mention": [], "progress": [], "resolve": []})
        self.assertEqual(pure.MODULE.ParsedReply("正文", "text", 300, True).topic_control, None)

    def test_voice_delay_and_topic_permutations_keep_each_intention(self):
        topic = marker({"mention": [{"id": ID, "quote": "对了，结局还没讲完呢"}]})
        for suffix in (topic + "[[语音]][[开口:300]]",
                       "[[文字]]" + topic + "[[开口:300]]",
                       "[[开口:300]][[语音]]" + topic):
            with self.subTest(suffix=suffix):
                parsed = self.parse("接着说。" + suffix)
                self.assertEqual(parsed.text, "接着说。")
                self.assertEqual(parsed.delay_seconds, 300)
                self.assertIsNotNone(parsed.topic_control)
                self.assertEqual(parsed.voice_mode, "text" if "[[文字]]" in suffix else "voice")

    def test_all_supported_actions_and_two_operations_bound(self):
        data = {"open": [OPEN, dict(OPEN, topic="摄影")],
                "mention": [{"id": ID, "quote": "还差结局"}],
                "progress": [{"id": ID, "quote": "结局其实是", "point": "剩一个细节"}],
                "resolve": [{"id": ID, "quote": "我明白这个结局啦"}]}
        self.assertEqual(self.parse("行。" + marker(data)).topic_control, data)
        data["open"].append(dict(OPEN, topic="第三个"))
        parsed = self.parse("行。" + marker(data))
        self.assertIsNone(parsed.topic_control)
        self.assertEqual(parsed.text, "行。")

    def test_unknown_keys_wrong_types_bad_id_and_lengths_strip_without_execution(self):
        invalid = [[], {"extra": []}, {"open": OPEN}, {"open": [dict(OPEN, secret="no")]},
                   {"open": [dict(OPEN, topic="字" * 41)]},
                   {"open": [dict(OPEN, point="字" * 101)]},
                   {"open": [dict(OPEN, user_quote="字" * 61)]},
                   {"open": [dict(OPEN, kind="anything")]},
                   {"open": [dict(OPEN, point="")]},
                   {"mention": [{"id": "other", "quote": "原话"}]},
                   {"resolve": [{"id": ID, "quote": "原话", "point": "unknown"}]},
                   {"progress": [{"id": ID, "quote": "原话", "point": False}]},
                   {"mention": [{"id": ID, "quote": "bad\nquote"}]}]
        for data in invalid:
            with self.subTest(data=data):
                parsed = self.parse("好。" + marker(data) + "[[文字]]")
                self.assertEqual(parsed.text, "好。")
                self.assertIsNone(parsed.topic_control)
                self.assertEqual(parsed.voice_mode, "text")

    def test_empty_and_duplicate_json_keys_or_nonfinite_are_not_operations(self):
        for raw in ('{}', '{"open":[]}', '{"open":[],"open":[]}',
                    '{"progress":[{"id":"' + ID + '","quote":NaN}]}'):
            parsed = self.parse("好。[[话题:" + raw + "]]")
            self.assertEqual(parsed.text, "好。")
            self.assertIsNone(parsed.topic_control)

    def test_oversized_payload_is_removed_and_does_not_affect_legacy_controls(self):
        raw = json.dumps({"open": [dict(OPEN, point="字" * 2200)]}, ensure_ascii=False)
        parsed = self.parse("好。[[话题:" + raw + "]][[语音]]")
        self.assertEqual(parsed.text, "好。")
        self.assertEqual(parsed.voice_mode, "voice")
        self.assertIsNone(parsed.topic_control)

    def test_multiple_topic_blocks_are_removed_but_not_executed_even_if_identical(self):
        topic = marker({"open": [OPEN]})
        parsed = self.parse("好。" + topic + "[[语音]]" + topic)
        self.assertEqual(parsed.text, "好。")
        self.assertIsNone(parsed.topic_control)
        self.assertEqual(parsed.voice_mode, "voice")

    def test_json_strings_containing_inner_controls_and_closing_pairs_are_atomic(self):
        data = {"open": [dict(OPEN, user_quote='请解释[[文字]]和 "]]" 是什么') ]}
        parsed = self.parse("好。" + marker(data) + "[[语音]]")
        self.assertEqual(parsed.text, "好。")
        self.assertEqual(parsed.topic_control["open"], data["open"])
        self.assertEqual(parsed.voice_mode, "voice")

    def test_pretty_json_whitespace_does_not_pollute_following_markers(self):
        parsed = self.parse("好。" + marker({"open": [OPEN]}, indent=2) + "\n[[语音]]")
        self.assertEqual(parsed.text, "好。")
        self.assertEqual(parsed.topic_control["open"], [OPEN])
        self.assertEqual(parsed.voice_mode, "voice")

    def test_malformed_quotes_do_not_protect_following_controls_or_leak_topic(self):
        for raw in ('{"open":[{"topic":"oops}',
                    '{"open":[{"topic":"inner[[文字]]oops}',
                    '{not_json:[[[文字]]oops}'):
            parsed = self.parse("正文 [[话题:" + raw + "]] [[语音]]")
            self.assertEqual(parsed.text, "正文")
            self.assertIsNone(parsed.topic_control)
            self.assertEqual(parsed.voice_mode, "voice")

    def test_control_only_or_internal_decision_without_body_never_has_topic(self):
        topic = marker({"open": [OPEN]})
        for body in ("", "[[SILENT]]", "[[SPEAK]]"):
            parsed = self.parse(body + topic + "[[语音]]")
            self.assertEqual(parsed.text, body)
            self.assertIsNone(parsed.topic_control)
            self.assertIsNone(parsed.voice_mode)

    def test_quoted_code_escaped_and_non_trailing_examples_stay_inert(self):
        topic = marker({"open": [OPEN]})
        examples = ['示例 "' + topic + '"', "示例 `" + topic + "`",
                    "```json\n" + topic + "\n```", "```json\n" + topic,
                    "> " + topic, "    " + topic, "\\" + topic,
                    "示例 “" + topic, topic + " 是格式示例"]
        for text in examples:
            with self.subTest(text=text):
                parsed = self.parse(text)
                self.assertEqual(parsed.text, text)
                self.assertIsNone(parsed.topic_control)


class TopicBridgeTests(unittest.IsolatedAsyncioTestCase):
    setUp = rt.RuntimeIntegrationTests.setUp

    async def response(self, event, text):
        context = SimpleNamespace(messages=[
            SimpleNamespace(role="user", content=[rt.TextPart("真实用户输入")]),
            SimpleNamespace(role="assistant", content=[rt.TextPart(text)])])
        self.turn.reply_markers.agent_begin(event, context)
        response = SimpleNamespace(completion_text=text,
                                   result_chain=SimpleNamespace(chain=[rt.Plain(text)]))
        await self.turn.reply_markers.response(event, response)
        return response, context

    async def test_topic_is_clean_before_memory_tts_and_emotion_with_both_orders(self):
        topic = marker({"open": [OPEN]})
        for suffix in (topic + "[[语音]][需要情感评估]",
                       "[需要情感评估][[语音]]" + topic):
            with self.subTest(suffix=suffix):
                event = rt.Event()
                response, context = await self.response(event, "好，等你有空再聊。" + suffix)
                self.assertEqual(response.completion_text, "好，等你有空再聊。[需要情感评估]")
                self.assertEqual(response.result_chain.chain[0].text, "好，等你有空再聊。")
                self.assertEqual(context.messages[-1].content[0].text, "好，等你有空再聊。")
                self.assertEqual(event.get_extra("turn_taking.topic_response"), "好，等你有空再聊。")
                self.assertEqual(event.get_extra("turn_taking.topic_control")["open"], [OPEN])
                self.assertEqual(event.get_extra("turn_taking.reply_voice_mode"), "voice")

    async def test_later_tool_or_unmarked_response_resets_old_topic_stage(self):
        event = rt.Event()
        await self.response(event, "先听你说。" + marker({"open": [OPEN]}))
        self.assertIsNotNone(event.get_extra("turn_taking.topic_control"))
        await self.response(event, "")
        self.assertIsNone(event.get_extra("turn_taking.topic_control"))
        self.assertEqual(event.get_extra("turn_taking.topic_response"), "")
        await self.response(event, "现在是最终正文。")
        self.assertIsNone(event.get_extra("turn_taking.topic_control"))
        self.assertEqual(event.get_extra("turn_taking.topic_response"), "现在是最终正文。")

    async def test_disabled_flag_and_empty_body_strip_without_staging(self):
        topic = marker({"open": [OPEN]})
        disabled = rt.Event()
        self.turn.enabled = False
        response, _ = await self.response(disabled, "普通回复。" + topic)
        self.assertEqual(response.completion_text, "普通回复。")
        self.assertIsNone(disabled.get_extra("turn_taking.topic_control"))
        self.turn.enabled = True
        internal = rt.Event()
        internal.set_extra(rt.Runtime.FLAG, True)
        response, _ = await self.response(internal, "[[SPEAK]]普通回复。" + topic)
        self.assertNotIn("[[话题:", response.completion_text)
        self.assertIsNone(internal.get_extra("turn_taking.topic_control"))
        empty = rt.Event()
        response, _ = await self.response(empty, "[需要情感评估]" + topic + "[[语音]]")
        self.assertEqual(response.completion_text, "")
        self.assertIsNone(empty.get_extra("turn_taking.topic_control"))

    async def test_real_main_response_hook_never_forwards_topic_metadata(self):
        event = rt.Event("电影结局我晚点接着说")
        text = "好，等你有空再聊。" + marker({"open": [OPEN]}) + "[[文字]]"
        response = SimpleNamespace(completion_text=text,
                                   result_chain=SimpleNamespace(chain=[rt.Plain(text)]))
        await self.turn.initiative_response(event, response)
        self.assertEqual(response.completion_text, "好，等你有空再聊。")
        self.assertEqual(response.result_chain.chain[0].text, "好，等你有空再聊。")
        self.assertEqual(self.runtime.store.list_pending(), [])


if __name__ == "__main__":
    unittest.main()
