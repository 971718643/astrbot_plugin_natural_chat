"""Bridge final-answer control suffixes into per-event voice and existing plans."""

import json
import re
from dataclasses import replace

from astrbot.core.agent.message import TextPart

from .initiative_runtime import FLAG, STAGED
from .reply_markers import parse_reply
from .voice_markers import VoiceMarkerController, KEY as VOICE_KEY


MARKER_GUIDANCE = (
    "【回复方式】你可在最终回复正文末尾选择隐藏标记：[[语音]]表示这条适合说出来，"
    "[[文字]]表示这条只发文字。两者不同时使用；无标记沿用现有语音概率。"
    "用户明确要语音或文字时优先遵从，在末尾使用对应标记，避免落回随机选择。"
    "根据表达需要选择，温柔的简短回应、安慰或自然口头交流可以用语音；"
    "代码、网址、清单、长解释或需要复制的内容优先文字，不为使用功能而加标记。"
    "想稍后再自然接一句，也可在末尾加[[开口:秒数]]，范围1到7200；"
    "不足300秒按300秒处理。它与self_initiative工具共用同一项待办，二选一即可，"
    "工具预约优先；到时仍会重新判断，可能保持安静。不要每轮安排，不向群友宣告或承诺预约时间。"
    "开口标记同样需要当前具体话题和可补充的新内容；只有时间、客套、泛泛关心或想等人回复时不安排。"
    "标记只放在最后，不放入引用或代码；系统在发送和写入记忆前移除它们。"
)


