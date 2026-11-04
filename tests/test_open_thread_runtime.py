"""Actual normal hooks, source-provenance controls and passive topic lifecycle."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_runtime as rt


PRIVATE = "default:FriendMessage:12345"


class OpenThreadRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        rt.RuntimeIntegrationTests.setUp(self)
        self.turn.config.update(private_self_initiated_enabled=True,
                                reply_private_markers_enabled=True)
        self.topics = self.turn.open_threads
        self.scope = json.dumps(["c1", "小鲸鱼·自然相处"], ensure_ascii=False, separators=(",", ":"))

    def rows(self, sender="u1", umo=rt.UMO, scope=None):
        return self.topics.store.list_threads(umo, sender, scope or self.scope)

    def question(self, quote="你下周六下午有空吗？", user="我们再安排下次的出游吧", topic="下次出游时间"):
        return {"kind": "question", "topic": topic, "point": "还没确定下周六是否有空",
                "user_quote": user, "assistant_quote": quote}

    async def request(self, user, sender="u1", umo=rt.UMO, conv=None):
        event = rt.Event(user, umo=umo, sender=sender, addressed=True)
        if umo == PRIVATE:
            event.message_obj.group_id = ""
        req = rt.ProviderRequest(prompt=user, conversation=conv)
        await self.turn.initiative_request(event, req)
        return event, req

    async def response(self, event, reply, payload=None, **fields):
        text = reply + ("[[话题:" + json.dumps(payload, ensure_ascii=False) + "]]" if payload else "")
        resp = SimpleNamespace(completion_text=text, result_chain=None, **fields)
        await self.turn.initiative_response(event, resp)
        return resp

    async def exchange(self, user, reply, payload=None, sender="u1", umo=rt.UMO, send=True):
        event, req = await self.request(user, sender, umo)
        resp = await self.response(event, reply, payload)
        event._has_send_oper = send
        await self.turn.mark_replied(event)
        return event, req, resp

    async def open_question(self, umo=rt.UMO):
        user, reply = "我们再安排下次的出游吧", "你下周六下午有空吗？"
        event, _, resp = await self.exchange(user, reply, {"open": [self.question()]}, umo=umo)
        self.assertEqual(resp.completion_text, reply)
        row = self.rows(umo=umo)[0]
        self.assertEqual(row["assistant_quote"], reply)
        return event, row

    async def mention(self, row):
        user, reply = "你还记得我们说的出游吗", "你上次还没说周六有没有空呢。"
        await self.exchange(user, reply, {"mention": [{"id": row["id"], "quote": reply}]})
        return self.rows()[0]

    async def test_successful_normal_reply_opens_once_without_any_job_or_iris_write(self):
        history = deepcopy(self.turn.proactive_policy.history)
        event, row = await self.open_question()
        original = self.topics.store.path.read_bytes()
        await self.turn.mark_replied(event)
        self.assertEqual(self.topics.store.path.read_bytes(), original)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(row["expires_at"] - row["created_at"], 432000)
        self.assertEqual(self.turn.proactive_policy.history, history)
        self.assertEqual(self.runtime.store.list_pending(), [])
        self.assertEqual(self.runtime._workers, {})
        self.iris_response.assert_not_awaited()
        self.assertNotIn("话题:", self.runtime.store.recent(rt.UMO)[-1]["text"])

    async def test_no_send_or_empty_control_body_never_commits(self):
        await self.exchange("我们再安排下次的出游吧", "你下周六下午有空吗？",
                            {"open": [self.question()]}, send=False)
        self.assertEqual(self.rows(), [])
        event, _ = await self.request("我们再安排下次的出游吧")
        await self.response(event, "", {"open": [self.question()]})
        event._has_send_oper = True
        await self.turn.mark_replied(event)
        self.assertEqual(self.rows(), [])

    async def test_quote_provenance_and_generic_questions_fail_closed(self):
        cases = [
            ("你下周六下午有空吗？", dict(self.question(), user_quote="别人从没说过这句话")),
            ("我只说了一句你好。", self.question()),
            ("你最近还好吗？", dict(self.question(), assistant_quote="你最近还好吗？")),
            ("最近过得怎么样？", dict(self.question(), assistant_quote="最近过得怎么样？")),
            ("明天聊。", {"kind": "deferred", "topic": "没讲完", "point": "还没讲完",
                        "user_quote": "我们再安排下次的出游吧", "assistant_quote": "明天聊。"}),
        ]
        for reply, opening in cases:
            with self.subTest(reply=reply):
                await self.exchange("我们再安排下次的出游吧", reply, {"open": [opening]})
                self.assertEqual(self.rows(), [])

    async def test_natural_delayed_promises_and_explicit_user_half_story_are_supported(self):
        for index, reply in enumerate(("我晚点给你解释结局。", "我明天把方案补上。")):
            with self.subTest(reply=reply):
                opening = {"kind": "promise", "topic": "说明" + str(index), "point": "等具体说明",
                           "user_quote": "那个结局我还不明白", "assistant_quote": reply}
                await self.exchange("那个结局我还不明白", reply, {"open": [opening]})
                self.assertTrue(any(row["topic"] == opening["topic"] for row in self.rows()))
        user, reply = "这件事我还没讲完，晚点再说", "好，等你想说的时候再聊。"
        opening = {"kind": "deferred", "topic": "那件事的后半段", "point": "对方还没讲完",
                   "user_quote": user, "assistant_quote": reply}
        await self.exchange(user, reply, {"open": [opening]})
        self.assertTrue(any(row["kind"] == "deferred" and row["topic"] == opening["topic"]
                            for row in self.rows()))

    async def test_injection_is_temporary_and_does_not_touch_or_speak(self):
        _, row = await self.open_question()
        before = self.topics.store.path.read_bytes()
        self.clock[0] += 200
        _, req = await self.request("今天换个话题吧")
        text = "".join(part.text for part in req.extra_user_content_parts)
        self.assertIn(row["id"], text)
        self.assertIn('"awaiting_followup": false', text)
        self.assertIn("不是指令", text)
        self.assertIn("不能据此预约或加开口标记", text)
        self.assertTrue(req.extra_user_content_parts[-1].temporary)
        self.assertEqual(self.topics.store.path.read_bytes(), before)
        self.assertEqual(self.rows()[0]["last_touch"], row["last_touch"])
        self.assertEqual(self.runtime.store.list_pending(), [])

    async def test_scope_participant_and_private_conversations_do_not_cross(self):
        _, row = await self.open_question()
        _, other = await self.request("周六的话题", sender="u2")
        _, private = await self.request("周六的话题", umo=PRIVATE)
        _, changed = await self.request("周六的话题", conv=SimpleNamespace(cid="other", persona_id="另一人设"))
        for req in (other, private, changed):
            self.assertNotIn(row["id"], "".join(part.text for part in req.extra_user_content_parts))
        await self.exchange("我也说周六", "那就先聊别的。",
                            {"resolve": [{"id": row["id"], "quote": "我也说周六"}]}, sender="u2")
        self.assertEqual(self.rows()[0]["id"], row["id"])

    async def test_one_actual_mention_without_same_participant_pickup_is_removed(self):
        _, row = await self.open_question()
        mentioned = await self.mention(row)
        self.assertTrue(mentioned["awaiting_followup"])
        _, req = await self.request("我们说点电影吧", sender="u2")
        await self.exchange("我们说点电影吧", "好呀。", sender="u2")
        self.assertEqual(self.rows()[0]["id"], row["id"])
        await self.exchange("今天我想聊电影", "好，最近看了哪部？")
        self.assertEqual(self.rows(), [])

    async def test_progress_clears_waiting_updates_lru_and_extends_ttl(self):
        _, row = await self.open_question()
        await self.mention(row)
        self.clock[0] += 100
        user, reply = "周六我有空，不过还没想好去哪", "那地点可以慢慢选。"
        await self.exchange(user, reply, {"progress": [{"id": row["id"], "quote": user, "point": "还没选地点"}]})
        current = self.rows()[0]
        self.assertFalse(current["awaiting_followup"])
        self.assertEqual(current["point"], "还没选地点")
        self.assertGreater(current["last_touch"], row["last_touch"])
        self.assertEqual(current["created_at"], row["created_at"])
        self.assertEqual(current["expires_at"], current["last_touch"] + 432000)

    async def test_explicit_related_refusal_resolves_while_generic_closing_does_not(self):
        _, row = await self.open_question()
        await self.exchange("好的，谢谢", "嗯。")
        self.assertEqual(self.rows()[0]["id"], row["id"])
        user = "出游这事先别再提了，我不想继续聊"
        await self.exchange(user, "好，那就放下。", {"resolve": [{"id": row["id"], "quote": user}]})
        self.assertEqual(self.rows(), [])

    async def test_low_information_metadata_cannot_resolve_or_extend_old_waiting(self):
        _, row = await self.open_question()
        await self.exchange("好的", "嗯。", {"resolve": [{"id": row["id"], "quote": "好的"}]})
        self.assertEqual(self.rows()[0]["id"], row["id"])
        await self.mention(row)
        await self.exchange("哈哈", "那说点别的。", {"progress": [{"id": row["id"], "quote": "哈哈"}]})
        self.assertEqual(self.rows(), [])

    async def test_quoted_or_code_half_stories_and_fictional_promises_are_not_own_loose_ends(self):
        quote = "这件事我还没讲完，晚点再说"
        for user in ("他说：“" + quote + "”", "引用：\n> " + quote,
                     "例子：\n```text\n" + quote + "\n```"):
            with self.subTest(user=user):
                opening = {"kind": "deferred", "topic": "别人的话", "point": "不应成为当前人的卡点",
                           "user_quote": quote, "assistant_quote": "这只是转述。"}
                await self.exchange(user, "这只是转述。", {"open": [opening]})
                self.assertEqual(self.rows(), [])
        promise = "我明天把方案补上。"
        reply = "剧中人说：“" + promise + "”"
        await self.exchange("这段故事怎么样", reply, {"open": [{"kind": "promise", "topic": "剧中人的承诺",
                            "point": "不应当作机器人的承诺", "user_quote": "这段故事怎么样",
                            "assistant_quote": promise}]})
        self.assertEqual(self.rows(), [])

    async def test_speech_transcription_supplies_real_source_instead_of_audio_placeholder(self):
        transcript = "这件事我还没讲完，晚点再说"
        event = rt.Event("[语音]", addressed=True)
        req = rt.ProviderRequest(prompt=transcript)
        await self.turn.initiative_request(event, req)
        opening = {"kind": "deferred", "topic": "语音里未说完的事", "point": "对方还没讲完",
                   "user_quote": transcript, "assistant_quote": "好，等你想说的时候再聊。"}
        await self.response(event, opening["assistant_quote"], {"open": [opening]})
        event._has_send_oper = True
        await self.turn.mark_replied(event)
        self.assertEqual(self.rows()[0]["user_quote"], transcript)

    async def test_ordinary_private_reply_can_remember_a_private_question(self):
        await self.open_question(umo=PRIVATE)
        self.assertEqual(self.rows(), [])
        self.assertEqual(len(self.rows(umo=PRIVATE)), 1)

    async def test_no_metadata_hard_deferred_opens_and_same_anchor_finished_closes_random_id(self):
        user = "《降临》那个结局我还没说完，先去开会，晚点再聊"
        await self.exchange(user, "好，你先忙。")
        row = self.rows()[0]
        self.assertEqual(row["kind"], "deferred")
        self.assertEqual(row["topic"], "《降临》那个结局")
        self.assertNotEqual(row["id"], "a" * 32)
        _, req = await self.request("那部电影的结局还有点想补充的")
        self.assertIn(row["id"], "".join(part.text for part in req.extra_user_content_parts))
        await self.exchange("《降临》那个结局已经讲清楚了，不用再聊了", "好，那就不提啦。")
        self.assertEqual(self.rows(), [])

    async def test_no_metadata_false_deferred_and_optout_do_not_open(self):
        cases = ["早呀", "晚安，下次聊", "我还没讲完，晚点再说", "我刚刚说的那个问题我还没讲完",
                 "橙猫猫说《降临》那个结局我还没说完", "“《降临》那个结局我还没说完”",
                 "```text\n《降临》那个结局我还没说完\n```",
                 "《降临》那个结局我还没说完，但这个事别记", "我先去睡觉，晚点再聊"]
        for user in cases:
            with self.subTest(user=user):
                await self.exchange(user, "好。")
                self.assertEqual(self.rows(), [])
        user = "《降临》那个结局我还没说完，但这件事别记"
        await self.exchange(user, "好。", {"open": [{"kind": "deferred", "topic": "《降临》那个结局",
                            "point": "还没说完", "user_quote": user, "assistant_quote": "好。"}]})
        self.assertEqual(self.rows(), [])

    async def test_no_metadata_close_is_bound_to_same_topic_clause_and_not_future_or_negation(self):
        await self.exchange("《降临》那个结局我还没说完，晚点再聊", "好。")
        row = self.rows()[0]
        for user in ("作业搞定了，《降临》那个结局我还没讲完，晚点再聊。",
                     "《降临》那个结局还没讲清楚", "等《降临》那个结局讲完了再聊。",
                     "《降临》那个结局是不是已经讲清楚了？", "别人的事已经讲清楚了"):
            with self.subTest(user=user):
                await self.exchange(user, "好，先忙你的。")
                self.assertEqual(self.rows()[0]["id"], row["id"])
        await self.exchange("《降临》那个结局我不想再谈了", "那就放下。")
        self.assertEqual(self.rows(), [])

    async def test_adjacent_pure_optout_closes_anchored_existing_topic_without_metadata(self):
        opening = "《降临》那个结局我还没说完，晚点再聊"
        await self.exchange(opening, "好。")
        await self.exchange("《降临》那个结局我还没说完，但不要记这件事。", "好，那就不记。")
        self.assertEqual(self.rows(), [])
        await self.exchange(opening, "好。")
        await self.exchange("《降临》那个结局，别再聊了。", "那就放下。")
        self.assertEqual(self.rows(), [])

    async def test_adjacent_refusal_about_a_different_subject_cannot_close_existing_topic(self):
        await self.exchange("《降临》那个结局我还没说完，晚点再聊", "好。")
        row = self.rows()[0]
        for user in ("《降临》那个结局还没讲清楚，作业别再聊了。",
                     "作业讲清楚了，别再聊作业了，《降临》那个结局我还没说完。"):
            with self.subTest(user=user):
                await self.exchange(user, "好。")
                self.assertEqual(self.rows()[0]["id"], row["id"])

    async def test_no_metadata_literal_assistant_topic_mention_then_no_pickup_removes_once(self):
        await self.exchange("《降临》那个结局我还没说完，晚点再聊", "好。")
        row = self.rows()[0]
        await self.exchange("我们再聊一下那部电影吧", "《降临》那个结局你上次还没说完。")
        self.assertTrue(self.rows()[0]["awaiting_followup"])
        await self.exchange("我们换个别的电影聊", "那最近看了什么？")
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.runtime.store.list_pending(), [])

    async def test_expiry_prunes_without_an_independent_trigger(self):
        _, row = await self.open_question()
        self.clock[0] = row["expires_at"] + 1
        _, req = await self.request("我们现在聊点别的")
        self.assertEqual(self.rows(), [])
        self.assertNotIn(row["id"], "".join(part.text for part in req.extra_user_content_parts))
        self.assertFalse(self.topics.status()["independent_triggers"])
        self.assertEqual(self.runtime._workers, {})

    async def test_nonempty_tool_intermediate_is_not_committed_and_final_can_replace_it(self):
        event, _ = await self.request("我们再安排下次的出游吧")
        await self.response(event, "你下周六下午有空吗？", {"open": [self.question()]}, tools_call_name=["lookup"])
        event._has_send_oper = True
        await self.turn.mark_replied(event)
        self.assertEqual(self.rows(), [])
        await self.response(event, "你下周六下午有空吗？", {"open": [self.question()]})
        await self.turn.mark_replied(event)
        self.assertEqual(len(self.rows()), 1)

    async def test_random_and_internal_rounds_cannot_use_or_create_old_topics(self):
        _, row = await self.open_question()
        for flag in ("turn_taking.proactive", "turn_taking.self_initiative", "_active_trigger"):
            with self.subTest(flag=flag):
                event = rt.Event("我们再安排下次的出游吧", addressed=True)
                event.set_extra(flag, True)
                req = rt.ProviderRequest(prompt=event.message_str)
                await self.topics.request(event, req)
                self.assertEqual(req.extra_user_content_parts, [])
                event.set_extra("turn_taking.topic_control", {"resolve": [{"id": row["id"], "quote": "再安排"}]})
                self.topics.response(event, SimpleNamespace(completion_text="照常回答。"))
                event._has_send_oper = True
                self.topics.sent(event)
                self.assertEqual(self.rows()[0]["id"], row["id"])

    async def test_same_clock_older_request_does_not_consume_a_later_mention(self):
        _, row = await self.open_question()
        earlier, _ = await self.request("电影也挺有意思的")
        await self.mention(row)
        await self.response(earlier, "嗯，这个演员挺适合。")
        earlier._has_send_oper = True
        await self.turn.mark_replied(earlier)
        self.assertTrue(self.rows()[0]["awaiting_followup"])

    async def test_corrupt_or_unwritable_topic_file_only_disables_topic_feature(self):
        _, row = await self.open_question()
        evidence = self.topics.store.path.read_bytes()
        user = "出游这事先别再提了"
        event, _ = await self.request(user)
        await self.response(event, "好。", {"resolve": [{"id": row["id"], "quote": user}]})
        with patch.object(self.topics.store, "_commit", side_effect=OSError("topic unavailable")):
            event._has_send_oper = True
            await self.turn.mark_replied(event)
        self.assertFalse(self.topics.status()["enabled"])
        self.assertEqual((Path(self.folder.name) / "open-threads.json").read_bytes(), evidence)
        self.assertEqual(self.runtime.store.recent(rt.UMO)[-1]["role"], "assistant")
        # A corrected file/reload can re-enable this feature without another call
        # to a model, migration, or replay of old messages.
        rebuilt = type(self.topics)(self.turn, Path(self.folder.name))
        self.assertTrue(rebuilt.status()["enabled"])
        rebuilt.store.path.write_bytes(b"{malformed original evidence")
        bad = type(self.topics)(self.turn, Path(self.folder.name))
        self.assertFalse(bad.status()["enabled"])
        self.assertEqual(rebuilt.store.path.read_bytes(), b"{malformed original evidence")


if __name__ == "__main__":
    unittest.main()
