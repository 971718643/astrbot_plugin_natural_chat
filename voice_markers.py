"""Per-reply voice decisions without changing shared TTS configuration."""

from copy import copy
import inspect


KEY = "turn_taking.reply_voice_mode"


class VoiceMarkerController:
    """A reversible, concurrency-safe shim around AstrBot's native TTS stage.

    Event values are ``voice`` or ``text``. Unmarked events keep the original
    stage object and probability. All native enable, session and provider gates,
    synthesis, and fallback behavior continue to run inside the original method.
    """

    def __init__(self, stage_type=None):
        self._stage_type = stage_type
        self._original = None
        self._wrapper = None
        self._generation = None

    def install(self):
        if self._generation and self._generation["active"]:
            return False
        if self._stage_type is None:
            from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
            self._stage_type = ResultDecorateStage
        original = self._stage_type.process
        generation = {"active": True}

        def target_for(stage, event):
            target = stage
            mode = event.get_extra(KEY) if generation["active"] else None
            if mode in ("voice", "text"):
                target = copy(stage)
                # Native code uses <=, so probability zero would still accept
                # an exact zero draw. A negative value deterministically skips.
                target.tts_trigger_probability = 1.0 if mode == "voice" else -1.0
            return target

        if inspect.isasyncgenfunction(original):
            async def process(stage, event):
                async for item in original(target_for(stage, event), event):
                    yield item
        else:
            # PipelineScheduler distinguishes coroutine stages from generators.
            # This function must contain no yield: a zero-yield generator would
            # prevent its downstream RespondStage from running altogether.
            async def process(stage, event):
                return await original(target_for(stage, event), event)

        self._original, self._wrapper, self._generation = original, process, generation
        self._stage_type.process = process
        return True

    def restore(self):
        if self._generation:
            self._generation["active"] = False
        # A later plugin may wrap this hook. Do not overwrite that plugin;
        # deactivation still makes this generation inert inside its chain.
        if self._stage_type and self._wrapper and self._stage_type.process is self._wrapper:
            self._stage_type.process = self._original
            return True
        return False

    def status(self):
        active = bool(self._generation and self._generation["active"])
        return {"active": active,
                "owns_process": bool(self._stage_type and self._wrapper
                                     and self._stage_type.process is self._wrapper),
                "event_key": KEY}
