import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("reply_markers", ROOT / "reply_markers.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
parse_reply = MODULE.parse_reply


class ReplyMarkersTest(unittest.TestCase):
    def test_plain_replies_preserve_exact_whitespace(self):
        for text in ("", "你好", " 你好 \r\n", "[[SPEAK]]这句话直接发群。"):
            with self.subTest(text=text):
                self.assertEqual(parse_reply(text), MODULE.ParsedReply(text))

    def test_voice_and_text_are_explicit(self):
        self.assertEqual(parse_reply("你好 [[语音]]").voice_mode, "voice")
        result = parse_reply("你好\r\n[[文字]]\r\n")
        self.assertEqual((result.text, result.voice_mode, result.had_markers), ("你好", "text", True))

    def test_combination_in_either_order_and_speak_prefix_preserved(self):
        for suffix in ("[[语音]] [[开口:120]]", "[[开口:120]]\r\n[[语音]]"):
            result = parse_reply("[[SPEAK]]等会再聊～\n" + suffix)
            self.assertEqual(result.text, "[[SPEAK]]等会再聊～")
            self.assertEqual((result.voice_mode, result.delay_seconds), ("voice", 120))

    def test_valid_delay_boundaries_and_spaces(self):
        for raw, expected in (("1", 1), ("7200", 7200), (" 300 ", 300), ("0120", 120)):
            result = parse_reply("好呀 [[开口:" + raw + "]]")
            self.assertEqual((result.text, result.delay_seconds), ("好呀", expected))

    def test_invalid_and_giant_delay_stripped_without_execution(self):
        for raw in ("", "0", "-120", "+120", "120.0", "1e3", "7201", "随便", "１２３", "9" * 6000):
            with self.subTest(raw=raw[:40]):
                result = parse_reply("好呀 [[开口:" + raw + "]]")
                self.assertEqual(result.text, "好呀")
                self.assertIsNone(result.delay_seconds)
                self.assertTrue(result.had_markers)

    def test_conflicting_delays_do_not_schedule_same_delay_deduplicates(self):
        self.assertIsNone(parse_reply("好 [[开口:120]][[开口:300]]").delay_seconds)
        self.assertEqual(parse_reply("好 [[开口:120]][[开口:120]]").delay_seconds, 120)
        self.assertIsNone(parse_reply("好 [[开口:120]][[开口:bad]]").delay_seconds)

    def test_conflicting_voice_controls_choose_text(self):
        for suffix in ("[[语音]][[文字]]", "[[文字]][[语音]]", "[[文字]][[语音]][[语音]]"):
            self.assertEqual(parse_reply("好 " + suffix).voice_mode, "text")

    def test_controls_without_body_never_produce_intentions(self):
        for text in ("[[语音]]", " \n[[开口:120]] [[语音]]\n", "[[文字]][[开口:300]]"):
            result = parse_reply(text)
            self.assertEqual(result.text, "")
            self.assertIsNone(result.voice_mode)
            self.assertIsNone(result.delay_seconds)
            self.assertTrue(result.had_markers)

    def test_internal_decision_prefix_alone_is_not_a_spoken_body(self):
        for prefix in ("[[SPEAK]]", "[[SILENT]]"):
            result = parse_reply(prefix + " [[语音]][[开口:120]]")
            self.assertEqual(result.text, prefix)
            self.assertIsNone(result.voice_mode)
            self.assertIsNone(result.delay_seconds)
            self.assertTrue(result.had_markers)

    def test_markers_in_body_or_followed_by_prose_are_not_controls(self):
        for text in ("[[语音]]是什么意思？", "你好 [[开口:120]] 我只是举例", "语音[[语音]]的例子"):
            self.assertEqual(parse_reply(text), MODULE.ParsedReply(text))

    def test_closed_quotes_and_inline_code_examples_preserved(self):
        for text in ('例子是 "[[语音]]"', "例子是‘[[开口:120]]’", "例子是「[[文字]]」",
                     "示例 `[[语音]]`", "示例 ``[[语音]]``", "《[[语音]]》"):
            self.assertEqual(parse_reply(text), MODULE.ParsedReply(text))

    def test_unclosed_quotes_and_inline_code_never_execute(self):
        for text in ('例子是 "[[语音]]', "例子是“[[开口:120]]", "示例 `[[语音]]",
                     "示例 ``[[开口:120]]", "\\[[语音]]"):
            self.assertEqual(parse_reply(text), MODULE.ParsedReply(text))

    def test_fenced_code_examples_closed_and_unclosed_preserved(self):
        for text in ("```text\n[[语音]]", "~~~\n[[开口:120]]", "```\n[[语音]]\n```",
                     "    [[语音]]", "\t[[开口:120]]"):
            self.assertEqual(parse_reply(text), MODULE.ParsedReply(text))

    def test_quoted_lines_are_examples_not_controls(self):
        for text in ("> [[语音]]", "正文\n> [[开口:120]]", "正文\n  > [[文字]]"):
            self.assertEqual(parse_reply(text), MODULE.ParsedReply(text))

    def test_control_after_quoted_example_or_closed_code_still_works(self):
        for body in ("示例 `[[文字]]`", "```\n[[语音]]\n```", "> [[语音]]", '例子是 "[[语音]]"'):
            result = parse_reply(body + "\n[[开口:300]]")
            self.assertEqual(result.text, body)
            self.assertEqual(result.delay_seconds, 300)
            self.assertIsNone(result.voice_mode)

    def test_english_contractions_dont_open_quotes(self):
        result = parse_reply("I'll be here. [[语音]]")
        self.assertEqual((result.text, result.voice_mode), ("I'll be here.", "voice"))

    def test_oversize_input_fails_closed(self):
        text = "字" * MODULE.MAX_INPUT + "[[语音]]"
        self.assertEqual(parse_reply(text), MODULE.ParsedReply(text))

    def test_non_string_input_rejected(self):
        with self.assertRaises(TypeError):
            parse_reply(None)


if __name__ == "__main__":
    unittest.main()
