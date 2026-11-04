"""Offline integration tests for runtime hooks against real durable state/policy.

Only the unavailable AstrBot framework boundaries are stubbed.  The plugin's
runtime, main hooks, InitiativeStore and ProactivePolicy are imported unchanged.
No network calls, model calls or real messages can be made by these tests.
"""

from copy import deepcopy
from datetime import datetime, timezone, timedelta
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parent
UMO = "default:GroupMessage:100001"
OTHER_UMO = "default:GroupMessage:999"
TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=TZ).timestamp()
CONFIG = {
    "private_self_initiated_enabled": False,
    "reply_private_markers_enabled": False,
    "proactive_all_groups": False,
    "proactive_enabled": True,
    "self_initiated_enabled": True,
    "proactive_group_whitelist": [UMO],
    "proactive_probability": 0.05,
    "proactive_cooldown_seconds": 300,
    "proactive_max_per_hour": 3,
    "proactive_max_per_day": 12,
    "proactive_quiet_start": 23,
    "proactive_quiet_end": 8,
    "proactive_startup_grace_seconds": 0,
}


class Plain:
    type = "plain"

    def __init__(self, text):
        self.text = text


class TextPart:
    def __init__(self, text):
        self.text = text
        self.temporary = False

    def mark_as_temp(self):
        self.temporary = True
        return self


class ToolSet:
    def __init__(self, names=()):
        self.names = list(names)

    def remove_tool(self, name):
        if name in self.names:
            self.names.remove(name)

    def __bool__(self):
        return bool(self.names)


class MessageSession:
    def __init__(self, platform, message_type, session_id):
        self.platform_name = platform
        self.message_type = message_type
        self.session_id = session_id

    @classmethod
    def from_str(cls, umo):
        return cls(*umo.split(":", 2))

    def __str__(self):
        return ":".join((self.platform_name, self.message_type, self.session_id))


class Event:
    def __init__(self, text="大家今晚准备聊些什么", umo=UMO, sender="u1", addressed=False):
        self.message_str = text
        self.unified_msg_origin = umo
        self.sender = sender
        self.extra = {}
        self.message_obj = types.SimpleNamespace(message_id="m1", group_id=umo.split(":")[-1])
        self.components = [Plain(text)]
        self.is_at_or_wake_command = addressed
        self.is_wake = False
        self.call_llm = False
        self._has_send_oper = False
        self.stopped = False
        self.delivered = []
        self.result = types.SimpleNamespace(chain=[Plain("刚才想到一件小事")])

    def set_extra(self, name, value):
        self.extra[name] = value

    def get_extra(self, name, default=None):
        return self.extra.get(name, default)

    def get_sender_id(self):
        return self.sender

    def get_sender_name(self):
        return "群友"

    def get_self_id(self):
        return "bot"

    def get_messages(self):
        return self.components

    def get_message_outline(self):
        return self.message_str

    def should_call_llm(self, value):
        self.call_llm = value

    def get_result(self):
        return self.result

    def stop_event(self):
        self.stopped = True


class CronMessageEvent(Event):
    def __init__(self, *, context, session, message, sender_id, sender_name, message_type):
        super().__init__(message, str(session), sender_id)
        self.context, self.session = context, session

    async def send(self, message):
        self.delivered.append(message)
        self._has_send_oper = True


class ProviderRequest:
    def __init__(self, **kwargs):
        self.prompt = "用户原文"
        self.system_prompt = "已有温柔人格"
        self.contexts = []
        self.conversation = None
        self.extra_user_content_parts = []
        self.func_tool = None
        self.__dict__.update(kwargs)


class Star:
    def __init__(self, context):
        self.context = context


class StarTools:
    get_data_dir = None  # Set separately for every test's temporary directory.


def decorator(*args, **kwargs):
    def apply(function):
        return function
    return apply


class Filter:
    EventMessageType = types.SimpleNamespace(GROUP_MESSAGE="group", ALL="all")

    def __getattr__(self, name):
        return decorator


class Stage:
    async def initialize(self, context):
        self.context = context


class PipelineScheduler:
    def __init__(self, context):
        self.context, self.stages = context, []


def module(name, **items):
    result = types.ModuleType(name)
    result.__dict__.update(items)
    return result


