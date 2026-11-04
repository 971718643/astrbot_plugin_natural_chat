"""Expiry evidence is passive, scoped and only surfaced for relevant requests."""

import unittest
from unittest.mock import patch
import test_open_thread_runtime as old
import test_runtime as rt


class TopicExpiryRuntimeTests(unittest.IsolatedAsyncioTestCase):
    setUp = old.OpenThreadRuntimeTests.setUp
    rows = old.OpenThreadRuntimeTests.rows
    question = old.OpenThreadRuntimeTests.question
    request = old.OpenThreadRuntimeTests.request
    response = old.OpenThreadRuntimeTests.response
    exchange = old.OpenThreadRuntimeTests.exchange
    open_question = old.OpenThreadRuntimeTests.open_question

    async def expired(self):
        _, row = await self.open_question()
        self.clock[0] += 5 * 86400
        return row

    @staticmethod
    def text(req):
        return "".join(part.text for part in req.extra_user_content_parts)

    async def test_explicit_question_gets_only_title_time_reason_no_resurrection(self):
        row = await self.expired()
        for _ in range(2):
            _, req = await self.request("上次没聊完的话题还记得吗")
            text = self.text(req)
            self.assertIn("【未完话题到期记录】", text)
            self.assertIn(row["topic"], text)
            self.assertIn("+08:00", text)
            self.assertNotIn(row["point"], text)
            self.assertNotIn(row["assistant_quote"], text)
            self.assertTrue(req.extra_user_content_parts[-1].temporary)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.topics.store.status()["expiry_event_count"], 1)
        self.assertEqual(self.runtime.store.list_pending(), [])
        self.assertEqual(self.runtime._workers, {})

    async def test_literal_topic_can_explain_expiry_but_irrelevant_chat_is_quiet(self):
        row = await self.expired()
        _, req = await self.request("今天天气不错")
        self.assertNotIn("【未完话题到期记录】", self.text(req))
        _, related = await self.request(row["topic"] + "后来怎么样")
        self.assertIn("【未完话题到期记录】", self.text(related))

    async def test_current_person_scope_and_umo_required(self):
        await self.expired()
        for kwargs in ({"sender": "u2"}, {"umo": old.PRIVATE},
                       {"conv": old.SimpleNamespace(cid="other", persona_id="another")}):
            _, req = await self.request("记得之前的话题吗", **kwargs)
            self.assertNotIn("【未完话题到期记录】", self.text(req))

    async def test_internal_wakes_never_get_expiry_notes(self):
        await self.expired()
        for flag in ("turn_taking.self_initiative", "turn_taking.proactive", "_active_trigger"):
            event = rt.Event("记得之前的话题吗", addressed=True)
            event.set_extra(flag, True)
            req = rt.ProviderRequest(prompt=event.message_str)
            await self.topics.request(event, req)
            self.assertEqual(req.extra_user_content_parts, [])

    async def test_unwritable_expiry_keeps_file_and_normal_reply_alive(self):
        await self.expired()
        original = self.topics.store.path.read_bytes()
        path = self.topics.store.path
        with patch.object(self.topics.store, "_commit", side_effect=OSError("full")):
            event, req = await self.request("记得之前的话题吗")
        self.assertFalse(self.topics.status()["enabled"])
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(event.stopped)
        self.assertGreater(len(req.extra_user_content_parts), 0)


if __name__ == "__main__":
    unittest.main()
