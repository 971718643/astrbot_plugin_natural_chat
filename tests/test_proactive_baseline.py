import ast
from datetime import datetime
import importlib.util
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("proactive_policy_baseline", ROOT.parent / "proactive.py")
policy_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy_module)
Policy = policy_module.ProactivePolicy
TZ = policy_module.CHINA_TIME
UMO = "default:GroupMessage:100001"
CONFIG = {"private_self_initiated_enabled": False, "proactive_all_groups": False, "proactive_enabled": True, "proactive_group_whitelist": [UMO],
          "proactive_probability": 0.05, "proactive_cooldown_seconds": 300,
          "proactive_max_per_hour": 3, "proactive_max_per_day": 12,
          "proactive_quiet_start": 23, "proactive_quiet_end": 8,
          "proactive_startup_grace_seconds": 0}


def ts(hour=12, minute=0, day=30):
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ).timestamp()


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.clock = [ts()]
        self.path = Path(self.folder.name) / "counts.json"
        self.policy = Policy(dict(CONFIG), self.path, now=lambda: self.clock[0], draw=lambda: 0.01)

    def test_selection_and_persistent_cooldown_after_restart(self):
        self.assertTrue(self.policy.select(UMO, "这个游戏今晚有人一起玩吗")[0])
        restarted = Policy(dict(CONFIG), self.path, now=lambda: self.clock[0], draw=lambda: 0)
        self.assertEqual(restarted.select(UMO, "换一个话题继续聊聊")[1], "proactive_cooldown")

    def test_probability_boundary(self):
        self.policy.draw = lambda: 0.05
        self.assertFalse(self.policy.select(UMO, "这个游戏今晚有人一起玩吗")[0])
        self.policy.draw = lambda: 0.049
        self.assertTrue(self.policy.select(UMO, "这个游戏今晚有人一起玩吗")[0])

    def test_scope_quotes_mentions_and_commands_do_not_call_random(self):
        self.policy.draw = lambda: (_ for _ in ()).throw(AssertionError("random must not run"))
        cases = [("other:GroupMessage:123", "正常的群聊消息", {}),
                 (UMO, "这个问题请你来回答", {"other_mention": True}),
                 (UMO, "他说的那句话是什么意思", {"quoted": True}),
                 (UMO, "/some_command", {}), (UMO, "哈哈哈哈哈哈哈哈", {}),
                 (UMO, "好", {})]
        for umo, text, kw in cases:
            with self.subTest(umo=umo, text=text):
                self.assertFalse(self.policy.select(umo, text, **kw)[0])

    def test_quiet_window_and_startup_grace(self):
        for hour in (23, 0, 7):
            self.clock[0] = ts(hour)
            self.policy.started_at = self.clock[0] - 1
            self.assertEqual(self.policy.select(UMO, "大家现在准备聊些什么")[1], "quiet_hours")
        self.clock[0] = ts(8)
        self.policy.started_at = self.clock[0] - 1
        self.assertTrue(self.policy.select(UMO, "大家现在准备聊些什么")[0])
        self.policy.config["proactive_startup_grace_seconds"] = 300
        self.policy.started_at = self.clock[0]
        self.assertEqual(self.policy.select(UMO, "大家现在准备聊些什么")[1], "proactive_startup_grace")

    def test_recent_direct_reply_suppresses_random_participation(self):
        self.assertEqual(self.policy.select(UMO, "大家现在准备聊些什么", last_bot_ts=self.clock[0] - 10)[1],
                         "proactive_cooldown")

    def test_rolling_hour_and_local_day_caps(self):
        self.policy.history[UMO] = [self.clock[0] - 1200, self.clock[0] - 800, self.clock[0] - 400]
        self.assertEqual(self.policy.select(UMO, "大家现在准备聊些什么")[1], "proactive_hour_cap")
        self.clock[0] = ts(22)
        self.policy.history[UMO] = [ts(8) + i * 2000 for i in range(12)]
        self.assertEqual(self.policy.select(UMO, "大家现在准备聊些什么")[1], "proactive_day_cap")
        self.clock[0] = ts(8, day=31)
        self.assertTrue(self.policy.select(UMO, "大家现在准备聊些什么")[0])


class Plain:
    def __init__(self, text):
        self.text = text
        self.type = "plain"


class At:
    def __init__(self, qq):
        self.qq = qq
        self.type = "at"


class Reply:
    type = "reply"


class Event:
    def __init__(self, text="大家今晚准备聊些什么", *, addressed=False, components=None):
        self.message_str = text
        self.unified_msg_origin = UMO
        self.is_at_or_wake_command = addressed
        self.is_wake = False
        self.call_llm = False
        self._has_send_oper = False
        self.extra = {}
        self.components = components if components is not None else [Plain(text)]

    def get_sender_id(self):
        return "u1"

    def get_self_id(self):
        return "bot"

    def get_messages(self):
        return self.components

    def set_extra(self, k, v):
        self.extra[k] = v

    def should_call_llm(self, v):
        self.call_llm = v


