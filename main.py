# -*- coding: utf-8 -*-
"""
astrbot_plugin_turn_taking v0.2 —— 群聊话权状态机

AstrBot 默认的唤醒链路（v4.29.1: pipeline/waking_check/stage.py）只认三件事：
    @机器人 / 引用机器人的消息 / wake_prefix 前缀，私聊另算。
其余群消息在 WakingCheckStage 就被 event.stop_event() 掐掉，模型根本看不到——
所以「刚聊完一句，对方接着说，机器人装死」不是模型的问题，是消息没进链路。

本插件在 ProcessStage 里改 event.is_at_or_wake_command，把「话轮还在我这儿」的消息
重新放行给默认 LLM 链路；同时用 should_call_llm(True) 把不该接的消息挡掉。

状态机：
    IDLE ──被@/被点名──► DIALOG(partner, turns)
    DIALOG ──同一人继续说话、没@别人、未超时──► DIALOG，turns+1（不用再@）
    DIALOG ──轮数到顶 / 收尾语 / 超时 / 第三人插话──► IDLE
"""

import time
import sys

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools, register
from .proactive import ProactivePolicy
from .initiative_runtime import InitiativeRuntime
from .reply_marker_runtime import ReplyMarkerRuntime
from .open_thread_runtime import OpenThreadRuntime

CLOSING_WORDS = (
    "谢谢", "好的", "好滴", "好嘞", "行吧", "行了", "嗯嗯", "收到", "明白", "懂了",
    "晚安", "睡了", "我睡了", "拜拜", "88", "再见", "下次聊", "先这样", "不聊了",
    "笑死", "ok", "OK", "Ok",
)
QUESTION_HINTS = (
    "?", "？", "吗", "呢", "怎么", "什么", "为啥", "为什么", "哪", "谁", "多少",
    "能不能", "可不可以", "是不是", "有没有", "帮我", "你说",
)


