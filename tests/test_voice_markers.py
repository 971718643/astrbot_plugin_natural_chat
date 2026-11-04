"""Use the real native TTS decision/conversion AST with framework edges stubbed."""

import ast
import asyncio
from copy import deepcopy
import importlib.util
import inspect
from pathlib import Path
import types
import unittest
from collections.abc import AsyncGenerator
from typing import cast


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("voice_markers", ROOT / "voice_markers.py")
voice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(voice)
NATIVE = ROOT / "tests/fixtures/native_result_decorate.py"
SCHEDULER = ROOT / "tests/fixtures/native_scheduler.py"


class Plain:
    def __init__(self, text):
        self.text = text


class Record:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Event:
    def __init__(self, mode=None, text="这是一条完整的回复"):
        self.mode = mode
        self.unified_msg_origin = "default:GroupMessage:111"
        self.result = types.SimpleNamespace(chain=[Plain(text)], is_llm_result=lambda: True)
        self.session_enabled = True
        self.stage_seen = None
        self.stopped = False

    def get_extra(self, key, default=None):
        return self.mode if key == voice.KEY else default

    def get_result(self):
        return self.result

    def is_stopped(self):
        return self.stopped


class Provider:
    def __init__(self, error=None, path="/tmp/voice.mp3"):
        self.calls = []
        self.error, self.path = error, path

    async def get_audio(self, text):
        self.calls.append(text)
        await asyncio.sleep(0)
        if self.error:
            raise self.error
        return self.path


class SessionManager:
    @staticmethod
    async def should_process_tts_request(event):
        return event.session_enabled


def native_stage_type(draw=0.99, provider=None, enabled=True, wait=None):
    """Compile only the official decision/conversion statements, without retyping them."""
    tree = ast.parse(NATIVE.read_text(encoding="utf-8"))
    stage = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ResultDecorateStage")
    method = next(n for n in stage.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "process")
    named = {}
    conversion = None
    for node in ast.walk(method):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ("tts_provider", "should_tts"):
                    named[target.id] = node
        if (isinstance(node, ast.If) and isinstance(node.test, ast.BoolOp)
                and [getattr(part, "id", None) for part in node.test.values] == ["should_tts", "tts_provider"]):
            conversion = node
    if conversion is None or set(named) != {"tts_provider", "should_tts"}:
        raise AssertionError("Native TTS code shape changed; review the wrapper integration")
    conversion = deepcopy(conversion)
    conversion.orelse = []  # The following native text-to-image branch is unrelated.
    body = ast.parse("result = event.get_result()\nevent.stage_seen = self").body
    body += [named["tts_provider"], named["should_tts"], conversion]
    body += ast.parse("event.decision = bool(should_tts)").body
    function = ast.AsyncFunctionDef(name="process", args=ast.arguments(
        posonlyargs=[], args=[ast.arg(arg="self"), ast.arg(arg="event")],
        kwonlyargs=[], kw_defaults=[], defaults=[]), body=body, decorator_list=[])
    logger = types.SimpleNamespace(info=lambda *a: None, debug=lambda *a: None,
                                   warning=lambda *a: None, error=lambda *a: None)
    namespace = {"Plain": Plain, "Record": Record, "SessionServiceManager": SessionManager,
                 "random": types.SimpleNamespace(random=lambda: draw), "logger": logger,
                 "traceback": types.SimpleNamespace(format_exc=lambda: "fixture synthesis failed")}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                 str(NATIVE), "exec"), namespace)

    async def get_provider(_umo):
        if wait:
            await wait()
        return provider

    cfg = {"provider_tts_settings": {"enable": enabled, "use_file_service": False, "dual_output": False},
           "callback_api_base": ""}
    fixture_type = type("NativeTTSStageFixture", (), {"process": namespace["process"]})
    instance = fixture_type()
    instance.tts_trigger_probability = 0.2
    instance.ctx = types.SimpleNamespace(astrbot_config=cfg, plugin_manager=types.SimpleNamespace(
        context=types.SimpleNamespace(get_using_tts_provider_async=get_provider)))
    return fixture_type, instance


async def consume(stage, event):
    result = stage.process(event)
    if inspect.isasyncgen(result):
        return [value async for value in result]
    return await result


