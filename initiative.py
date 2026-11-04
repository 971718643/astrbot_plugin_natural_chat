"""Durable, bounded self-initiated follow-up plans; no generation or delivery.

The caller owns normal reply limits, current conversation validation and sending.
Claiming a due plan moves it durably to ``claimed`` before returning it.
The caller records a terminal outcome before explicitly finishing the claim.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import tempfile
from threading import RLock
import time
import uuid


CHINA_TIME = timezone(timedelta(hours=8))
MIN_DELAY = 300
MAX_DELAY = 7200
MAX_OVERDUE = 120
MAX_CAPACITY_WAIT = 600
# Includes completed claims retained while their outcome journal is unavailable.
# Bound fault backlog without deleting evidence or changing per-chat quotas.
MAX_RETAINED_CLAIMS = 100
MAX_TERMINAL_RECEIPTS = 100
MAX_RECENT = 20
MAX_TEXT = 600

_RECEIPT_FIELDS = {"id", "umo", "job_id", "at", "outcome", "reason", "delivered_parts"}
_RECEIPT_IDENTIFIER = re.compile(r"[a-zA-Z0-9-]{1,64}\Z")
_RECEIPT_CODE = re.compile(r"[a-zA-Z0-9_:-]+\Z")


def execution_due_at(job):
    """Original due time plus only the recorded capacity-wait duration."""
    wait = job.get("capacity_wait_seconds", 0)
    if (not isinstance(wait, (int, float)) or isinstance(wait, bool)
            or not math.isfinite(wait) or not 0 <= wait <= MAX_CAPACITY_WAIT):
        raise ValueError("invalid_capacity_wait")
    return job["due_at"] + wait


class InitiativeStore:
    def __init__(self, config, path, now=time.time):
        self.config = config
        self.path = Path(path)
        self.now = now
        self._lock = RLock()
        self._discarded = []
        # A local terminal result may release the session barrier while a
        # separate outcome file is unavailable. Never remove its disk evidence.
        self._released_claim_ids = set()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            # Malformed files are deliberately never repaired/overwritten here.
            state = json.loads(self.path.read_text(encoding="utf-8"))
            self._validate_state(state)
            self._state = state
        else:
            self._state = {"version": 1, "jobs": {}, "recent": {}}
        with self._lock:
            state = deepcopy(self._state)
            now = self._clock()
            discarded = []
            for umo, job in list(state["jobs"].items()):
                reason = self._expiry_reason(job, now)
                if reason:
                    discarded.append({"job": deepcopy(job), "reason": reason})
                    del state["jobs"][umo]
            if state != self._state:
                self._commit(state)
                self._discarded.extend(discarded)

    def _clock(self):
        value = float(self.now())
        if not math.isfinite(value):
            raise ValueError("invalid_clock")
        return value

    @staticmethod
    def _finite_number(value):
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value))

    @classmethod
    def _validate_job(cls, umo, job):
        if (not isinstance(umo, str) or not umo
                    or not isinstance(job, dict) or job.get("umo") != umo
                    or not isinstance(job.get("id"), str) or not job["id"]
                    or not isinstance(job.get("reason"), str)
                    or not 1 <= len(job["reason"]) <= 200
                    or not isinstance(job.get("conversation_id"), str)
                    or not 1 <= len(job["conversation_id"]) <= 200
                    or not cls._finite_number(job.get("created_at"))
                    or not cls._finite_number(job.get("due_at"))
                    or not MIN_DELAY <= job["due_at"] - job["created_at"] <= MAX_DELAY):
            raise ValueError("invalid_initiative_job")
        if "capacity_queued_at" in job:
            queued_at = job["capacity_queued_at"]
            if (not cls._finite_number(queued_at)
                    or not job["due_at"] <= queued_at <= job["due_at"] + MAX_OVERDUE):
                raise ValueError("invalid_capacity_queue")
        if "capacity_wait_seconds" in job:
            wait = job["capacity_wait_seconds"]
            if ("capacity_queued_at" not in job or not cls._finite_number(wait)
                    or not 0 <= wait <= MAX_CAPACITY_WAIT):
                raise ValueError("invalid_capacity_wait")

    @staticmethod
    def _expiry_reason(job, now):
        queued_at = job.get("capacity_queued_at")
        if queued_at is not None:
            return "queue_timeout" if now - queued_at > MAX_CAPACITY_WAIT else None
        return "expired" if now - job["due_at"] > MAX_OVERDUE else None

    @staticmethod
    def _valid_receipt_umo(umo):
        if (not isinstance(umo, str) or len(umo) > 512
                or any(char.isspace() or ord(char) < 32 for char in umo)):
            return False
        parts = umo.split(":", 2)
        return (len(parts) == 3 and bool(parts[0]) and bool(parts[2])
                and parts[1] in ("GroupMessage", "FriendMessage"))

    @staticmethod
    def _valid_receipt_code(value, maximum, empty=False):
        if not isinstance(value, str) or len(value) > maximum:
            return False
        if not value:
            return empty
        if value.lower().startswith(("sk-", "bearer:", "token:", "api_key:")):
            return False
        return _RECEIPT_CODE.fullmatch(value) is not None

    @classmethod
    def _validate_terminal_receipt(cls, row):
        # Same metadata schema as OutcomeLog. Keeping validation local also
        # allows this pure store to be imported without the AstrBot package.
        if (not isinstance(row, dict) or set(row) != _RECEIPT_FIELDS
                or not isinstance(row["id"], str) or not _RECEIPT_IDENTIFIER.fullmatch(row["id"])
                or not cls._valid_receipt_umo(row["umo"])
                or not isinstance(row["job_id"], str) or not _RECEIPT_IDENTIFIER.fullmatch(row["job_id"])
                or not cls._finite_number(row["at"])
                or not cls._valid_receipt_code(row["outcome"], 48)
                or not cls._valid_receipt_code(row["reason"], 96, empty=True)
                or type(row["delivered_parts"]) is not int or row["delivered_parts"] < 0):
            raise ValueError("invalid_terminal_receipt")

    @classmethod
    def _validate_state(cls, state):
        if (not isinstance(state, dict) or state.get("version") != 1
                or not isinstance(state.get("jobs"), dict)
                or not isinstance(state.get("recent"), dict)
                or not isinstance(state.get("claimed", {}), dict)
                or not isinstance(state.get("terminal_receipts", []), list)
                or len(state.get("terminal_receipts", [])) > MAX_TERMINAL_RECEIPTS
                or type(state.get("terminal_receipts_dropped", 0)) is not int
                or state.get("terminal_receipts_dropped", 0) < 0):
            raise ValueError("invalid_initiative_state")
        for umo, job in state["jobs"].items():
            cls._validate_job(umo, job)
        # The optional field keeps v1 files readable by the previous plugin.
        # Claims survive process death and are never returned by pop_due again.
        for job_id, claim in state.get("claimed", {}).items():
            if (not isinstance(job_id, str) or not job_id
                    or not isinstance(claim, dict)
                    or not isinstance(claim.get("job"), dict)
                    or claim["job"].get("id") != job_id
                    or not cls._finite_number(claim.get("claimed_at"))):
                raise ValueError("invalid_initiative_claim")
            cls._validate_job(claim["job"].get("umo"), claim["job"])
        receipt_ids, receipt_jobs = set(), set()
        pending_ids = {job["id"] for job in state["jobs"].values()}
        for row in state.get("terminal_receipts", []):
            cls._validate_terminal_receipt(row)
            if (row["id"] in receipt_ids or row["job_id"] in receipt_jobs
                    or row["job_id"] in state.get("claimed", {})
                    or row["job_id"] in pending_ids):
                raise ValueError("duplicate_terminal_receipt")
            receipt_ids.add(row["id"])
            receipt_jobs.add(row["job_id"])
        for umo, rows in state["recent"].items():
            if not isinstance(umo, str) or not isinstance(rows, list) or len(rows) > MAX_RECENT:
                raise ValueError("invalid_initiative_recent")
            for row in rows:
                if (not isinstance(row, dict) or row.get("role") not in ("user", "assistant")
                        or not isinstance(row.get("text"), str)
                        or not 1 <= len(row["text"]) <= MAX_TEXT
                        or not cls._finite_number(row.get("at"))
                        or any(not isinstance(row.get(key), str) or len(row[key]) > 128
                               for key in ("speaker_id", "speaker_name", "message_id"))):
                    raise ValueError("invalid_initiative_recent_row")

    def _commit(self, state):
        # Keep current in-memory state intact if writing or replacing fails.
        self._validate_state(state)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                             dir=self.path.parent,
                                             prefix=self.path.name + ".", suffix=".tmp",
                                             delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(state, handle, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self._state = state
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def allowed(self, umo):
        parts = umo.split(":", 2) if isinstance(umo, str) else []
        if (not self.config.get("proactive_enabled", False)
                or not self.config.get("self_initiated_enabled", False)
                or len(parts) != 3 or not parts[0].strip() or not parts[2].strip()):
            return False
        if parts[1] == "FriendMessage":
            return bool(self.config.get("private_self_initiated_enabled", True))
        return (parts[1] == "GroupMessage"
                and (bool(self.config.get("proactive_all_groups", True))
                     or umo in self.config.get("proactive_group_whitelist", [])))

    def quiet(self, timestamp):
        hour = datetime.fromtimestamp(timestamp, CHINA_TIME).hour
        start = int(self.config.get("proactive_quiet_start", 23))
        end = int(self.config.get("proactive_quiet_end", 8))
        if not 0 <= start <= 23 or not 0 <= end <= 23:
            raise ValueError("invalid_quiet_hours")
        return ((start <= hour < end) if start < end
                else (hour >= start or hour < end) if start > end else False)

    def schedule(self, umo, delay_seconds, reason, conversation_id):
        with self._lock:
            if not self.allowed(umo):
                raise ValueError("outside_self_initiated_scope")
            if type(delay_seconds) is not int or not MIN_DELAY <= delay_seconds <= MAX_DELAY:
                raise ValueError("delay_must_be_integer_300_to_7200")
            if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 200:
                raise ValueError("reason_must_be_1_to_200_characters")
            if (not isinstance(conversation_id, str) or not conversation_id.strip()
                    or len(conversation_id) > 200):
                raise ValueError("conversation_id_required")
            now = self._clock()
            due = now + delay_seconds
            if self.quiet(due):
                raise ValueError("due_during_quiet_hours")
            if len(self._state.get("claimed", {})) >= MAX_RETAINED_CLAIMS:
                raise ValueError("claim_capacity_exhausted")
            job = {"id": uuid.uuid4().hex, "umo": umo, "created_at": now,
                   "due_at": due, "reason": reason.strip(),
                   "conversation_id": conversation_id}
            state = deepcopy(self._state)
            state["jobs"][umo] = job
            self._commit(state)
            return deepcopy(job)

    def list_pending(self, umo=None):
        with self._lock:
            return [deepcopy(job) for key, job in self._state["jobs"].items()
                    if umo is None or key == umo]

    def cancel(self, umo, job_id=None):
        with self._lock:
            job = self._state["jobs"].get(umo)
            if not job or (job_id is not None and job["id"] != job_id):
                return False
            state = deepcopy(self._state)
            del state["jobs"][umo]
            self._commit(state)
            return True

    def cancel_unclaimable_pending(self):
        """Record and remove all plans when retained claims exhaust capacity."""
        with self._lock:
            if (len(self._state.get("claimed", {})) < MAX_RETAINED_CLAIMS
                    or not self._state["jobs"]):
                return 0
            state = deepcopy(self._state)
            discarded = [{"job": deepcopy(job), "reason": "claim_capacity_exhausted"}
                         for job in state["jobs"].values()]
            state["jobs"] = {}
            self._commit(state)
            self._discarded.extend(discarded)
            return len(discarded)

    def list_claimed(self, umo=None):
        """Return durable in-flight claims for recovery/status, never execution."""
        with self._lock:
            return [deepcopy(claim) for claim in self._state.get("claimed", {}).values()
                    if umo is None or claim["job"]["umo"] == umo]

    def list_blocking_claimed(self, umo=None):
        """Claims still blocking this process, excluding locally terminal ones."""
        with self._lock:
            return [deepcopy(claim) for job_id, claim in self._state.get("claimed", {}).items()
                    if job_id not in self._released_claim_ids
                    and (umo is None or claim["job"]["umo"] == umo)]

    def claim_capacity_remaining(self):
        """Global durable-claim capacity, including locally released evidence."""
        with self._lock:
            return max(0, MAX_RETAINED_CLAIMS - len(self._state.get("claimed", {})))

    def list_terminal_receipts(self, umo=None):
        """Bounded durable terminal metadata, newest first; never executable."""
        with self._lock:
            rows = sorted(self._state.get("terminal_receipts", []), key=lambda row: row["at"])
            return [deepcopy(row) for row in reversed(rows)
                    if umo is None or row["umo"] == umo]

    def terminal_receipts_dropped(self):
        with self._lock:
            return self._state.get("terminal_receipts_dropped", 0)

    def archive_claim(self, job_id, row):
        """Atomically exchange an in-flight claim for a terminal receipt.

        A separate journal may be unavailable while this file remains writable.
        The bounded receipt ring preserves recent results without holding their
        claims forever. Older results follow the explicit retention counter.
        """
        with self._lock:
            copied = deepcopy(row)
            self._validate_terminal_receipt(copied)
            if copied["job_id"] != job_id:
                raise ValueError("terminal_receipt_job_mismatch")
            receipts = self._state.get("terminal_receipts", [])
            existing = next((item for item in receipts if item["job_id"] == job_id), None)
            if existing is not None:
                if copied["umo"] != existing["umo"]:
                    raise ValueError("terminal_receipt_umo_mismatch")
                return True
            claim = self._state.get("claimed", {}).get(job_id)
            if claim is None:
                return False
            if copied["umo"] != claim["job"]["umo"]:
                raise ValueError("terminal_receipt_umo_mismatch")
            if any(item["id"] == copied["id"] for item in receipts):
                raise ValueError("duplicate_terminal_receipt")
            state = deepcopy(self._state)
            del state["claimed"][job_id]
            rows = sorted(deepcopy(receipts) + [copied], key=lambda item: item["at"])
            dropped = max(0, len(rows) - MAX_TERMINAL_RECEIPTS)
            state["terminal_receipts"] = rows[-MAX_TERMINAL_RECEIPTS:]
            state["terminal_receipts_dropped"] = state.get("terminal_receipts_dropped", 0) + dropped
            self._commit(state)
            self._released_claim_ids.discard(job_id)
            return True

    def finish_terminal_receipt(self, job_id):
        """Remove a receipt only after the caller has persisted its journal row."""
        with self._lock:
            rows = self._state.get("terminal_receipts", [])
            if not any(row["job_id"] == job_id for row in rows):
                return False
            state = deepcopy(self._state)
            state["terminal_receipts"] = [row for row in state["terminal_receipts"]
                                          if row["job_id"] != job_id]
            self._commit(state)
            return True

    def release_claim_locally(self, job_id):
        """Release only this process's barrier; disk claim remains recoverable."""
        with self._lock:
            if job_id not in self._state.get("claimed", {}):
                return False
            self._released_claim_ids.add(job_id)
            return True

    def is_claim_locally_released(self, job_id):
        with self._lock:
            return job_id in self._released_claim_ids

    def finish_claim(self, job_id):
        """Forget one claim after its terminal outcome is durably recorded."""
        with self._lock:
            if job_id not in self._state.get("claimed", {}):
                self._released_claim_ids.discard(job_id)
                return False
            state = deepcopy(self._state)
            del state["claimed"][job_id]
            self._commit(state)
            self._released_claim_ids.discard(job_id)
            return True

    def pop_due(self, max_jobs=None, exclude_umos=()):
        """Atomically move eligible due jobs to claims, without replaying claims.

        Busy/capacity-limited eligible jobs stay pending. Expiry and policy
        discards retain their existing behavior, even when capacity is zero.
        """
        if max_jobs is not None and (type(max_jobs) is not int or max_jobs < 0):
            raise ValueError("invalid_claim_capacity")
        if isinstance(exclude_umos, (str, bytes)):
            raise ValueError("invalid_claim_exclusions")
        excluded = set(exclude_umos)
        if any(not isinstance(umo, str) for umo in excluded):
            raise ValueError("invalid_claim_exclusions")
        with self._lock:
            now = self._clock()
            state = deepcopy(self._state)
            # Apply under the same lock/commit as claiming. Old files above the
            # limit remain readable and untouched; only new claims are paused.
            retained_slots = max(0, MAX_RETAINED_CLAIMS - len(state.get("claimed", {})))
            claim_limit = retained_slots if max_jobs is None else min(max_jobs, retained_slots)
            due = []
            discarded = []
            claimed_umos = {claim["job"]["umo"] for job_id, claim in state.get("claimed", {}).items()
                            if job_id not in self._released_claim_ids}
            ordered = sorted(state["jobs"].items(), key=lambda item: item[1]["due_at"])
            for umo, job in ordered:
                if job["due_at"] > now:
                    continue
                if self._expiry_reason(job, now):
                    reason = self._expiry_reason(job, now)
                elif not self.allowed(umo):
                    reason = "scope_disabled"
                elif self.quiet(now):
                    reason = "quiet_hours"
                else:
                    reason = None
                    if (umo in excluded or umo in claimed_umos
                            or len(due) >= claim_limit):
                        # Mark only a still-fresh, policy-eligible task that is
                        # actually held by a capacity/session barrier.
                        job.setdefault("capacity_queued_at", now)
                        continue
                    if "capacity_queued_at" in job:
                        job["capacity_wait_seconds"] = max(0, now - job["capacity_queued_at"])
                    # One atomic replacement preserves evidence across a hard
                    # exit after claiming but before task creation/generation.
                    state.setdefault("claimed", {})[job["id"]] = {
                        "job": deepcopy(job), "claimed_at": now}
                    claimed_umos.add(umo)
                    due.append(deepcopy(job))
                del state["jobs"][umo]
                if reason:
                    discarded.append({"job": deepcopy(job), "reason": reason})
            if state != self._state:
                self._commit(state)
                self._discarded.extend(discarded)
            return sorted(due, key=lambda job: job["due_at"])

    def drain_discarded(self):
        """Return committed removals for the caller's outcome journal, once."""
        with self._lock:
            discarded = deepcopy(self._discarded)
            self._discarded.clear()
            return discarded

    @staticmethod
    def _text(value, limit):
        return " ".join(str(value or "").split())[:limit]

    def remember(self, umo, role, text, speaker_id="", speaker_name="", message_id="", at=None):
        with self._lock:
            if not self.allowed(umo):
                return False
            if role not in ("user", "assistant"):
                raise ValueError("invalid_message_role")
            text = self._text(text, MAX_TEXT)
            if not text:
                return False
            stamp = self._clock() if at is None else float(at)
            if not math.isfinite(stamp):
                raise ValueError("invalid_message_timestamp")
            row = {"role": role, "text": text, "at": stamp,
                   "speaker_id": self._text(speaker_id, 128),
                   "speaker_name": self._text(speaker_name, 128),
                   "message_id": self._text(message_id, 128)}
            rows = self._state["recent"].get(umo, [])
            if row["message_id"] and any(
                    old["role"] == role and old["message_id"] == row["message_id"] for old in rows):
                return False
            state = deepcopy(self._state)
            state["recent"][umo] = (rows + [row])[-MAX_RECENT:]
            self._commit(state)
            return True

    def recent(self, umo):
        with self._lock:
            return deepcopy(self._state["recent"].get(umo, []))