def make_turn(clock, policy):
    tree = ast.parse((ROOT.parent / "main.py").read_text(encoding="utf-8"))
    original_class = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    wanted = {"_sess", "_has_other_at", "_is_closing", "_decide", "flow", "mark_replied"}
    methods = [n for n in original_class.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in wanted]
    for method in methods:
        method.decorator_list = [d for d in method.decorator_list if isinstance(d, ast.Name) and d.id == "staticmethod"]
    cls = ast.ClassDef(name="LiveMethods", bases=[], keywords=[], body=methods, decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    namespace = {"time": types.SimpleNamespace(time=lambda: clock[0]), "AstrMessageEvent": Event,
                 "logger": types.SimpleNamespace(info=lambda *a: None, exception=lambda *a: None)}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in {"CLOSING_WORDS", "QUESTION_HINTS"} for t in node.targets):
            namespace[node.targets[0].id] = ast.literal_eval(node.value)
    exec(compile(module, "deployed_turn_methods", "exec"), namespace)
    instance = namespace["LiveMethods"]()
    instance.sessions = {}
    instance.enabled, instance.dialog_timeout, instance.max_turns = True, 180, 5
    instance.bot_names = ("小鲸鱼",)
    instance.proactive_policy = policy
    # The legacy policies/turn transitions remain exercised unchanged.  This
    # isolated baseline intentionally has no staged autonomous follow-up.
    instance.initiative = types.SimpleNamespace(
        observe=lambda event: None,
        has_pending=lambda umo: False,
        sent=AsyncMock(),
    )
    instance.reply_markers = types.SimpleNamespace(observe=lambda event: None)
    return instance


class FlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.clock = [ts()]
        policy = Policy(dict(CONFIG), Path(self.folder.name) / "counts.json", now=lambda: self.clock[0], draw=lambda: 0)
        self.turn = make_turn(self.clock, policy)

    async def test_selected_proactive_uses_default_chain_and_honest_trigger_markers(self):
        event = Event()
        await self.turn.flow(event)
        self.assertTrue(event.is_at_or_wake_command)
        self.assertFalse(event.call_llm)
        self.assertTrue(event.extra["turn_taking.proactive"])
        self.assertTrue(event.extra["_active_trigger"])
        self.assertEqual(event.message_str, "大家今晚准备聊些什么")

    async def test_explicit_address_still_works_at_night_or_over_budget(self):
        self.clock[0] = ts(23)
        self.turn.proactive_policy.config["proactive_max_per_day"] = 0
        event = Event("小鲸鱼你好", addressed=True)
        await self.turn.flow(event)
        self.assertTrue(event.is_at_or_wake_command)
        self.assertNotIn("turn_taking.proactive", event.extra)

    async def test_other_addressee_quote_and_existing_llm_block_stay_silent(self):
        events = [Event(components=[At("u2"), Plain("大家今晚准备聊些什么")]),
                  Event(components=[Reply(), Plain("大家今晚准备聊些什么")]), Event()]
        events[-1].call_llm = True
        for event in events:
            with self.subTest(event=event):
                await self.turn.flow(event)
                self.assertFalse(event.is_at_or_wake_command)
                self.assertTrue(event.call_llm)

    async def test_media_only_and_closing_messages_do_not_start_chat(self):
        for event in (Event("[图片][图片]", components=[]), Event("谢谢大家先这样吧")):
            await self.turn.flow(event)
            self.assertFalse(event.is_at_or_wake_command)

    async def test_successful_delivery_only_updates_dialog_state(self):
        event = Event(addressed=True)
        await self.turn.flow(event)
        await self.turn.mark_replied(event)
        self.assertEqual(self.turn.sessions[UMO]["last_bot_ts"], 0)
        event._has_send_oper = True
        await self.turn.mark_replied(event)
        self.assertEqual(self.turn.sessions[UMO]["last_bot_ts"], self.clock[0])

    async def test_proactive_dialog_does_not_capture_long_unaddressed_monologue(self):
        event = Event()
        await self.turn.flow(event)
        event._has_send_oper = True
        await self.turn.mark_replied(event)
        for _ in range(2):
            self.clock[0] += 2
            followup = Event("我们接着刚才的话题说")
            await self.turn.flow(followup)
            self.assertTrue(followup.is_at_or_wake_command)
            followup._has_send_oper = True
            await self.turn.mark_replied(followup)
        third = Event("我们接着刚才的话题说")
        await self.turn.flow(third)
        self.assertFalse(third.is_at_or_wake_command)


if __name__ == "__main__":
    unittest.main(verbosity=2)
