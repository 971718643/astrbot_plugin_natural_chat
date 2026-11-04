"""Parse only unquoted, trailing assistant reply controls.

The caller must pass a final assistant reply, never an incoming user message.
No scheduling or speech side effects happen here. Work is bounded to a 65,536
character reply and its last 8,192 characters; longer input fails closed.
"""

from dataclasses import dataclass
import json
import re
from typing import Optional


MAX_INPUT = 65536
MAX_TAIL = 8192
MAX_DELAY = 7200
MAX_TOPIC_PAYLOAD = 2048
_TOKEN = re.compile(r"\[\[(?:(?P<voice>语音|文字)|开口:(?P<delay>[^\]\r\n]*))\]\]")
_TOPIC_START = re.compile(r"\[\[话题:")
_TOPIC_ID = re.compile(r"[0-9a-f]{32}\Z")
_FENCE = re.compile(r" {0,3}(?P<ticks>`{3,}|~{3,})(?P<rest>.*)")
_OPEN_QUOTES = {"\"": "\"", "'": "'", "“": "”", "‘": "’",
                "「": "」", "『": "』", "«": "»", "‹": "›", "《": "》"}


@dataclass(frozen=True)
class ParsedReply:
    text: str
    voice_mode: Optional[str] = None
    delay_seconds: Optional[int] = None
    had_markers: bool = False
    topic_control: Optional[dict] = None


class _TopicToken:
    def __init__(self, start, end, payload):
        self._start, self._end, self.payload = start, end, payload

    def start(self):
        return self._start

    def end(self):
        return self._end


def _topic_tokens(text, start, end):
    """Locate complete topic blocks without interpreting quoted inner controls.

    JSON's decoder supplies valid boundaries, including arrays and strings with
    `]]`. A tolerant single-line fallback also strips malformed bare controls.
    """
    result = []
    covered_until = start
    decoder = json.JSONDecoder()
    for match in _TOPIC_START.finditer(text, start, end):
        if match.start() < covered_until:
            continue
        payload_start = match.end()
        raw = text[payload_start:end]
        leading = len(raw) - len(raw.lstrip())
        boundary = None
        try:
            _, consumed = decoder.raw_decode(raw, leading)
            closing = consumed
            while closing < len(raw) and raw[closing].isspace():
                closing += 1
            if raw[closing:closing + 2] == "]]":
                boundary = closing
        except (ValueError, RecursionError):
            pass
        if boundary is None:
            # Prefer the complete malformed suffix before any trailing legacy
            # controls. A `]]` inside its broken JSON string is not the end.
            tail = end
            for token in reversed(list(_TOKEN.finditer(text, payload_start, end))):
                if text[token.end():tail].strip():
                    break
                tail = token.start()
            malformed_suffix = text[payload_start:tail].rstrip()
            if malformed_suffix.endswith("]]"):
                boundary = len(malformed_suffix) - 2
        if boundary is None:
            quoted, escaped = False, False
            for index, char in enumerate(raw):
                if escaped:
                    escaped = False
                elif char == "\\" and quoted:
                    escaped = True
                elif char == '"':
                    quoted = not quoted
                elif not quoted and raw[index:index + 2] == "]]":
                    boundary = index
                    break
            if boundary is None:
                # A bad unclosed string must not protect the following controls.
                # Take its first closing pair rather than swallowing their tags.
                closing = raw.find("]]")
                boundary = closing if closing >= 0 else None
        if boundary is not None:
            covered_until = payload_start + boundary + 2
            result.append(_TopicToken(match.start(), covered_until, raw[:boundary]))
    return result


def _topic_control(payload):
    if len(payload) > MAX_TOPIC_PAYLOAD:
        return None

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_topic_key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("nonfinite_topic")

    def text(value, maximum, empty=False):
        return (isinstance(value, str) and len(value) <= maximum
                and (empty or bool(value.strip()))
                and not any(ord(char) < 32 for char in value))

    def identifier(value):
        return isinstance(value, str) and _TOPIC_ID.fullmatch(value) is not None

    try:
        obj = json.loads(payload, object_pairs_hook=unique_object, parse_constant=reject_constant)
        keys = {"open", "mention", "progress", "resolve"}
        if not isinstance(obj, dict) or not set(obj) <= keys:
            return None
        control = {key: obj.get(key, []) for key in ("open", "mention", "progress", "resolve")}
        if any(not isinstance(rows, list) or len(rows) > 2 for rows in control.values()):
            return None
        for row in control["open"]:
            if (not isinstance(row, dict)
                    or set(row) != {"kind", "topic", "point", "user_quote", "assistant_quote"}
                    or row["kind"] not in ("question", "promise", "deferred")
                    or not text(row["topic"], 40) or not text(row["point"], 100)
                    or not text(row["user_quote"], 60, empty=True)
                    or not text(row["assistant_quote"], 60, empty=True)):
                return None
        for key in ("mention", "progress", "resolve"):
            for row in control[key]:
                if (not isinstance(row, dict)
                        or not {"id", "quote"} <= set(row)
                        or not set(row) <= ({"id", "quote", "point"} if key == "progress" else {"id", "quote"})
                        or not identifier(row["id"]) or not text(row["quote"], 60)
                        or ("point" in row and not text(row["point"], 100))):
                    return None
        return control if any(control.values()) else None
    except (ValueError, TypeError, RecursionError):
        return None


def _escaped(text, start):
    count = 0
    index = start - 1
    while index >= 0 and text[index] == "\\":
        count += 1
        index -= 1
    return bool(count % 2)


