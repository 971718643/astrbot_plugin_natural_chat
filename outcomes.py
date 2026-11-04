"""Bounded delivery outcomes containing identifiers and reason codes, never prose."""

from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import tempfile
from threading import RLock
import time
import uuid


_FIELDS = {"id", "umo", "job_id", "at", "outcome", "reason", "delivered_parts"}
_CODE = re.compile(r"[a-zA-Z0-9_:-]+\Z")
_IDENTIFIER = re.compile(r"[a-zA-Z0-9-]{1,64}\Z")


class OutcomeLog:
    def __init__(self, path, now=time.time, max_records=100):
        if type(max_records) is not int or not 1 <= max_records <= 100:
            raise ValueError("invalid_outcome_limit")
        self.path = Path(path)
        self.now = now
        self.max_records = max_records
        self._lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state = {"version": 1, "records": []}
        if self.path.exists():
            try:
                state = json.loads(self.path.read_text(encoding="utf-8"))
                self._validate_state(state)
            except (ValueError, UnicodeError, TypeError) as error:
                # Preserve the original evidence; the caller decides recovery.
                raise ValueError("invalid_outcome_file") from error
            self._state = state

    @staticmethod
    def _valid_umo(umo):
        if (not isinstance(umo, str) or len(umo) > 512
                or any(char.isspace() or ord(char) < 32 for char in umo)):
            return False
        parts = umo.split(":", 2)
        return (len(parts) == 3 and bool(parts[0]) and bool(parts[2])
                and parts[1] in ("GroupMessage", "FriendMessage"))

    @staticmethod
    def _valid_code(value, maximum, empty=False):
        if not isinstance(value, str) or len(value) > maximum:
            return False
        if not value:
            return empty
        # Identifiers such as API tokens are not diagnostic codes.
        if value.lower().startswith(("sk-", "bearer:", "token:", "api_key:")):
            return False
        return _CODE.fullmatch(value) is not None

    @classmethod
    def _validate_row(cls, row):
        if (not isinstance(row, dict) or set(row) != _FIELDS
                or not isinstance(row["id"], str) or not _IDENTIFIER.fullmatch(row["id"])
                or not cls._valid_umo(row["umo"])
                or not isinstance(row["job_id"], str) or not _IDENTIFIER.fullmatch(row["job_id"])
                or not isinstance(row["at"], (int, float)) or isinstance(row["at"], bool)
                or not math.isfinite(row["at"])
                or not cls._valid_code(row["outcome"], 48)
                or not cls._valid_code(row["reason"], 96, empty=True)
                or type(row["delivered_parts"]) is not int or row["delivered_parts"] < 0):
            raise ValueError("invalid_outcome_record")

    @classmethod
    def _validate_state(cls, state):
        if (not isinstance(state, dict) or set(state) != {"version", "records"}
                or state["version"] != 1 or type(state["version"]) is not int
                or not isinstance(state["records"], list) or len(state["records"]) > 100):
            raise ValueError("invalid_outcome_state")
        ids = set()
        for row in state["records"]:
            cls._validate_row(row)
            if row["id"] in ids:
                raise ValueError("duplicate_outcome_id")
            ids.add(row["id"])

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

    def record(self, umo, job_id, outcome, reason="", delivered_parts=0):
        with self._lock:
            row = {"id": uuid.uuid4().hex, "umo": umo, "job_id": job_id,
                   "at": self.now(), "outcome": outcome, "reason": reason,
                   "delivered_parts": delivered_parts}
            self._validate_row(row)
            state = {"version": 1,
                     "records": (deepcopy(self._state["records"]) + [row])[-self.max_records:]}
            self._commit(state)
            return deepcopy(row)

    def append_record(self, row):
        """Persist a buffered result without replacing its identity or time.

        A repeated ID is already committed and returns the stored record. The
        caller's object is never retained, including on a failed commit.
        """
        with self._lock:
            copied = deepcopy(row)
            self._validate_row(copied)
            existing = next((item for item in self._state["records"]
                             if item["id"] == copied["id"]), None)
            if existing is not None:
                return deepcopy(existing)
            state = {"version": 1,
                     "records": (deepcopy(self._state["records"]) + [copied])[-self.max_records:]}
            self._commit(state)
            return deepcopy(copied)

    def records(self, umo=None, limit=20):
        if umo is not None and not self._valid_umo(umo):
            raise ValueError("invalid_outcome_session")
        if type(limit) is not int or not 0 <= limit <= 100:
            raise ValueError("invalid_outcome_read_limit")
        with self._lock:
            rows = [row for row in reversed(self._state["records"])
                    if umo is None or row["umo"] == umo]
            return deepcopy(rows[:limit])

    def last(self, umo=None):
        rows = self.records(umo, limit=1)
        return rows[0] if rows else None
