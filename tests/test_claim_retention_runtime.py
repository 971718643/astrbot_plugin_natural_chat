"""Bounded unresolved claims when both terminal persistence paths fail."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_queue_runtime_recovery as queue_tests
import test_runtime as rt


GROUPS = ["default:GroupMessage:" + str(7100 + number) for number in range(6)]
PRIVATE = "default:FriendMessage:8100"


class ClaimRetentionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    pending = queue_tests.QueueRuntimeRecoveryTests.pending
    use_pipeline = queue_tests.QueueRuntimeRecoveryTests.use_pipeline
    silent = queue_tests.QueueRuntimeRecoveryTests.silent
    settle = queue_tests.QueueRuntimeRecoveryTests.settle

    def setUp(self):
        rt.RuntimeIntegrationTests.setUp(self)
        # Small capacity exercises multiple real generations and disk commits;
        # the store tests separately exercise the production 100-claim limit.
        for subject in (rt.SUBJECT["initiative"], rt.Runtime):
            setting = patch.object(subject, "MAX_RETAINED_CLAIMS", 3)
            setting.start()
            self.addCleanup(setting.stop)
        self.executed = []

        async def handler(event):
            self.executed.append(event.job["id"])
            self.silent(event)

        self.use_pipeline(handler)
        self.addAsyncCleanup(self.runtime.terminate)

    async def fill_limit(self):
        jobs = []
        # Distinct sessions and two scheduling rounds show cumulative retention
        # rather than just the four-worker concurrency barrier.
        for umos in (GROUPS[:2], GROUPS[2:3]):
            jobs.extend(await self.pending(umos))
            self.runtime.poll_once()
            await self.settle()
            self.runtime.poll_once()
        self.assertEqual(len(self.executed), 3)
        self.assertEqual(len(self.runtime.store.list_claimed()), 3)
        self.assertEqual(self.runtime.store.list_blocking_claimed(), [])
        self.assertTrue(self.runtime.status()["claim_backpressure"])
        return jobs

    async def accepted_before_fault_fills_capacity(self, umos):
        self.turn.config["proactive_all_groups"] = True
        jobs = []
        for umo in umos:
            key = await self.runtime.conversation_key(umo)
            jobs.append(self.runtime.store.schedule(umo, 1200, "已接受的真实待续话题", key))
        return jobs

    def terminal_persistence_faults(self):
        # The queue remains writable, as do normal chat observations. Simulate
        # failure of only the canonical journal and atomic terminal fallback.
        from contextlib import ExitStack
        faults = ExitStack()
        faults.enter_context(patch.object(rt.Runtime.OutcomeLog, "_commit",
                                          side_effect=OSError("journal unavailable")))
        faults.enter_context(patch.object(rt.SUBJECT["initiative"].InitiativeStore, "archive_claim",
                                          side_effect=OSError("terminal fallback unavailable")))
        return faults

    async def test_dual_fault_bounds_claims_rejects_plans_and_records_blocked_pending_privately(self):
        blocked = await self.accepted_before_fault_fills_capacity(GROUPS[3:5])
        with self.terminal_persistence_faults():
            retained = await self.fill_limit()
            self.assertEqual(self.runtime.store.list_pending(), [])
            blocked_rows = [row for row in self.runtime.outcome_records(limit=100)
                            if row["job_id"] in {job["id"] for job in blocked}]
            self.assertEqual({row["job_id"] for row in blocked_rows}, {job["id"] for job in blocked})
            self.assertEqual({(row["outcome"], row["reason"]) for row in blocked_rows},
                             {("blocked", "claim_capacity_exhausted")})
            self.assertFalse(self.runtime.has_pending(GROUPS[3]))
            event = rt.Event(umo=GROUPS[3])
            rejected = json.loads(await self.runtime.tool(event, "plan", 300, "新的具体补充"))
            self.assertFalse(rejected["ok"])
            self.assertEqual(rejected["code"], "claim_capacity_exhausted")
            self.assertIsNone(event.get_extra(rt.Runtime.STAGED))
            key = await self.runtime.conversation_key(GROUPS[3])
            with self.assertRaisesRegex(ValueError, "claim_capacity_exhausted"):
                self.runtime.store.schedule(GROUPS[3], 300, "绕过工具的计划", key)
            disk_before = self.runtime.store.path.read_bytes()
            for _ in range(25):
                self.runtime.poll_once()
            self.assertEqual(self.runtime.pipeline.await_count, 3)
            self.assertEqual(self.runtime.store.path.read_bytes(), disk_before)
            disk = json.loads(disk_before)
            self.assertEqual(set(disk["claimed"]), {job["id"] for job in retained})
            self.assertEqual(self.runtime.store.list_pending(), [])
            self.assertEqual(self.runtime.store.list_terminal_receipts(), [])
            status = self.runtime.status()
            self.assertEqual((status["retained_claim_limit"], status["claim_capacity_remaining"],
                              status["claim_backpressure"]), (3, 0, True))
            local = json.loads(await self.runtime.tool(rt.Event(umo=GROUPS[3]), "status"))
            self.assertEqual((local["retained_claim_limit"], local["claim_capacity_remaining"],
                              local["claim_backpressure"]), (3, 0, True))
            self.assertEqual(local["pending"], [])
            for other_umo in GROUPS[:3] + GROUPS[4:5]:
                self.assertNotIn(other_umo, json.dumps(local))

    async def test_repair_accepts_new_plans_without_replaying_previously_blocked_pending(self):
        blocked = await self.accepted_before_fault_fills_capacity(GROUPS[3:5])
        with self.terminal_persistence_faults():
            retained = await self.fill_limit()
            self.assertEqual(len(self.executed), 3)
            self.assertEqual(self.runtime.store.list_pending(), [])
        self.clock[0] += 30
        self.runtime.poll_once()
        self.assertEqual(self.runtime.store.list_claimed(), [])
        self.assertEqual(self.runtime.store.claim_capacity_remaining(), 3)
        self.assertEqual(self.runtime.pipeline.await_count, 3)
        fresh = []
        for umo in GROUPS[3:5]:
            event = rt.Event(umo=umo)
            planned = json.loads(await self.runtime.tool(event, "plan", 300, "修复后的新话题补充"))
            self.assertTrue(planned["ok"])
            event._has_send_oper = True
            await self.turn.mark_replied(event)
            fresh.append(self.runtime.store.list_pending(umo)[0])
        self.clock[0] += 300
        self.runtime.poll_once()
        await self.settle()
        self.runtime.poll_once()
        self.assertEqual(set(self.executed), {job["id"] for job in retained + fresh})
        self.assertTrue({job["id"] for job in blocked}.isdisjoint(self.executed))
        self.assertEqual(len(self.executed), len(set(self.executed)))
        self.assertEqual(self.runtime.store.list_claimed(), [])
        self.assertEqual(self.runtime.store.list_pending(), [])
        status = self.runtime.status()
        self.assertEqual((status["claim_capacity_remaining"], status["claim_backpressure"]), (3, False))
        self.assertTrue(status["outcome_journal_persistent"])
        self.assertEqual(len(self.runtime.outcomes.records()), 7)
        blocked_rows = [row for row in self.runtime.outcomes.records(limit=100)
                        if row["job_id"] in {job["id"] for job in blocked}]
        self.assertEqual(len(blocked_rows), 2)
        self.assertEqual({row["outcome"] for row in blocked_rows}, {"blocked"})

    async def test_restart_during_dual_fault_keeps_cap_evidence_and_refuses_false_reservation(self):
        with self.terminal_persistence_faults():
            retained = await self.fill_limit()
            await self.runtime.terminate()
            restarted = rt.Runtime.InitiativeRuntime(self.turn, Path(self.folder.name))
            self.addAsyncCleanup(restarted.terminate)
            restarted.store.now = lambda: self.clock[0]
            restarted.pipeline = AsyncMock(side_effect=AssertionError("retention full must not generate"))
            restarted.recover_claims()
            key = await restarted.conversation_key(GROUPS[3])
            with self.assertRaisesRegex(ValueError, "claim_capacity_exhausted"):
                restarted.store.schedule(GROUPS[3], 300, "最新真实话题", key)
            denied = json.loads(await restarted.tool(rt.Event(umo=GROUPS[3]), "plan", 300, "不能虚挂的预约"))
            self.assertFalse(denied["ok"])
            self.assertEqual(denied["code"], "claim_capacity_exhausted")
            self.clock[0] += 300
            restarted.poll_once()
            restarted.pipeline.assert_not_awaited()
            self.assertTrue(restarted.status()["claim_backpressure"])
            self.assertEqual(restarted.status()["claim_capacity_remaining"], 0)
            self.assertEqual({row["job"]["id"] for row in restarted.store.list_claimed()},
                             {job["id"] for job in retained})
            self.assertEqual(restarted.store.list_pending(), [])
            self.assertEqual(restarted.store.list_terminal_receipts(), [])
            self.assertTrue(all(row["outcome"] == "interrupted" for row in restarted.outcome_records()))

    async def test_explicit_group_and_private_replies_continue_when_retention_is_full(self):
        self.turn.config.update(private_self_initiated_enabled=True, reply_private_markers_enabled=True)
        with self.terminal_persistence_faults():
            retained = await self.fill_limit()
            attempt_history = deepcopy(self.turn.proactive_policy.history)
            group = rt.Event("@小鲸鱼 能继续聊一下刚才的电影吗", umo=GROUPS[0], addressed=True)
            await self.turn.flow(group)
            self.assertTrue(group.is_wake)
            self.assertFalse(group.stopped)
            self.assertFalse(group.get_extra(rt.Runtime.FLAG, False))
            group_request = rt.ProviderRequest()
            self.runtime.request(group, group_request)
            group_guidance = "".join(part.text for part in group_request.extra_user_content_parts)
            self.assertIn("暂不可用", group_guidance)
            self.assertNotIn("已有一项待考虑的接话", group_guidance)
            group._has_send_oper = True
            await self.turn.mark_replied(group)
            self.assertEqual(self.runtime.store.recent(GROUPS[0])[-1]["role"], "assistant")

            private = rt.Event("我还想聊刚才那个话题", umo=PRIVATE, addressed=True)
            private.message_obj.group_id = ""
            native_flags = (private.is_at_or_wake_command, private.is_wake, private.call_llm)
            await self.turn.observe_private(private)
            self.assertEqual((private.is_at_or_wake_command, private.is_wake, private.call_llm), native_flags)
            request = rt.ProviderRequest(func_tool=rt.ToolSet(["self_initiative", "other"]))
            self.runtime.request(private, request)
            self.assertEqual(request.func_tool.names, ["self_initiative", "other"])
            self.assertIn("暂不可用", "".join(part.text for part in request.extra_user_content_parts))
            self.assertFalse(private.stopped)
            private._has_send_oper = True
            await self.turn.mark_replied(private)
            self.assertEqual([row["role"] for row in self.runtime.store.recent(PRIVATE)], ["user", "assistant"])
            self.assertEqual(self.turn.proactive_policy.history, attempt_history)
            self.assertEqual(self.runtime.pipeline.await_count, 3)
            self.assertEqual({row["job"]["id"] for row in self.runtime.store.list_claimed()},
                             {job["id"] for job in retained})
            self.assertTrue(self.runtime.status()["claim_backpressure"])

    async def test_backpressure_does_not_cancel_an_active_worker_or_start_same_session_twice(self):
        release = asyncio.Event()
        entered = asyncio.Event()

        async def handler(event):
            self.executed.append(event.job["id"])
            if event.unified_msg_origin == GROUPS[0]:
                entered.set()
                await release.wait()
            self.silent(event)

        self.use_pipeline(handler)
        with self.terminal_persistence_faults():
            jobs = await self.pending(GROUPS[:2])
            self.runtime.poll_once()
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.sleep(0)
            key = await self.runtime.conversation_key(GROUPS[0])
            second = self.runtime.store.schedule(GROUPS[0], 300, "同会话新的真实话题", key)
            third_key = await self.runtime.conversation_key(GROUPS[2])
            third = self.runtime.store.schedule(GROUPS[2], 300, "填满最后容量的真实话题", third_key)
            self.clock[0] += 300
            self.runtime.poll_once()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.runtime.poll_once()
            self.assertTrue(self.runtime.status()["claim_backpressure"])
            self.assertEqual(len(self.executed), 3)
            self.assertEqual(len(self.runtime._workers), 1)
            self.assertTrue(self.runtime.has_pending(GROUPS[0]))
            self.assertFalse(self.runtime._workers[GROUPS[0]]["task"].done())
            self.assertEqual(self.runtime.store.list_pending(GROUPS[0]), [])
            blocked_row = next(row for row in self.runtime.outcome_records(limit=100)
                               if row["job_id"] == second["id"])
            self.assertEqual((blocked_row["outcome"], blocked_row["reason"]),
                             ("blocked", "claim_capacity_exhausted"))
            release.set()
            await self.settle()
            self.runtime.poll_once()
            self.assertEqual(set(self.executed), {job["id"] for job in jobs + [third]})
            self.assertEqual(len(self.executed), len(set(self.executed)))
            self.assertEqual(len(self.runtime.store.list_claimed()), 3)
            self.assertEqual(self.runtime.store.list_pending(GROUPS[0]), [])


if __name__ == "__main__":
    unittest.main()