def native_scheduler(stages):
    tree = ast.parse(SCHEDULER.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PipelineScheduler")
    method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_process_stages")
    method.returns = None
    for arg in method.args.args:
        arg.annotation = None
    fixture = ast.ClassDef(name="NativeSchedulerFixture", bases=[], keywords=[], body=[method], decorator_list=[])
    namespace = {"AsyncGenerator": AsyncGenerator, "cast": cast,
                 "logger": types.SimpleNamespace(debug=lambda *a: None)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fixture], type_ignores=[])),
                 str(SCHEDULER), "exec"), namespace)
    scheduler = namespace["NativeSchedulerFixture"]()
    scheduler.stages = stages
    return scheduler


class VoiceMarkerTests(unittest.IsolatedAsyncioTestCase):
    def controller(self, stage_type):
        controller = voice.VoiceMarkerController(stage_type)
        controller.install()
        self.addCleanup(controller.restore)
        return controller

    async def test_concurrent_voice_text_and_unmarked_replies_do_not_share_probability(self):
        ready = asyncio.Event()
        arrivals = 0

        async def barrier():
            nonlocal arrivals
            arrivals += 1
            if arrivals == 3:
                ready.set()
            await ready.wait()

        provider = Provider()
        stage_type, stage = native_stage_type(provider=provider, wait=barrier)
        self.controller(stage_type)
        events = [Event("voice"), Event("text"), Event()]
        await asyncio.gather(*(consume(stage, event) for event in events))
        self.assertEqual([event.decision for event in events], [True, False, False])
        self.assertIsInstance(events[0].result.chain[0], Record)
        self.assertIsInstance(events[1].result.chain[0], Plain)
        self.assertIsNot(events[0].stage_seen, stage)
        self.assertIsNot(events[1].stage_seen, stage)
        self.assertIs(events[2].stage_seen, stage)
        self.assertEqual(stage.tts_trigger_probability, 0.2)
        self.assertTrue(stage.ctx.astrbot_config["provider_tts_settings"]["enable"])

    async def test_text_marker_skips_even_exact_zero_random_draw(self):
        stage_type, stage = native_stage_type(draw=0, provider=Provider())
        self.controller(stage_type)
        event = Event("text")
        await consume(stage, event)
        self.assertFalse(event.decision)
        self.assertIsInstance(event.result.chain[0], Plain)

    async def test_voice_marker_accepts_exact_one_draw_but_respects_native_gates(self):
        for enabled, session, provider_present, expected in [
            (True, True, True, True), (False, True, True, False),
            (True, False, True, False), (True, True, False, False),
        ]:
            with self.subTest(enabled=enabled, session=session, provider=provider_present):
                stage_type, stage = native_stage_type(draw=1, provider=Provider() if provider_present else None,
                                                      enabled=enabled)
                controller = self.controller(stage_type)
                event = Event("voice")
                event.session_enabled = session
                await consume(stage, event)
                self.assertEqual(event.decision, expected)
                controller.restore()

    async def test_native_synthesis_failure_keeps_the_existing_text(self):
        provider = Provider(error=RuntimeError("failed locally"))
        stage_type, stage = native_stage_type(provider=provider)
        self.controller(stage_type)
        event = Event("voice")
        original = event.result.chain[0]
        await consume(stage, event)
        self.assertIs(event.result.chain[0], original)
        self.assertEqual(provider.calls, [original.text])

    async def test_empty_native_audio_result_keeps_text(self):
        stage_type, stage = native_stage_type(provider=Provider(path=""))
        self.controller(stage_type)
        event = Event("voice")
        await consume(stage, event)
        self.assertIsInstance(event.result.chain[0], Plain)

    async def test_non_llm_result_is_not_forced_to_audio(self):
        provider = Provider()
        stage_type, stage = native_stage_type(provider=provider)
        self.controller(stage_type)
        event = Event("voice")
        event.result.is_llm_result = lambda: False
        await consume(stage, event)
        self.assertFalse(event.decision)
        self.assertEqual(provider.calls, [])

    async def test_unknown_marker_uses_the_original_probability_and_instance(self):
        stage_type, stage = native_stage_type(draw=0.1, provider=Provider())
        self.controller(stage_type)
        event = Event("other")
        await consume(stage, event)
        self.assertTrue(event.decision)
        self.assertIs(event.stage_seen, stage)

    async def test_install_is_idempotent_and_restore_reinstates_exact_native_method(self):
        stage_type, stage = native_stage_type(provider=Provider())
        original = stage_type.process
        controller = self.controller(stage_type)
        current = stage_type.process
        self.assertFalse(controller.install())
        self.assertIs(stage_type.process, current)
        self.assertTrue(controller.status()["active"])
        self.assertTrue(controller.restore())
        self.assertIs(stage_type.process, original)
        self.assertFalse(controller.status()["active"])
        self.assertFalse(controller.restore())

    async def test_restore_leaves_a_later_hook_in_place_and_deactivates_its_inner_shim(self):
        stage_type, stage = native_stage_type(provider=Provider())
        controller = self.controller(stage_type)
        inner = stage_type.process

        async def later_hook(instance, event):
            event.later_hook_seen = True
            return await inner(instance, event)

        stage_type.process = later_hook
        self.assertFalse(controller.restore())
        self.assertIs(stage_type.process, later_hook)
        event = Event("voice")
        await consume(stage, event)
        self.assertFalse(event.decision)
        self.assertTrue(event.later_hook_seen)
        self.assertIs(event.stage_seen, stage)

    async def test_cancelled_synthesis_is_propagated(self):
        stage_type, stage = native_stage_type(provider=Provider(error=asyncio.CancelledError()))
        self.controller(stage_type)
        with self.assertRaises(asyncio.CancelledError):
            await consume(stage, Event("voice"))

    async def test_coroutine_stage_patch_also_preserves_event_isolation(self):
        class CoroutineStage:
            tts_trigger_probability = 0.2

            async def process(self, event):
                await asyncio.sleep(0)
                event.probability = self.tts_trigger_probability

        self.controller(CoroutineStage)
        stage = CoroutineStage()
        events = [Event("voice"), Event("text"), Event()]
        await asyncio.gather(*(consume(stage, event) for event in events))
        self.assertEqual([event.probability for event in events], [1.0, -1.0, 0.2])
        self.assertEqual(stage.tts_trigger_probability, 0.2)

    async def test_native_scheduler_reaches_respond_after_wrapped_coroutine_for_every_mode(self):
        stage_type, stage = native_stage_type(provider=Provider())
        original = stage_type.process
        self.assertTrue(inspect.iscoroutinefunction(original))
        self.controller(stage_type)
        self.assertTrue(inspect.iscoroutinefunction(stage_type.process))
        self.assertFalse(inspect.isasyncgenfunction(stage_type.process))

        class Respond:
            async def process(self, event):
                event.responded = True

        scheduler = native_scheduler([stage, Respond()])
        for mode in ("voice", "text", None):
            with self.subTest(mode=mode):
                event = Event(mode)
                await scheduler._process_stages(event)
                self.assertTrue(event.responded)

    async def test_native_scheduler_keeps_async_generator_yield_count_and_onion_order(self):
        trace = []

        class GeneratorStage:
            tts_trigger_probability = 0.2

            async def process(self, event):
                trace.append(("before", self.tts_trigger_probability))
                yield "first"
                trace.append(("between", self.tts_trigger_probability))
                yield "second"
                trace.append(("after", self.tts_trigger_probability))

        class Respond:
            async def process(self, event):
                trace.append(("respond", event.mode))

        original = GeneratorStage.process
        self.controller(GeneratorStage)
        self.assertTrue(inspect.isasyncgenfunction(GeneratorStage.process))
        stage = GeneratorStage()
        self.assertEqual(await consume(stage, Event("voice")), ["first", "second"])
        trace.clear()
        await native_scheduler([stage, Respond()])._process_stages(Event("text"))
        wrapped_trace = list(trace)
        trace.clear()
        native_type = type("UnwrappedGeneratorStage", (), {
            "tts_trigger_probability": -1.0, "process": original})
        await native_scheduler([native_type(), Respond()])._process_stages(Event("text"))
        self.assertEqual(wrapped_trace, trace)
        self.assertEqual([item for item in wrapped_trace if item[0] != "respond"],
                         [("before", -1.0), ("between", -1.0), ("after", -1.0)])
        self.assertEqual(stage.tts_trigger_probability, 0.2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