@register(
    "astrbot_plugin_turn_taking",
    "dsh & OpenAI Codex",
    "群聊话权状态机：连续对话 + 自动收尾",
    "v0.7.2",
)
class TurnTaking(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.sessions = {}
        self.enabled = True           # 总开关
        self.dialog_timeout = 180.0   # 对话态静默多久后退出（秒）
        self.max_turns = 5            # 同一个人最多连续聊几轮
        self.bot_names = ("小鲸鱼", "鲸鱼娘", "DeepSeek", "deepseek", "大肥鱼")
        self.config = config
        data_dir = StarTools.get_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        self.proactive_policy = ProactivePolicy(config, data_dir / "proactive-attempts.json")
        self.initiative = InitiativeRuntime(self, data_dir)
        self.reply_markers = ReplyMarkerRuntime(self)
        self.open_threads = OpenThreadRuntime(self, data_dir)

    async def initialize(self):
        self.reply_markers.initialize()
        try:
            await self.initiative.initialize()
        except BaseException:
            self.reply_markers.terminate()
            raise
        self.context.register_web_api(
            "/astrbot_plugin_turn_taking/status", self.status, ["GET"],
            "群聊话权与主动参与状态（只读）",
        )
        logger.info("[turn_taking] Proactive participation enabled=%s, scope=%s, groups=%s, probability=%s",
                    self.config.get("proactive_enabled"),
                    "all_groups" if self.config.get("proactive_all_groups", True) else "whitelist",
                    self.config.get("proactive_group_whitelist"),
                    self.config.get("proactive_probability"))

    async def status(self):
        return {"success": True, "enabled": self.enabled,
                "scope": "all_groups" if self.config.get("proactive_all_groups", True) else "whitelist",
                "private_self_initiated_enabled": bool(self.config.get("private_self_initiated_enabled", True)),
                "configuration": dict(self.config),
                "normal_max_turns": self.max_turns,
                "dialog_timeout_seconds": self.dialog_timeout,
                "startup_grace_remaining_seconds": max(
                    0, float(self.config.get("proactive_startup_grace_seconds", 300))
                    - (time.time() - self.proactive_policy.started_at)),
                "proactive_attempts_recorded": {
                    key: len(values) for key, values in self.proactive_policy.history.items()},
                "self_initiative": self.initiative.status(),
                "reply_markers": self.reply_markers.status(),
                "open_threads": self.open_threads.status()}

    async def terminate(self):
        self.reply_markers.terminate()
        await self.initiative.terminate()

    @filter.llm_tool(name="self_initiative")
    async def self_initiative(self, event: AstrMessageEvent, action: str,
                              delay_seconds: int = 300, reason: str = "") -> str:
        """自主安排稍后是否再接一句。需要当前具体话题中尚未展开的点或有用的新补充，不用等待用户明确要求；只有时间或泛泛关心时不安排。只管理当前会话的一项待办，不是用户提醒或定时广播。plan在本轮成功回复后生效；到时可放弃。

        Args:
            action(string): plan预约、cancel取消、status查看。
            delay_seconds(number): plan的延时，300到7200的整数秒；其他操作忽略。
            reason(string): plan的简短话题意图，不预写要发送的台词。
        """
        return await self.initiative.tool(event, action, delay_seconds, reason)

    @filter.on_waiting_llm_request()
    async def initiative_waiting(self, event: AstrMessageEvent):
        self.initiative.waiting(event)

    @filter.on_llm_request(priority=-sys.maxsize)
    async def initiative_request(self, event: AstrMessageEvent, req):
        self.initiative.request(event, req)
        self.reply_markers.request(event, req)
        await self.open_threads.request(event, req)

    @filter.on_agent_begin(priority=sys.maxsize)
    async def initiative_agent_begin(self, event: AstrMessageEvent, run_context):
        self.reply_markers.agent_begin(event, run_context)
        self.initiative.agent_begin(event, run_context)

    @filter.on_llm_response(priority=sys.maxsize)
    async def initiative_response(self, event: AstrMessageEvent, resp):
        await self.reply_markers.response(event, resp)
        self.open_threads.response(event, resp)
        self.initiative.response(event, resp)

    @filter.on_agent_done(priority=sys.maxsize)
    async def initiative_agent_done(self, event: AstrMessageEvent, run_context, resp):
        self.reply_markers.agent_done(event, run_context, resp)
        self.initiative.agent_done(event, run_context, resp)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=sys.maxsize)
    async def observe_private(self, event: AstrMessageEvent):
        """Observe actual private input without changing its native wake/reply rules."""
        if not self.enabled or not self.reply_markers.is_private(event):
            return
        self.initiative.observe(event)
        self.reply_markers.observe(event)

    # ------------------------------------------------------------------ util
    def _sess(self, umo: str) -> dict:
        st = self.sessions.get(umo)
        if st is None:
            st = {
                "partner": None,      # 正在和谁聊
                "turns": 0,
                "last_bot_ts": 0.0,
                "last_speaker": None,  # 群里最后一条消息是谁发的
                "proactive_dialog": False,
            }
            self.sessions[umo] = st
        return st

    @staticmethod
    def _has_other_at(event: AstrMessageEvent) -> bool:
        """这条消息有没有 @ 别人"""
        try:
            me = str(event.get_self_id())
            for comp in event.get_messages():
                qq = getattr(comp, "qq", None)
                if qq is None:
                    continue
                if str(qq) not in (me, "all"):
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def _is_closing(text: str) -> bool:
        if not text:
            return False
        t = text.strip().lower()
        if any(q in t for q in QUESTION_HINTS):
            return False
        return any(w.lower() in t for w in CLOSING_WORDS)

    def _decide(self, event: AstrMessageEvent, text: str):
        """返回 (allow, reason, turns)"""
        st = self._sess(event.unified_msg_origin)
        now = time.time()
        try:
            sender = str(event.get_sender_id())
        except Exception:
            sender = "unknown"

        # 超时 → 退出对话态
        if st["partner"] and now - st["last_bot_ts"] > self.dialog_timeout:
            st["partner"] = None
            st["turns"] = 0

        addressed = bool(getattr(event, "is_at_or_wake_command", False))
        if not addressed and text:
            addressed = any(n in text for n in self.bot_names)

        if addressed:
            st.update(partner=sender, turns=0, proactive_dialog=False)
            return True, "addressed", 0

        same_partner = st["partner"] is not None and sender == st["partner"]
        recent = now - st["last_bot_ts"] <= self.dialog_timeout

        if same_partner and recent:
            if self._has_other_at(event):
                st["partner"] = None
                st["turns"] = 0
                return False, "at_other(在叫别人)", 0
            turn_cap = min(self.max_turns, 2) if st.get("proactive_dialog") else self.max_turns
            if st["turns"] >= turn_cap:
                st["partner"] = None
                st["turns"] = 0
                return False, "turn_cap(聊够%d轮)" % turn_cap, 0
            if st["turns"] >= 1 and self._is_closing(text):
                st["partner"] = None
                st["turns"] = 0
                return False, "closing(收尾语)", 0
            st["turns"] += 1
            return True, "continue(%d)" % st["turns"], st["turns"]

        if st["partner"] and not same_partner:
            st["partner"] = None
            st["turns"] = 0
            return False, "interrupted(别人插话)", 0

        return False, "idle(没人叫)", 0

    # ------------------------------------------------------------------ hooks
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def flow(self, event: AstrMessageEvent):
        try:
            self.initiative.observe(event)
            self.reply_markers.observe(event)
            if not self.enabled:
                return
            text = (event.message_str or "").strip()
            if text.startswith("/"):
                return  # 指令交给别的插件
            try:
                if str(event.get_sender_id()) == str(event.get_self_id()):
                    return  # 自己发的
            except Exception:
                pass

            st = self._sess(event.unified_msg_origin)
            allow, reason, turns = self._decide(event, text)
            st["last_speaker"] = str(event.get_sender_id())

            if (not allow and reason == "idle(没人叫)" and not self._is_closing(text)
                    and not self.initiative.has_pending(event.unified_msg_origin)):
                components = event.get_messages()
                plain_text = " ".join(
                    str(getattr(comp, "text", "")) for comp in components
                    if comp.__class__.__name__.lower() == "plain"
                )
                selected, proactive_reason = self.proactive_policy.select(
                    event.unified_msg_origin, plain_text,
                    last_bot_ts=st["last_bot_ts"],
                    other_mention=bool(getattr(event, "call_llm", False)) or any(
                        getattr(comp, "qq", None) is not None for comp in components),
                    quoted=any(str(getattr(comp, "type", "")).lower().endswith("reply")
                               or comp.__class__.__name__.lower() == "reply" for comp in components),
                )
                if selected:
                    allow, reason, turns = True, proactive_reason, 0
                    st.update(partner=str(event.get_sender_id()), turns=0, proactive_dialog=True)
                    event.set_extra("turn_taking.proactive", True)
                    event.set_extra("_active_trigger", True)

            head = text.replace("\n", " ")[:36]
            if allow:
                event.is_at_or_wake_command = True
                event.is_wake = True
                logger.info(
                    "[turn_taking] ✓ 接话 %s | %s | turns=%d | %r",
                    event.unified_msg_origin, reason, turns, head,
                )
            else:
                event.should_call_llm(True)
                logger.info(
                    "[turn_taking] ✗ 沉默 %s | %s | %r",
                    event.unified_msg_origin, reason, head,
                )
        except Exception:
            logger.exception("[turn_taking] flow 异常，已放行")

    @filter.after_message_sent()
    async def mark_replied(self, event: AstrMessageEvent):
        """机器人发出去了，才把话权交回它"""
        try:
            if not getattr(event, "_has_send_oper", False):
                return
            st = self._sess(event.unified_msg_origin)
            st["last_bot_ts"] = time.time()
            if st.get("last_speaker"):
                st["partner"] = st["last_speaker"]
            self.open_threads.sent(event)
            await self.initiative.sent(event)
        except Exception:
            logger.exception("[turn_taking] mark_replied 异常")

    # ------------------------------------------------------------------ cmd
    @filter.command("tt")
    async def tt(self, event: AstrMessageEvent):
        """话权状态机：/tt | /tt on | /tt off | /tt max 3 | /tt timeout 120 | /tt reset"""
        try:
            try:
                if not event.is_admin():
                    yield event.plain_result("这个只有管理员能改哦")
                    return
            except Exception:
                pass

            args = (event.message_str or "").split()
            sub = args[1].lower() if len(args) > 1 else ""

            if sub == "on":
                self.enabled = True
                msg = "已开启：没叫我不吭声，叫了能连聊 %d 轮" % self.max_turns
            elif sub == "off":
                self.enabled = False
                msg = "已关闭（回到 AstrBot 原生行为）"
            elif sub == "max" and len(args) > 2:
                self.max_turns = max(1, min(10, int(args[2])))
                msg = "最大连续轮数 = %d" % self.max_turns
            elif sub == "timeout" and len(args) > 2:
                self.dialog_timeout = max(30.0, float(args[2]))
                msg = "对话态超时 = %.0f 秒" % self.dialog_timeout
            elif sub == "reset":
                self.sessions.clear()
                msg = "所有群的状态已清空"
            else:
                active = sum(1 for s in self.sessions.values() if s["partner"])
                msg = (
                    "%s\n对话中的群 %d / 已知群 %d\n最大轮数 %d，超时 %.0fs"
                    % (
                        "开启中" if self.enabled else "已关闭",
                        active, len(self.sessions),
                        self.max_turns, self.dialog_timeout,
                    )
                )
                if self.config.get("proactive_enabled", False):
                    msg += "\n已启用指定群的适度主动接话（冷却和限次独立生效）"
            yield event.plain_result(msg)
        except Exception as e:
            logger.exception("[turn_taking] 命令异常")
            yield event.plain_result("命令出错：%s" % e)
