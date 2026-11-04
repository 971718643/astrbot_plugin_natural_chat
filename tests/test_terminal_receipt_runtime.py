"""Persistent terminal fallback, recovery and honest admission; no remote calls."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_runtime as rt


class TerminalReceiptRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        rt.RuntimeIntegrationTests.setUp(self)
        self.turn.config.update(proactive_all_groups=True, private_self_initiated_enabled=True,
                                reply_private_markers_enabled=True)

    async def claims(self, count=1, start=9000):
        jobs = []
        for index in range(count):
            umo = "default:GroupMessage:" + str(start + index)
            key = await self.runtime.conversation_key(umo)
            jobs.append(self.runtime.store.schedule(umo, 300, "真实具体话题", key))
        self.clock[0] += 300
        claimed = self.runtime.store.pop_due()
        self.assertEqual({job["id"] for job in claimed}, {job["id"] for job in jobs})
        return jobs

    async def receipts(self, count=1):
        jobs = await self.claims(count)
        with patch.object(rt.Runtime.OutcomeLog, "_commit", side_effect=OSError("journal unavailable")):
            rows = [self.runtime.record_outcome(job["umo"], job["id"], "sent", "delivery_confirmed", 1)
                    for job in jobs]
        self.assertEqual(self.runtime.store.list_claimed(), [])
        self.assertEqual(len(self.runtime.store.list_terminal_receipts()), count)
        return jobs, rows

    async def test_receipt_survives_restart_and_flushes_original_identity_without_replay(self):
        jobs, rows = await self.receipts()
        journal = Path(self.folder.name) / "self-initiative-outcomes.json"
        journal.write_bytes(b"{corrupt journal evidence")
        restarted = rt.Runtime.InitiativeRuntime(self.turn, Path(self.folder.name))
        restarted.store.now = lambda: self.clock[0]
        restarted.pipeline = AsyncMock(side_effect=AssertionError("receipt must not replay"))
        restarted.recover_claims()
        self.assertEqual(restarted.outcome_records()[0], rows[0])
        self.assertEqual(restarted.store.list_claimed(), [])
        self.clock[0] += 30
        restarted.poll_once()
        self.assertEqual(journal.read_bytes(), b"{corrupt journal evidence")
        self.assertEqual(restarted.store.list_terminal_receipts(), rows)
        journal.write_text('{"version":1,"records":[]}', encoding="utf-8")
        self.clock[0] += 30
        restarted.poll_once()
        restarted.pipeline.assert_not_awaited()
        self.assertEqual(restarted.outcomes.records(), rows)
        self.assertEqual(restarted.store.list_terminal_receipts(), [])
        self.assertEqual(restarted.store.list_claimed(), [])
        self.assertEqual(restarted.status()["awaiting_journal_count"], 0)

    async def test_partial_flush_retries_original_ids_without_duplicate_rows(self):
        _, rows = await self.receipts(3)
        original = rt.Runtime.OutcomeLog.append_record
        appended = []

        def fail_second(journal, row):
            appended.append(row["job_id"])
            if len(appended) == 2:
                raise OSError("second append failed")
            return original(journal, row)

        self.clock[0] += 30
        with patch.object(rt.Runtime.OutcomeLog, "append_record", fail_second):
            self.runtime._retry_outcome_journal()
        self.assertFalse(self.runtime.status()["outcome_journal_persistent"])
        self.assertEqual(len(self.runtime.outcomes.records()), 1)
        self.assertEqual(len(self.runtime.store.list_terminal_receipts()), 2)
        self.clock[0] += 30
        self.runtime._retry_outcome_journal()
        committed = self.runtime.outcomes.records(limit=100)
        self.assertEqual({row["id"] for row in committed}, {row["id"] for row in rows})
        self.assertEqual(len(committed), 3)
        self.assertEqual({row["job_id"]: row for row in committed},
                         {row["job_id"]: row for row in rows})
        self.assertEqual(self.runtime.store.list_terminal_receipts(), [])

    async def test_receipt_cleanup_failure_keeps_canonical_true_and_retries_with_memory_none(self):
        _, rows = await self.receipts()
        self.clock[0] += 30
        with patch.object(self.runtime.store, "finish_terminal_receipt", side_effect=OSError("queue unavailable")):
            self.runtime._retry_outcome_journal()
        status = self.runtime.status()
        self.assertTrue(status["outcome_journal_persistent"])
        self.assertIsNone(self.runtime._outcome_memory)
        self.assertEqual(status["awaiting_journal_count"], 0)
        self.assertEqual(status["terminal_receipt_count"], 1)
        self.assertEqual(status["terminal_receipt_error"], "OSError")
        self.assertEqual(self.runtime.outcomes.records(), rows)
        self.clock[0] += 30
        self.runtime._retry_outcome_journal()
        self.assertEqual(self.runtime.store.list_terminal_receipts(), [])
        self.assertEqual(self.runtime.outcomes.records(), rows)
        self.assertIsNone(self.runtime.status()["terminal_receipt_error"])

    def small_cap(self):
        for subject in (rt.SUBJECT["initiative"], rt.Runtime):
            change = patch.object(subject, "MAX_RETAINED_CLAIMS", 1)
            change.start()
            self.addCleanup(change.stop)

    async def stage(self):
        event = rt.Event("真实话题还有一些想聊的")
        result = json.loads(await self.runtime.tool(event, "plan", 300, "补充真实话题"))
        self.assertTrue(result["ok"])
        event._has_send_oper = True
        self.runtime.active_until[rt.UMO] = self.clock[0] + 300
        return event

    async def assert_rejected_sent(self, event):
        await self.turn.mark_replied(event)
        await self.turn.mark_replied(event)
        self.assertIsNone(event.get_extra(rt.Runtime.STAGED))
        self.assertNotIn(rt.UMO, self.runtime.active_until)
        self.assertEqual(self.runtime.store.list_pending(rt.UMO), [])
        blocked = [row for row in self.runtime.outcome_records(rt.UMO, limit=100)
                   if row["reason"] == "claim_capacity_exhausted"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["outcome"], "blocked")
        ordinary = rt.Event("这是没有预约的正常回复")
        ordinary._has_send_oper = True
        await self.turn.mark_replied(ordinary)
        self.assertEqual(len([row for row in self.runtime.outcome_records(rt.UMO, limit=100)
                              if row["reason"] == "claim_capacity_exhausted"]), 1)

    async def test_staged_plan_capacity_taken_before_send_is_rejected_once(self):
        self.small_cap()
        event = await self.stage()
        await self.claims()
        await self.assert_rejected_sent(event)

    async def test_capacity_taken_during_sent_conversation_lookup_is_rejected_once(self):
        self.small_cap()
        event = await self.stage()
        staged = event.get_extra(rt.Runtime.STAGED)

        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked_lookup(umo):
            entered.set()
            await release.wait()
            return staged["conversation_id"]

        with patch.object(self.runtime, "conversation_key", side_effect=blocked_lookup):
            sending = asyncio.create_task(self.turn.mark_replied(event))
            await asyncio.wait_for(entered.wait(), 1)
            self.runtime.store.schedule("default:GroupMessage:9000", 300,
                                        "占用执行容量", staged["conversation_id"])
            self.clock[0] += 300
            self.runtime.store.pop_due()
            release.set()
            await asyncio.wait_for(sending, 1)
            await self.assert_rejected_sent(event)

    async def test_full_capacity_combined_request_keeps_voice_without_encouraging_followup(self):
        self.small_cap()
        await self.claims()
        for umo in (rt.UMO, "default:FriendMessage:111"):
            with self.subTest(umo=umo):
                event = rt.Event(umo=umo)
                request = rt.ProviderRequest(func_tool=rt.ToolSet(["self_initiative", "other"]))
                await self.turn.initiative_request(event, request)
                text = "".join(part.text for part in request.extra_user_content_parts)
                self.assertIn("暂不可", text)
                self.assertIn("[[语音]]", text)
                self.assertIn("[[文字]]", text)
                self.assertNotIn("想稍后再自然接一句", text)
                self.assertNotIn("已有一项待考虑", text)
                self.assertNotIn("可主动调用self_initiative(action=plan", text)
                self.assertEqual(request.func_tool.names, ["self_initiative", "other"])
                event.set_extra(rt.Runtime.STAGED, {"delay_seconds": 300})
                result = json.loads(await self.runtime.tool(event, "plan", 300, "仍不可预约"))
                self.assertFalse(result["ok"])
                self.assertIsNone(event.get_extra(rt.Runtime.STAGED))
                response = SimpleNamespace(completion_text="普通回应。[[语音]][[开口:300]]", result_chain=None)
                await self.turn.reply_markers.response(event, response)
                self.assertIsNone(event.get_extra(rt.Runtime.STAGED))
                self.assertEqual(response.completion_text, "普通回应。")

    async def test_actual_hundred_claim_terminals_survive_display_ring_eviction_and_clear_cache(self):
        jobs = await self.claims(100)
        with patch.object(rt.Runtime.OutcomeLog, "_commit", side_effect=OSError("journal unavailable")):
            with patch.object(self.runtime.store, "archive_claim", side_effect=OSError("queue unavailable")):
                rows = {job["id"]: self.runtime.record_outcome(job["umo"], job["id"],
                                                              "sent", "delivery_confirmed", 1)
                        for job in jobs}
                self.runtime.record_outcome(rt.UMO, "extra-blocked", "blocked", "claim_capacity_exhausted")
                with patch.object(self.runtime, "record_outcome", wraps=self.runtime.record_outcome) as record:
                    self.runtime._retry_claim_acks()
                    record.assert_not_called()
                self.assertEqual(len(self.runtime._claim_terminals), 100)
                self.assertEqual(self.runtime._claim_terminals, rows)
            # Queue repairs while canonical remains broken: archive actual
            # preserved rows, not newly invented interrupted outcomes.
            self.runtime._retry_claim_acks()
            self.assertEqual(self.runtime._claim_terminals, {})
            self.assertEqual(self.runtime.store.list_claimed(), [])
            receipts = self.runtime.store.list_terminal_receipts()
            self.assertEqual({row["job_id"]: row for row in receipts}, rows)
        self.clock[0] += 30
        self.runtime._retry_outcome_journal()
        self.assertEqual(self.runtime._claim_terminals, {})
        self.assertEqual(self.runtime.store.list_terminal_receipts(), [])
        self.assertTrue(self.runtime.status()["outcome_journal_persistent"])


if __name__ == "__main__":
    unittest.main()