class ReplyMarkerRuntime:
    def __init__(self, owner):
        self.owner = owner
        self.voice = VoiceMarkerController()
        self.last_result = None

    def initialize(self):
        self.voice.install()

    def terminate(self):
        self.voice.restore()

    def status(self):
        return {"voice_markers_enabled": bool(self.owner.config.get("reply_voice_markers_enabled", True)),
                "followup_markers_enabled": bool(self.owner.config.get("reply_followup_markers_enabled", True)),
                "private_markers_enabled": bool(self.owner.config.get("reply_private_markers_enabled", True)),
                "voice_controller": self.voice.status(),
                "last_result": self.last_result}

    @staticmethod
    def is_group(event):
        umo = event.unified_msg_origin
        parts = umo.split(":", 2) if isinstance(umo, str) else []
        return len(parts) == 3 and bool(parts[0].strip()) and parts[1] == "GroupMessage" and bool(parts[2].strip())

    @staticmethod
    def is_private(event):
        umo = event.unified_msg_origin
        parts = umo.split(":", 2) if isinstance(umo, str) else []
        return len(parts) == 3 and bool(parts[0].strip()) and parts[1] == "FriendMessage" and bool(parts[2].strip())

    def can_sanitize(self, event):
        return self.is_group(event) or self.is_private(event)

    def enabled(self, event):
        return self.owner.enabled and (self.is_group(event) or
                (self.is_private(event) and self.owner.config.get("reply_private_markers_enabled", True)))

    def observe(self, event):
        # Control suffixes must never be streamed before the final response hook.
        if self.can_sanitize(event) and (self.owner.config.get("reply_voice_markers_enabled", True)
                                    or self.owner.config.get("reply_followup_markers_enabled", True)
                                    or getattr(self.owner, "open_threads", None) is not None):
            event.set_extra("enable_streaming", False)

    def request(self, event, req):
        if not self.enabled(event):
            return
        current_input = getattr(req, "prompt", "")
        if (not event.get_extra(FLAG, False) and isinstance(current_input, str)
                and current_input.strip() and current_input.strip() != "<attachment>"):
            # A provider request may contain this turn's speech transcription
            # even when the original platform message has no plain text.
            event._reply_marker_current_input = " ".join(current_input.split())[:500]
        if not (self.owner.config.get("reply_voice_markers_enabled", True)
                or self.owner.config.get("reply_followup_markers_enabled", True)):
            return
        guidance = MARKER_GUIDANCE
        if self.owner.initiative.store.claim_capacity_remaining() == 0:
            guidance = (MARKER_GUIDANCE.split("想稍后", 1)[0]
                        + "自主接话暂不可预约，不使用开口标记，不承诺稍后主动回来。"
                        "语音和文字标记仍只放在最终正文末尾，不放引用或代码。")
        if self.is_private(event):
            guidance = guidance.replace("群友", "对方")
        if event.get_extra(FLAG, False):
            guidance = ("最终[[SPEAK]]正文末尾可以选[[语音]]或[[文字]]，"
                        "系统会隐藏标记。不得使用开口标记再次预约；[[SILENT]]时不加标记。")
        req.extra_user_content_parts.append(TextPart(text=guidance).mark_as_temp())

    def agent_begin(self, event, run_context):
        if self.can_sanitize(event):
            event._reply_marker_run_context = run_context

    @staticmethod
    def parse_controls(text):
        # EmotionAI's existing request flag may follow our final control suffix.
        # Keep its intent on the response until EmotionAI handles it, while
        # preventing either kind of metadata from entering audio or history.
        assessment = re.compile(r"(?:\s*\[需要情感评估\])+\s*$")
        suffix = assessment.search(text)
        prefix = text[:suffix.start()] if suffix else text
        parsed = parse_reply(prefix)
        if not parsed.had_markers:
            return parse_reply(text), ""
        before = assessment.search(parsed.text)
        clean = parsed.text[:before.start()].rstrip() if before else parsed.text
        parsed = replace(parsed, text=clean,
                         voice_mode=parsed.voice_mode if clean else None,
                         delay_seconds=parsed.delay_seconds if clean else None,
                         topic_control=parsed.topic_control if clean else None)
        return parsed, "[需要情感评估]" if clean and (suffix or before) else ""

    @classmethod
    def clean_result_chain(cls, response):
        result = getattr(response, "result_chain", None)
        for component in getattr(result, "chain", []) or []:
            if component.__class__.__name__.lower() == "plain":
                component.text = cls.parse_controls(str(component.text or ""))[0].text

    async def response(self, event, response):
        # One event may contain several tool responses. Never let intermediate
        # metadata survive into a later final answer with no topic control.
        event.set_extra("turn_taking.topic_control", None)
        event.set_extra("turn_taking.topic_response", None)
        if not self.can_sanitize(event):
            return
        parsed, assessment = self.parse_controls(str(response.completion_text or ""))
        clean_topic_response = parsed.text.replace("[需要情感评估]", "").strip()
        event.set_extra("turn_taking.topic_response", clean_topic_response)
        if (self.owner.enabled and not event.get_extra(FLAG, False)
                and clean_topic_response not in ("", "[[SPEAK]]", "[[SILENT]]")):
            event.set_extra("turn_taking.topic_control", parsed.topic_control)
        if not parsed.had_markers:
            return
        response.completion_text = parsed.text + assessment
        self.clean_result_chain(response)
        event._reply_marker_had_markers = True
        run_context = getattr(event, "_reply_marker_run_context", None)
        if run_context is not None:
            self.owner.initiative.clean_assistant_messages(run_context, parsed.text)
        mode = parsed.voice_mode if self.enabled(event) and self.owner.config.get("reply_voice_markers_enabled", True) else None
        if mode:
            event.set_extra(VOICE_KEY, mode)
        followup = None
        outcome = "not_requested"
        if parsed.delay_seconds is not None:
            if not self.enabled(event):
                outcome = "private_markers_disabled"
            elif event.get_extra(FLAG, False):
                outcome = "ignored_recursive_plan"
            elif not self.owner.config.get("reply_followup_markers_enabled", True):
                outcome = "disabled"
            elif (event.get_extra(STAGED)
                  and self.owner.initiative.store.claim_capacity_remaining() > 0):
                outcome = "existing_tool_plan_kept"
            else:
                followup = max(300, parsed.delay_seconds)
                user_text = " ".join(str(getattr(event, "_reply_marker_current_input", "")
                                         or getattr(event, "message_str", "") or "").split())
                if not user_text:
                    rows = self.owner.initiative.store.recent(event.unified_msg_origin)
                    user_text = next((row["text"] for row in reversed(rows)
                                      if row.get("role") == "user"), "")
                # Keep provenance with the real input, rather than using a short
                # acknowledgement such as '好呀' as the whole future intention.
                reason = ("话题依据（引用资料）：对方原话=" + user_text[:85]
                          + "；本轮已回复（勿复述）=" + parsed.text[:65])[:200]
                result = await self.owner.initiative.tool(event, "plan", followup, reason)
                outcome = "staged_after_delivery" if json.loads(result).get("ok") else "rejected_by_existing_policy"
        self.last_result = {"umo": event.unified_msg_origin, "voice_mode": mode,
                            "followup_delay_seconds": followup, "followup_outcome": outcome}

    def agent_done(self, event, run_context, response):
        if getattr(event, "_reply_marker_had_markers", False) and response is not None:
            # Other response hooks may also have cleaned their own metadata.
            text = self.parse_controls(str(response.completion_text or ""))[0].text
            text = text.replace("[需要情感评估]", "").strip()
            self.owner.initiative.clean_assistant_messages(run_context, text)
