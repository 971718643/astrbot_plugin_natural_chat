"""Regression tests for expanding both initiative paths to all group sessions."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import test_runtime as rt


class AllGroupsTests(unittest.IsolatedAsyncioTestCase):
    setUp = rt.RuntimeIntegrationTests.setUp

    def all_groups(self):
        self.turn.config.update(proactive_all_groups=True, proactive_group_whitelist=[])

    async def test_all_groups_share_scope_semantics_and_reject_private_or_malformed(self):
        self.all_groups()
        for umo in (rt.UMO, rt.OTHER_UMO, "another:GroupMessage:new-room"):
            with self.subTest(umo=umo):
                self.assertTrue(self.runtime.store.allowed(umo))
                self.assertTrue(self.turn.proactive_policy.admit(umo)[0])
        for umo in (None, "", "x", ":GroupMessage:123", "default:GroupMessage:",
                    "default:FriendMessage:123", "default:FriendMessage:GroupMessage:123"):
            with self.subTest(umo=umo):
                self.assertFalse(self.runtime.store.allowed(umo))
                self.assertFalse(self.turn.proactive_policy.admit(umo)[0])

    async def test_whitelist_mode_and_empty_deny_are_preserved(self):
        self.turn.config["proactive_all_groups"] = False
        self.assertTrue(self.runtime.store.allowed(rt.UMO))
        self.assertFalse(self.runtime.store.allowed(rt.OTHER_UMO))
        self.assertFalse(self.turn.proactive_policy.admit(rt.OTHER_UMO)[0])
        self.turn.config["proactive_group_whitelist"] = []
        self.assertFalse(self.runtime.store.allowed(rt.UMO))
        self.assertFalse(self.turn.proactive_policy.admit(rt.UMO)[0])

    async def test_group_attempt_caps_are_independent(self):
        self.all_groups()
        policy = self.turn.proactive_policy
        policy.history[rt.UMO] = [rt.NOW - 1200, rt.NOW - 800, rt.NOW - 400]
        self.assertEqual(policy.admit(rt.UMO)[1], "proactive_hour_cap")
        self.assertTrue(policy.admit(rt.OTHER_UMO)[0])
        policy.record_attempt(rt.OTHER_UMO)
        self.assertEqual(len(policy.history[rt.UMO]), 3)
        self.assertEqual(len(policy.history[rt.OTHER_UMO]), 1)

    async def test_new_group_plan_survives_restart_without_whitelist(self):
        self.all_groups()
        event = rt.Event(umo=rt.OTHER_UMO)
        result = json.loads(await self.runtime.tool(event, "plan", 600, "稍后想到新内容再聊"))
        self.assertTrue(result["ok"])
        event._has_send_oper = True
        await self.turn.mark_replied(event)
        planned = self.runtime.store.list_pending(rt.OTHER_UMO)
        self.assertEqual(len(planned), 1)
        restored = rt.SUBJECT["initiative"].InitiativeStore(
            self.turn.config, self.runtime.store.path, now=lambda: self.clock[0])
        self.assertEqual(restored.list_pending(rt.OTHER_UMO), planned)

    async def test_new_group_gets_tool_guidance_but_private_does_not(self):
        self.all_groups()
        event = rt.Event(umo=rt.OTHER_UMO)
        req = rt.ProviderRequest(func_tool=rt.ToolSet([rt.Runtime.TOOL, "other"]))
        self.runtime.request(event, req)
        self.assertIn(rt.Runtime.TOOL, req.func_tool.names)
        self.assertTrue(any("自主接话" in part.text for part in req.extra_user_content_parts))
        private = rt.Event(umo="default:FriendMessage:999")
        private_req = rt.ProviderRequest(func_tool=rt.ToolSet([rt.Runtime.TOOL, "other"]))
        self.runtime.request(private, private_req)
        self.assertEqual(private_req.func_tool.names, ["other"])
        self.assertEqual(private_req.extra_user_content_parts, [])
        self.assertFalse(json.loads(await self.runtime.tool(private, "plan", 300, "稍后聊"))["ok"])

    async def test_empty_whitelist_still_initializes_all_three_stages_without_execution(self):
        self.all_groups()
        stages = [type(name, (), {})() for name in (
            "AgentRequestSubStage", "ResultDecorateStage", "RespondStage")]
        self.runtime.pipeline = AsyncMock(return_value=SimpleNamespace(stages=stages))
        await self.runtime.initialize()
        try:
            self.runtime.pipeline.assert_awaited_once_with("default:GroupMessage:__scope_check__")
            status = self.runtime.status()
            self.assertTrue(status["ready"])
            self.assertTrue(status["worker_running"])
            self.assertEqual(status["scope"], "all_groups")
            self.assertEqual(status["stages"], [type(stage).__name__ for stage in stages])
        finally:
            await self.runtime.terminate()


if __name__ == "__main__":
    unittest.main()
