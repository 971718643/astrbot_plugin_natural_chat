"""Small, passive conversation-progress notes; never a job or message source."""

from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import tempfile
from threading import RLock
import time
import unicodedata
import uuid


MAX_THREADS = 2
TTL_SECONDS = 5 * 24 * 60 * 60
MAX_APPLIED_TURNS = 32
MAX_EXPIRY_EVENTS = 100
EXPIRY_EVENT_TTL_SECONDS = 7 * 24 * 60 * 60
_ID = re.compile(r"[0-9a-f]{32}\Z")
_TURN = re.compile(r"[a-zA-Z0-9_:-]{1,128}\Z")
_OPEN_FIELDS = {"kind", "topic", "point", "user_quote", "assistant_quote"}
_ROW_FIELDS = _OPEN_FIELDS | {
    "id", "participant_id", "scope_key", "created_at", "last_touch", "expires_at",
    "awaiting_followup", "awaiting_turn_id",
}


class OpenThreadStore:
    def __init__(self, path, now=time.time):
        self.path, self.now = Path(path), now
        self._lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state = {"version": 1, "threads": {}, "applied_turns": {}}
        if self.path.exists():
            # A malformed file is evidence, not something to silently repair.
            state = json.loads(self.path.read_text(encoding="utf-8"))
            self._validate_state(state)
            self._state = state
        with self._lock:
            self._prune()

    @staticmethod
    def _finite(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    @staticmethod
    def _text(value, maximum, empty=False):
        return (isinstance(value, str) and len(value) <= maximum
                and (bool(value.strip()) or empty)
                and all(ord(char) >= 32 for char in value))

    @staticmethod
    def _umo(umo):
        if (not isinstance(umo, str) or len(umo) > 512
                or any(char.isspace() or ord(char) < 32 for char in umo)):
            return False
        parts = umo.split(":", 2)
        return (len(parts) == 3 and bool(parts[0]) and bool(parts[2])
                and parts[1] in ("GroupMessage", "FriendMessage"))

    @staticmethod
    def _topic_key(topic):
        return " ".join(unicodedata.normalize("NFKC", topic).split()).casefold()

    def _clock(self):
        value = self.now()
        if not self._finite(value):
            raise ValueError("invalid_thread_clock")
        return float(value)

    @classmethod
    def _validate_open(cls, row):
        if (not isinstance(row, dict) or set(row) != _OPEN_FIELDS
                or row["kind"] not in ("question", "promise", "deferred")
                or not cls._text(row["topic"], 40)
                or not cls._text(row["point"], 100)
                or not cls._text(row["user_quote"], 60, empty=True)
                or not cls._text(row["assistant_quote"], 60, empty=True)
                or not (row["user_quote"].strip() or row["assistant_quote"].strip())):
            raise ValueError("invalid_open_thread")

    @classmethod
    def _validate_row(cls, row):
        if not isinstance(row, dict) or set(row) != _ROW_FIELDS:
            raise ValueError("invalid_thread_row")
        cls._validate_open({key: row[key] for key in _OPEN_FIELDS})
        if (not isinstance(row["id"], str) or not _ID.fullmatch(row["id"])
                or not cls._text(row["participant_id"], 128)
                or not cls._text(row["scope_key"], 512)
                or not all(cls._finite(row[key]) for key in ("created_at", "last_touch", "expires_at"))
                # Accept historical fixed deadlines without extending on load.
                or not row["created_at"] + TTL_SECONDS <= row["expires_at"] <= row["last_touch"] + TTL_SECONDS
                or not row["created_at"] <= row["last_touch"] < row["expires_at"]
                or type(row["awaiting_followup"]) is not bool
                or not isinstance(row["awaiting_turn_id"], str)
                or (row["awaiting_followup"] and not _TURN.fullmatch(row["awaiting_turn_id"]))
                or (not row["awaiting_followup"] and row["awaiting_turn_id"] != "")):
            raise ValueError("invalid_thread_row")

    @classmethod
    def _validate_state(cls, state):
        if (not isinstance(state, dict) or not {"version", "threads"} <= set(state)
                or not set(state) <= {"version", "threads", "applied_turns", "expiry_events", "expiry_events_dropped"}
                or type(state["version"]) is not int or state["version"] != 1
                or not isinstance(state["threads"], dict)
                or not isinstance(state.get("applied_turns", {}), dict)):
            raise ValueError("invalid_thread_state")
        ids = set()
        for umo, rows in state["threads"].items():
            if not cls._umo(umo) or not isinstance(rows, list) or not 1 <= len(rows) <= MAX_THREADS:
                raise ValueError("invalid_thread_session")
            keys = set()
            for row in rows:
                cls._validate_row(row)
                key = (row["participant_id"], row["scope_key"], cls._topic_key(row["topic"]))
                if row["id"] in ids or key in keys:
                    raise ValueError("duplicate_open_thread")
                ids.add(row["id"])
                keys.add(key)
        for umo, turns in state.get("applied_turns", {}).items():
            if not cls._umo(umo) or not isinstance(turns, list) or not 1 <= len(turns) <= MAX_APPLIED_TURNS:
                raise ValueError("invalid_thread_turns")
            turn_ids = set()
            for turn in turns:
                if (not isinstance(turn, dict) or set(turn) != {"id", "at"}
                        or not isinstance(turn["id"], str) or not _TURN.fullmatch(turn["id"])
                        or not cls._finite(turn["at"]) or turn["id"] in turn_ids):
                    raise ValueError("invalid_thread_turns")
                turn_ids.add(turn["id"])
        events = state.get("expiry_events", [])
        dropped = state.get("expiry_events_dropped", 0)
        if (not isinstance(events, list) or len(events) > MAX_EXPIRY_EVENTS
                or type(dropped) is not int or dropped < 0):
            raise ValueError("invalid_expiry_events")
        event_ids = set()
        for event in events:
            if (not isinstance(event, dict) or set(event) != {
                    "id", "umo", "participant_id", "scope_key", "topic", "expires_at", "recorded_at", "reason"}
                    or not isinstance(event["id"], str) or not _ID.fullmatch(event["id"])
                    or event["id"] in event_ids or event["id"] in ids
                    or not cls._umo(event["umo"])
                    or not cls._text(event["participant_id"], 128)
                    or not cls._text(event["scope_key"], 512)
                    or not cls._text(event["topic"], 40)
                    or not cls._finite(event["expires_at"]) or not cls._finite(event["recorded_at"])
                    or event["recorded_at"] < event["expires_at"]
                    or event["reason"] != "ttl_expired"):
                raise ValueError("invalid_expiry_event")
            event_ids.add(event["id"])

    def _commit(self, state):
        self._validate_state(state)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                             dir=self.path.parent, prefix=self.path.name + ".",
                                             suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(state, handle, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self._state = state
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    @staticmethod
    def _pruned(state, now):
        events = state.get("expiry_events", [])
        retained = [event for event in events if event["recorded_at"] + EXPIRY_EVENT_TTL_SECONDS > now]
        dropped = len(events) - len(retained)
        for umo, rows in list(state["threads"].items()):
            for row in rows:
                if row["expires_at"] <= now:
                    retained.append({key: row[key] for key in (
                        "id", "participant_id", "scope_key", "topic", "expires_at")}
                                    | {"umo": umo, "recorded_at": now, "reason": "ttl_expired"})
            remaining = [row for row in rows if row["expires_at"] > now]
            if remaining:
                state["threads"][umo] = remaining
            else:
                del state["threads"][umo]
        for umo, turns in list(state.get("applied_turns", {}).items()):
            remaining = [turn for turn in turns if turn["at"] + TTL_SECONDS > now]
            if remaining:
                state["applied_turns"][umo] = remaining
            else:
                del state["applied_turns"][umo]
        dropped += max(0, len(retained) - MAX_EXPIRY_EVENTS)
        if retained or "expiry_events" in state:
            state["expiry_events"] = retained[-MAX_EXPIRY_EVENTS:]
        if dropped:
            state["expiry_events_dropped"] = state.get("expiry_events_dropped", 0) + dropped

    def _prune(self):
        state = deepcopy(self._state)
        self._pruned(state, self._clock())
        if state != self._state:
            self._commit(state)

    @classmethod
    def _context(cls, umo, participant_id, scope_key):
        if (not cls._umo(umo) or not cls._text(participant_id, 128)
                or not cls._text(scope_key, 512)):
            raise ValueError("invalid_thread_context")

    def list_threads(self, umo, participant_id, scope_key):
        self._context(umo, participant_id, scope_key)
        with self._lock:
            self._prune()
            rows = [row for row in self._state["threads"].get(umo, [])
                    if row["participant_id"] == participant_id and row["scope_key"] == scope_key]
            return deepcopy(sorted(rows, key=lambda row: row["last_touch"], reverse=True))

    def list_expiry_events(self, umo, participant_id, scope_key, limit=2):
        self._context(umo, participant_id, scope_key)
        if type(limit) is not int or not 0 <= limit <= MAX_EXPIRY_EVENTS:
            raise ValueError("invalid_expiry_event_limit")
        with self._lock:
            self._prune()
            rows = [event for event in reversed(self._state.get("expiry_events", []))
                    if event["umo"] == umo and event["participant_id"] == participant_id
                    and event["scope_key"] == scope_key]
            return deepcopy(rows[:limit])

    @staticmethod
    def _operation_rows(raw, progress=False):
        if not isinstance(raw, (list, tuple)) or len(raw) > MAX_THREADS:
            raise ValueError("invalid_thread_operations")
        result = {}
        for item in raw:
            if isinstance(item, str):
                item = {"id": item}
            if (not isinstance(item, dict) or not set(item) <= {
                    "id", "quote", "point", "user_quote", "assistant_quote"}
                    or not isinstance(item.get("id"), str) or not _ID.fullmatch(item["id"])):
                raise ValueError("invalid_thread_operations")
            for key, value in item.items():
                if key != "id" and (not OpenThreadStore._text(value, 100, empty=key != "point")
                                    or (key == "point" and not progress)):
                    raise ValueError("invalid_thread_operations")
            if item["id"] in result:
                raise ValueError("duplicate_thread_operation")
            result[item["id"]] = deepcopy(item)
        return result

    def apply(self, umo, participant_id, scope_key, turn_id, operations, injected_ids,
              awaiting_ids=None, awaiting_turns=None):
        """Apply only already source-validated, delivered ordinary-turn changes.

        Runtime owns semantic/quote validation and successful-send admission.
        The store enforces identity, retention, bounded slots and atomicity.
        """
        self._context(umo, participant_id, scope_key)
        if not isinstance(turn_id, str) or not _TURN.fullmatch(turn_id):
            raise ValueError("invalid_thread_turn")
        if not isinstance(operations, dict) or not set(operations) <= {"open", "mention", "progress", "resolve"}:
            raise ValueError("invalid_thread_operations")
        opens = operations.get("open", [])
        if not isinstance(opens, (list, tuple)) or len(opens) > MAX_THREADS:
            raise ValueError("invalid_thread_operations")
        opens = deepcopy(opens)
        for item in opens:
            self._validate_open(item)
        mention = self._operation_rows(operations.get("mention", []))
        progress = self._operation_rows(operations.get("progress", []), progress=True)
        resolves = self._operation_rows(operations.get("resolve", []))
        if not isinstance(injected_ids, (list, tuple, set)) or len(injected_ids) > MAX_THREADS:
            raise ValueError("invalid_thread_snapshot")
        if any(not isinstance(key, str) or not _ID.fullmatch(key) for key in injected_ids):
            raise ValueError("invalid_thread_snapshot")
        injected = set(injected_ids)
        if awaiting_ids is not None and (not isinstance(awaiting_ids, (list, tuple, set))
                or len(awaiting_ids) > MAX_THREADS
                or any(not isinstance(key, str) or not _ID.fullmatch(key) for key in awaiting_ids)
                or not set(awaiting_ids) <= injected):
            raise ValueError("invalid_thread_snapshot")
        if awaiting_turns is not None and (not isinstance(awaiting_turns, dict)
                or not set(awaiting_turns) <= injected
                or any(not isinstance(value, str) or not _TURN.fullmatch(value)
                       for value in awaiting_turns.values())):
            raise ValueError("invalid_thread_snapshot")
        with self._lock:
            now = self._clock()
            state = deepcopy(self._state)
            self._pruned(state, now)
            turns = state.get("applied_turns", {}).get(umo, [])
            if any(item["id"] == turn_id for item in turns):
                if state != self._state:
                    self._commit(state)
                return False
            previous = deepcopy(state["threads"].get(umo, []))
            rows = deepcopy(previous)
            # A later delivered turn may have changed the waiting generation
            # after this request took its snapshot. Its earlier response must
            # not resolve/progress/mention, or upsert over, that newer state.
            stale_topics = {
                self._topic_key(row["topic"]) for row in rows
                if row["participant_id"] == participant_id and row["scope_key"] == scope_key
                and awaiting_turns is not None
                and row["awaiting_turn_id"] != awaiting_turns.get(row["id"], "")
            }
            eligible = {row["id"] for row in rows if row["participant_id"] == participant_id
                        and row["scope_key"] == scope_key and row["id"] in injected
                        and self._topic_key(row["topic"]) not in stale_topics}
            closed_topics = set()
            kept = []
            for row in rows:
                key = row["id"]
                old_wait = (key in eligible and row["awaiting_followup"]
                            and row["awaiting_turn_id"] != turn_id
                            and (awaiting_ids is None or key in awaiting_ids)
                            and (awaiting_turns is None or awaiting_turns.get(key) == row["awaiting_turn_id"]))
                if key in eligible and (key in resolves or (old_wait and key not in progress)):
                    closed_topics.add(self._topic_key(row["topic"]))
                    continue
                if key in eligible and key in progress:
                    row["awaiting_followup"], row["awaiting_turn_id"] = False, ""
                    if "point" in progress[key]:
                        row["point"] = progress[key]["point"]
                    row["last_touch"] = max(now, row["last_touch"])
                    row["expires_at"] = row["last_touch"] + TTL_SECONDS
                if key in eligible and key in mention:
                    row["awaiting_followup"], row["awaiting_turn_id"] = True, turn_id
                    row["last_touch"] = max(now, row["last_touch"])
                    row["expires_at"] = row["last_touch"] + TTL_SECONDS
                kept.append(row)
            rows = kept
            for item in opens:
                topic = self._topic_key(item["topic"])
                if topic in closed_topics or topic in stale_topics:
                    continue  # A contradictory block must not resurrect a closed topic.
                existing = next((row for row in rows if row["participant_id"] == participant_id
                                 and row["scope_key"] == scope_key
                                 and self._topic_key(row["topic"]) == topic), None)
                if existing is not None:
                    if any(existing[key] != item[key] for key in _OPEN_FIELDS):
                        existing.update(deepcopy(item))
                    continue
                if len(rows) >= MAX_THREADS:
                    victim = min(rows, key=lambda row: (row["last_touch"], row["created_at"], row["id"]))
                    rows.remove(victim)
                rows.append(dict(deepcopy(item), id=uuid.uuid4().hex, participant_id=participant_id,
                                 scope_key=scope_key, created_at=now, last_touch=now,
                                 expires_at=now + TTL_SECONDS, awaiting_followup=False, awaiting_turn_id=""))
            if rows:
                state["threads"][umo] = rows
            else:
                state["threads"].pop(umo, None)
            if rows != previous:
                state.setdefault("applied_turns", {})[umo] = (turns + [{"id": turn_id, "at": now}])[-MAX_APPLIED_TURNS:]
            changed = state != self._state
            if changed:
                self._commit(state)
            return changed

    def status(self):
        with self._lock:
            self._prune()
            return {"thread_count": sum(len(rows) for rows in self._state["threads"].values()),
                    "session_count": len(self._state["threads"]),
                    "per_session_limit": MAX_THREADS, "ttl_seconds": TTL_SECONDS,
                    "ttl_mode": "since_last_touch",
                    "expiry_event_count": len(self._state.get("expiry_events", [])),
                    "expiry_event_limit": MAX_EXPIRY_EVENTS,
                    "expiry_event_ttl_seconds": EXPIRY_EVENT_TTL_SECONDS,
                    "expiry_events_dropped": self._state.get("expiry_events_dropped", 0)}