def load_subject():
    """Import plugin modules with stubs only for missing framework imports."""
    logger = types.SimpleNamespace(info=MagicMock(), exception=MagicMock())
    stubs = {
        "astrbot.api": module("astrbot.api", logger=logger, AstrBotConfig=dict),
        "astrbot.api.event": module("astrbot.api.event", filter=Filter(), AstrMessageEvent=Event),
        "astrbot.api.star": module("astrbot.api.star", Context=object, Star=Star,
                                   StarTools=StarTools, register=decorator),
        "astrbot.core.agent.message": module("astrbot.core.agent.message", TextPart=TextPart),
        "astrbot.core.agent.tool": module("astrbot.core.agent.tool", ToolSet=ToolSet),
        "astrbot.core.cron.events": module("astrbot.core.cron.events", CronMessageEvent=CronMessageEvent),
        "astrbot.core.pipeline.context": module("astrbot.core.pipeline.context", PipelineContext=lambda *args: args),
        "astrbot.core.pipeline.process_stage.method.agent_request": module(
            "astrbot.core.pipeline.process_stage.method.agent_request", AgentRequestSubStage=type("AgentRequestSubStage", (Stage,), {})),
        "astrbot.core.pipeline.result_decorate.stage": module(
            "astrbot.core.pipeline.result_decorate.stage", ResultDecorateStage=type("ResultDecorateStage", (Stage,), {})),
        "astrbot.core.pipeline.respond.stage": module(
            "astrbot.core.pipeline.respond.stage", RespondStage=type("RespondStage", (Stage,), {})),
        "astrbot.core.pipeline.scheduler": module("astrbot.core.pipeline.scheduler", PipelineScheduler=PipelineScheduler),
        "astrbot.core.platform.message_session": module("astrbot.core.platform.message_session", MessageSession=MessageSession),
        "astrbot.core.provider.entities": module("astrbot.core.provider.entities", ProviderRequest=ProviderRequest),
    }
    for name in list(stubs):
        parts = name.split(".")
        for index in range(1, len(parts)):
            parent = ".".join(parts[:index])
            if parent not in stubs:
                stubs[parent] = module(parent)
                stubs[parent].__path__ = []
    package_name = "_initiative_runtime_test_subject"
    package = module(package_name)
    package.__path__ = [str(SOURCE)]
    stubs[package_name] = package
    loaded = {}
    # Framework mocks need not leak into unrelated tests in the same process.
    with patch.dict(sys.modules, stubs):
        for name in ("initiative", "proactive", "initiative_runtime", "reply_markers",
                     "voice_markers", "reply_marker_runtime", "main"):
            qualified = package_name + "." + name
            spec = importlib.util.spec_from_file_location(qualified, SOURCE / (name + ".py"))
            subject = importlib.util.module_from_spec(spec)
            sys.modules[qualified] = subject
            spec.loader.exec_module(subject)
            loaded[name] = subject
    return loaded, logger


SUBJECT, LOGGER = load_subject()
Runtime = SUBJECT["initiative_runtime"]
Main = SUBJECT["main"]


class RuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.clock = [NOW]
        fake_time = types.SimpleNamespace(time=lambda: self.clock[0])
        self.runtime_time = patch.object(Runtime, "time", fake_time)
        self.main_time = patch.object(Main, "time", fake_time)
        self.runtime_time.start()
        self.main_time.start()
        self.addCleanup(self.runtime_time.stop)
        self.addCleanup(self.main_time.stop)
        StarTools.get_data_dir = staticmethod(lambda: Path(self.folder.name))
        self.conv = types.SimpleNamespace(cid="c1", persona_id="小鲸鱼·自然相处", history='[{"role":"user","content":"真实旧话"}]')
        self.context = types.SimpleNamespace(conversation_manager=types.SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="c1"),
            get_conversation=AsyncMock(return_value=self.conv)))
        self.iris_response = AsyncMock()
        self.iris = types.SimpleNamespace(name="astrbot_plugin_iris_chat_memory", activated=True,
                                         star_cls=types.SimpleNamespace(on_llm_response=self.iris_response))
        self.context.get_all_stars = lambda: [self.iris]
        self.turn = Main.TurnTaking(self.context, dict(CONFIG))
        self.runtime = self.turn.initiative
        self.runtime.store.now = lambda: self.clock[0]
        self.turn.proactive_policy.now = lambda: self.clock[0]
        self.turn.proactive_policy.started_at = NOW
        self.turn.proactive_policy.draw = lambda: 0.0
        LOGGER.exception.reset_mock()
        self.addCleanup(lambda: LOGGER.exception.assert_not_called())

    async def stage(self, event=None, reason="有件刚才的话题想再说一句", delay=300):
        event = event or Event()
        result = json.loads(await self.runtime.tool(event, "plan", delay, reason))
        self.assertTrue(result["ok"])
        return event

    async def arm(self, event=None, reason="有件刚才的话题想再说一句", delay=300):
        event = await self.stage(event, reason, delay)
        event._has_send_oper = True
        await self.turn.mark_replied(event)
        return event, self.runtime.store.list_pending(UMO)[0]

    async def due_event(self, *, revision=0):
        key = await self.runtime.conversation_key(UMO)
        job = self.runtime.store.schedule(UMO, 300, "刚才聊的电影有了新想法", key)
        event = Runtime.InitiativeEvent(self.runtime, job, revision)
        event.started_at = self.clock[0]
        return event

    def response(self, text):
        return types.SimpleNamespace(completion_text=text, result_chain=[Plain("不能从旧消息链漏出去")])

    @staticmethod
    def agent_context(text="[[SPEAK]]我刚想到那部电影的结尾"):
        return types.SimpleNamespace(messages=[
            types.SimpleNamespace(role="user", content=[TextPart("真实旧话")]),
            types.SimpleNamespace(role="assistant", content=[TextPart("真实旧回复")]),
            types.SimpleNamespace(role="user", content=[TextPart("<internal_self_initiative>")]),
            types.SimpleNamespace(role="assistant", content=[TextPart(text)]),
        ])

    async def test_plan_is_only_staged_and_failed_send_does_not_arm(self):
        event = await self.stage()
        self.assertEqual(self.runtime.store.list_pending(), [])
        self.assertFalse(self.runtime.store.path.exists())
        self.assertIn(Runtime.STAGED, event.extra)
        await self.turn.mark_replied(event)
        self.assertEqual(self.runtime.store.list_pending(), [])
        self.assertFalse(self.runtime.store.path.exists())
        self.assertEqual(self.turn._sess(UMO)["last_bot_ts"], 0)

    async def test_successful_send_arms_persistent_job_and_records_actual_reply(self):
        _, job = await self.arm()
        self.assertEqual(job["due_at"], NOW + 300)
        self.assertEqual(job["conversation_id"], await self.runtime.conversation_key(UMO))
        restarted = SUBJECT["initiative"].InitiativeStore(
            self.turn.config, self.runtime.store.path, now=lambda: self.clock[0])
        self.assertEqual(restarted.list_pending()[0]["id"], job["id"])
        self.assertEqual(restarted.recent(UMO)[-1]["text"], "刚才想到一件小事")
        self.assertEqual(self.turn._sess(UMO)["last_bot_ts"], NOW)

    async def test_later_successful_plan_replaces_one_job_and_cancel_clears_both_states(self):
        _, first = await self.arm()
        event = await self.stage(reason="换成一个更新的话题", delay=600)
        self.assertEqual(self.runtime.store.list_pending()[0]["id"], first["id"])
        event._has_send_oper = True
        await self.turn.mark_replied(event)
        jobs = self.runtime.store.list_pending()
        self.assertEqual(len(jobs), 1)
        self.assertNotEqual(jobs[0]["id"], first["id"])
        self.assertEqual(jobs[0]["reason"], "换成一个更新的话题")
        result = json.loads(await self.runtime.tool(event, "cancel"))
        self.assertTrue(result["canceled"])
        self.assertIsNone(event.get_extra(Runtime.STAGED))
        self.assertEqual(self.runtime.store.list_pending(), [])

    async def test_successful_intervening_normal_reply_cancels_old_plan(self):
        await self.arm()
        event = Event("有新话题了")
        event._has_send_oper = True
        await self.turn.mark_replied(event)
        self.assertEqual(self.runtime.store.list_pending(), [])

    async def test_switched_conversation_between_plan_and_send_never_arms(self):
        event = await self.stage()
        self.conv.persona_id = "新人格"
        event._has_send_oper = True
        await self.turn.mark_replied(event)
        self.assertEqual(self.runtime.store.list_pending(), [])

    async def test_due_request_has_no_tools_and_cannot_plan_itself_again(self):
        event = await self.due_event()
        req = ProviderRequest(func_tool=ToolSet(["self_initiative", "schedule", "shell"]))
        self.runtime.request(event, req)
        self.assertFalse(req.func_tool)
        self.assertEqual(req.extra_user_content_parts, [])
        before = self.runtime.store.list_pending()
        self.assertFalse(json.loads(await self.runtime.tool(event, "plan", 300, "继续说"))["ok"])
        self.assertEqual(self.runtime.store.list_pending(), before)

    async def test_normal_outside_scope_removes_only_initiative_tool(self):
        req = ProviderRequest(func_tool=ToolSet(["self_initiative", "lookup_weather"]))
        self.runtime.request(Event(umo=OTHER_UMO), req)
        self.assertEqual(req.func_tool.names, ["lookup_weather"])
        self.assertEqual(req.extra_user_content_parts, [])

    async def test_due_request_defers_iris_response_write_without_changing_normal_plugins(self):
        names = ["astrbot_plugin_turn_taking", "astrbot_plugin_iris_chat_memory", "other_plugin"]
        due = await self.due_event()
        due.plugins_name = list(names)
        self.runtime.request(due, ProviderRequest())
        self.assertTrue(due._initiative_iris_allowed)
        self.assertEqual(due.plugins_name, ["astrbot_plugin_turn_taking", "other_plugin"])
        self.iris_response.assert_not_awaited()
        normal = Event()
        normal.plugins_name = list(names)
        self.runtime.request(normal, ProviderRequest())
        self.assertEqual(normal.plugins_name, names)
        self.assertFalse(hasattr(normal, "_initiative_iris_allowed"))

    async def test_iris_records_only_successfully_delivered_clean_response_once(self):
        due = await self.due_event()
        due.plugins_name = ["astrbot_plugin_turn_taking", "astrbot_plugin_iris_chat_memory"]
        self.runtime.request(due, ProviderRequest())
        resp = self.response("[[SPEAK]]我想到一个新角度。[需要情感评估]")
        self.runtime.response(due, resp)
        self.assertIs(due._initiative_final_response, resp)
        self.iris_response.assert_not_awaited()
        await self.turn.mark_replied(due)
        self.iris_response.assert_not_awaited()
        self.assertEqual(self.runtime.store.recent(UMO), [])
        # TTS may have replaced all Plain parts with a Record; use the clean
        # model text as the memory fallback after the successful send hook.
        due.result = types.SimpleNamespace(chain=[])
        due._has_send_oper = True
        await self.turn.mark_replied(due)
        await self.turn.mark_replied(due)
        self.iris_response.assert_awaited_once_with(due, resp)
        self.assertEqual(resp.completion_text, "我想到一个新角度。")
        self.assertEqual(self.runtime.store.recent(UMO)[-1]["text"], "我想到一个新角度。")

    async def test_silent_decision_never_has_an_iris_final_response_to_record(self):
        due = await self.due_event()
        due.plugins_name = ["astrbot_plugin_turn_taking", "astrbot_plugin_iris_chat_memory"]
        self.runtime.request(due, ProviderRequest())
        self.runtime.response(due, self.response("[[SILENT]]"))
        self.assertIsNone(due._initiative_final_response)
        await self.turn.mark_replied(due)
        self.iris_response.assert_not_awaited()
        self.assertEqual(self.runtime.store.recent(UMO), [])

    async def test_normal_request_preserves_persona_prompt_history_and_toolset(self):
        req = ProviderRequest(conversation=self.conv,
                              contexts=[{"role": "user", "content": "真实历史"}],
                              func_tool=ToolSet(["self_initiative", "lookup_weather"]))
        before = deepcopy((req.prompt, req.system_prompt, req.contexts, self.conv.__dict__))
        self.runtime.request(Event(), req)
        self.assertEqual((req.prompt, req.system_prompt, req.contexts, self.conv.__dict__), before)
        self.assertIs(req.conversation, self.conv)
        self.assertEqual(req.func_tool.names, ["self_initiative", "lookup_weather"])
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertTrue(req.extra_user_content_parts[0].temporary)
        self.assertIn("当前没有待考虑的接话", req.extra_user_content_parts[0].text)

    async def test_silent_or_unmarked_response_cannot_leak_text_or_result_chain(self):
        for text in ("[[SILENT]]", "未经批准的普通回复", "[[SPEAK]][[SILENT]]", "[[SPEAK]]", "[[SPEAK]]" + "啊" * 401):
            with self.subTest(text=text[:30]):
                event = await self.due_event()
                rc = self.agent_context(text)
                event._initiative_run_context = rc
                resp = self.response(text)
                self.runtime.response(event, resp)
                self.assertEqual(resp.completion_text, "")
                self.assertIsNone(resp.result_chain)
                self.assertFalse(event.approved)
                self.assertTrue(event.stopped)
                self.assertEqual(rc.messages[-1].content[0].text, "")
                self.assertEqual(rc.messages[1].content[0].text, "真实旧回复")

    async def test_approved_response_cleans_markers_in_payload_and_saved_assistant(self):
        event = await self.due_event()
        rc = self.agent_context()
        event._initiative_run_context = rc
        resp = self.response("[[SPEAK]]  我刚想到那部电影的结尾。[需要情感评估] ")
        self.runtime.response(event, resp)
        self.assertTrue(event.approved)
        self.assertFalse(event.stopped)
        self.assertEqual(resp.completion_text, "我刚想到那部电影的结尾。")
        self.assertIsNone(resp.result_chain)
        self.assertEqual(rc.messages[-1].content[0].text, resp.completion_text)
        self.runtime.agent_done(event, rc, resp)
        self.assertEqual(rc.messages[-1].content[0].text, "我刚想到那部电影的结尾。")

    async def test_new_real_message_during_generation_suppresses_even_valid_speak(self):
        event = await self.due_event()
        self.runtime.observe(Event("新来的聊天已经换了话题"))
        self.assertEqual(self.runtime.revisions[UMO], 1)
        resp = self.response("[[SPEAK]]迟到的旧话题回复")
        self.runtime.response(event, resp)
        self.assertFalse(event.approved)
        self.assertEqual(resp.completion_text, "")
        self.assertIsNone(resp.result_chain)

    async def test_due_send_gate_blocks_unapproved_new_chat_or_switched_persona(self):
        unapproved = await self.due_event()
        await unapproved.send("不能送达")
        self.assertEqual(unapproved.delivered, [])
        self.assertTrue(unapproved.stopped)

        stale = await self.due_event()
        stale.approved = True
        self.runtime.observe(Event("另一个群友刚刚发了新话"))
        await stale.send("不合时宜的旧回复")
        self.assertEqual(stale.delivered, [])

        switched = await self.due_event(revision=1)
        switched.approved = True
        self.conv.persona_id = "已切换的人格"
        await switched.send("旧人格的回复")
        self.assertEqual(switched.delivered, [])
        self.assertTrue(switched.stopped)

    async def test_due_send_gate_allows_current_approved_reply(self):
        event = await self.due_event()
        event.approved = True
        await event.send("这才是通过审批的当前回复")
        self.assertEqual(event.delivered, ["这才是通过审批的当前回复"])
        self.assertTrue(event._has_send_oper)

    async def test_agent_begin_marks_only_latest_internal_user_as_no_save(self):
        event = await self.due_event()
        rc = self.agent_context()
        self.runtime.agent_begin(event, rc)
        self.assertIs(event._initiative_run_context, rc)
        self.assertTrue(rc.messages[2]._no_save)
        self.assertFalse(getattr(rc.messages[0], "_no_save", False))
        normal = Event()
        normal_rc = self.agent_context()
        self.runtime.agent_begin(normal, normal_rc)
        self.assertFalse(any(getattr(m, "_no_save", False) for m in normal_rc.messages))
        self.assertFalse(hasattr(normal, "_initiative_run_context"))

    async def test_pending_plan_prevents_random_draw_but_explicit_address_still_works(self):
        key = await self.runtime.conversation_key(UMO)
        self.runtime.store.schedule(UMO, 300, "保持一项自主考虑", key)
        self.turn.proactive_policy.select = MagicMock(side_effect=AssertionError("random must not run"))
        idle = Event()
        await self.turn.flow(idle)
        self.turn.proactive_policy.select.assert_not_called()
        self.assertTrue(idle.call_llm)
        self.assertFalse(idle.is_at_or_wake_command)
        self.assertEqual(len(self.runtime.store.list_pending()), 1)
        addressed = Event("小鲸鱼你怎么看", addressed=True)
        await self.turn.flow(addressed)
        self.assertTrue(addressed.is_at_or_wake_command)
        self.assertFalse(addressed.call_llm)
        self.turn.proactive_policy.select.assert_not_called()

    async def test_no_pending_plan_retains_original_random_fallback(self):
        event = Event()
        await self.turn.flow(event)
        self.assertTrue(event.is_at_or_wake_command)
        self.assertTrue(event.get_extra("turn_taking.proactive"))
        self.assertFalse(event.call_llm)
        self.assertEqual(len(self.turn.proactive_policy.history[UMO]), 1)

    async def test_internal_observe_does_not_fabricate_user_memory_or_revision(self):
        event = await self.due_event()
        self.runtime.observe(event)
        self.assertEqual(self.runtime.revisions, {})
        self.assertEqual(self.runtime.store.recent(UMO), [])
        self.runtime.observe(Event("群友真实说过的话"))
        self.assertEqual(self.runtime.store.recent(UMO)[0]["speaker_id"], "u1")
        self.assertEqual(self.runtime.store.recent(UMO)[0]["text"], "群友真实说过的话")


if __name__ == "__main__":
    unittest.main(verbosity=2)