def _protected_starts(text, starts, opaque_spans=None):
    """Locate candidate starts inside Markdown code, quotes or quoted lines."""
    protected = {start for start in starts if _escaped(text, start)}
    fence_char, fence_size, inline_size = None, 0, 0
    quote_stack = []
    offset = 0
    ordered = sorted(starts)
    candidate_index = 0
    opaque_spans = opaque_spans or {}
    opaque_until = 0
    for line in text.splitlines(keepends=True):
        positions = set()
        while candidate_index < len(ordered) and ordered[candidate_index] < offset + len(line):
            positions.add(ordered[candidate_index])
            candidate_index += 1
        if opaque_until >= offset + len(line):
            offset += len(line)
            continue
        opaque_prefix = opaque_until > offset
        fence = _FENCE.match(line.rstrip("\r\n"))
        if fence_char is not None:
            protected.update(positions)
            if (fence and fence.group("ticks")[0] == fence_char
                    and len(fence.group("ticks")) >= fence_size
                    and not fence.group("rest").strip()):
                fence_char, fence_size = None, 0
            offset += len(line)
            continue
        # A quoted/indented code line must not open a quote or fence in later prose.
        if not opaque_prefix and (line.lstrip().startswith(">") or line.startswith(("    ", "\t"))):
            protected.update(positions)
            offset += len(line)
            continue
        if not opaque_prefix and not inline_size and not quote_stack and fence:
            fence_char, fence_size = fence.group("ticks")[0], len(fence.group("ticks"))
            protected.update(positions)
            offset += len(line)
            continue
        index = max(0, opaque_until - offset)
        while index < len(line):
            absolute = offset + index
            if absolute in positions and (inline_size or quote_stack):
                protected.add(absolute)
            if absolute in opaque_spans and not inline_size and not quote_stack:
                # Metadata is atomic for protection scanning. Malformed JSON
                # quotes cannot change whether a later voice marker is quoted.
                opaque_until = opaque_spans[absolute]
                index = min(len(line), opaque_until - offset)
                continue
            char = line[index]
            if char == "\\":
                index += 2
                continue
            if char == "`":
                end = index + 1
                while end < len(line) and line[end] == "`":
                    end += 1
                size = end - index
                if inline_size == size:
                    inline_size = 0
                elif not inline_size:
                    inline_size = size
                index = end
                continue
            if not inline_size:
                if quote_stack and char == quote_stack[-1]:
                    quote_stack.pop()
                elif char in _OPEN_QUOTES:
                    # English contractions aren't quotation delimiters.
                    contraction = (char == "'" and index > 0 and index + 1 < len(line)
                                   and line[index - 1].isalnum() and line[index + 1].isalnum())
                    if not contraction:
                        quote_stack.append(_OPEN_QUOTES[char])
            index += 1
        offset += len(line)
    return protected


def parse_reply(text: str) -> ParsedReply:
    """Strip supported trailing controls, returning side-effect-free intentions.

    Quoted/code examples are left intact. Conflicting voice modes choose text;
    conflicting or invalid delay controls never schedule. A control-only reply
    is stripped but produces neither speech nor scheduling intentions.
    """
    if not isinstance(text, str):
        raise TypeError("reply must be a string")
    if not text or len(text) > MAX_INPUT:
        return ParsedReply(text)
    end = len(text.rstrip())
    start = max(0, end - MAX_TAIL)
    topics = _topic_tokens(text, start, end)
    tokens = [token for token in _TOKEN.finditer(text, start, end)
              if not any(topic.start() <= token.start() < topic.end() for topic in topics)]
    tokens = sorted(tokens + topics, key=lambda token: token.start())
    suffix = []
    cursor = end
    for token in reversed(tokens):
        gap = text[token.end():cursor]
        if gap and not gap.isspace():
            break
        suffix.append(token)
        cursor = token.start()
    if not suffix:
        return ParsedReply(text)
    protected = _protected_starts(text, {token.start() for token in suffix},
                                  {token.start(): token.end() for token in suffix
                                   if isinstance(token, _TopicToken)})
    controls = []
    cutoff = end
    for token in suffix:
        if token.start() in protected:
            break
        controls.append(token)
        cutoff = token.start()
    if not controls:
        return ParsedReply(text)
    cleaned = text[:cutoff].rstrip()
    if not cleaned or cleaned in ("[[SPEAK]]", "[[SILENT]]"):
        return ParsedReply(cleaned, had_markers=True)
    voices = set()
    delays = set()
    invalid_delay = False
    topic_payloads = []
    for token in controls:
        if isinstance(token, _TopicToken):
            topic_payloads.append(token.payload)
            continue
        voice = token.group("voice")
        if voice:
            voices.add("voice" if voice == "语音" else "text")
            continue
        raw = token.group("delay").strip()
        # Bound before int() so giant integers cannot trigger conversion limits.
        if not 1 <= len(raw) <= 4 or not raw.isascii() or not raw.isdecimal():
            invalid_delay = True
            continue
        delay = int(raw)
        if not 1 <= delay <= MAX_DELAY:
            invalid_delay = True
        else:
            delays.add(delay)
    voice_mode = "text" if "text" in voices else "voice" if voices else None
    delay_seconds = next(iter(delays)) if len(delays) == 1 and not invalid_delay else None
    topic_control = _topic_control(topic_payloads[0]) if len(topic_payloads) == 1 else None
    return ParsedReply(cleaned, voice_mode, delay_seconds, True, topic_control)
