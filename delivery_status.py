"""Metadata-only local observations; no platform receipt or retry mechanism."""

from copy import deepcopy
import asyncio
import uuid
from types import FunctionType


EVENT_KEY = "turn_taking.delivery_observation"
MAX_OBSERVATIONS = 100
OBSERVATION_TIMEOUT = 600
ACTIVE_PHASES = {"processing", "generating", "preparing", "sending"}


def explicit_onebot_rejection(error):
    """Only structured, negative OneBot results are proof of API failure."""
    try:
        cls = type(error)
        result = getattr(error, "result", None)
        code = getattr(error, "retcode", None)
        return (cls.__module__ == "aiocqhttp.exceptions" and cls.__name__ == "ActionFailed"
                and isinstance(result, dict) and result.get("status") == "failed"
                and type(code) is int and code != 0)
    except Exception:
        return False


def atomic_message(message):
    # OneBot sends Node/Nodes/File chains one by one: a later rejection cannot
    # prove that an earlier component was not sent. Never retain the components.
    try:
        chain = getattr(message, "chain", None)
        return (isinstance(chain, (list, tuple)) and bool(chain)
                and not any(type(comp).__name__ in {"Node", "Nodes", "File"} for comp in chain))
    except Exception:
        return False


class LocalDeliveryObservations:
    def __init__(self, now):
        self.now = now
        self.rows = {}
        self.dropped = 0

    def begin(self, event):
        key = event.get_extra(EVENT_KEY)
        if key is None:
            key = uuid.uuid4().hex
            event.set_extra(EVENT_KEY, key)
        if key not in self.rows:
            at = self.now()
            self.rows[key] = {"id": key, "umo": event.unified_msg_origin,
                              "kind": "ordinary", "phase": "processing", "phase_at": at,
                              "started_at": at, "observed_at": at, "reason": "normal_reply_requested"}
            if len(self.rows) > MAX_OBSERVATIONS:
                self.rows.pop(next(iter(self.rows)))
                self.dropped += 1
        try:
            self._install_send_observer(event)
        except Exception:
            pass  # Ordinary replies must survive unavailable diagnostics.
        return key

    def _install_send_observer(self, event):
        current = getattr(event, "send", None)
        if not callable(current) or getattr(current, "_delivery_observer_owner", None) is self:
            return
        original = (getattr(current, "_delivery_observer_original", current)
                    if isinstance(current, FunctionType) else current)

        def observe(method, *args):
            # Diagnostics never replace an adapter's return value or exception.
            try:
                method(event, *args)
            except Exception:
                pass

        async def send(*args, **kwargs):
            message = args[0] if args else kwargs.get("message", kwargs.get("message_chain"))
            observe(self._send_started)
            try:
                result = await original(*args, **kwargs)
            except asyncio.CancelledError:
                observe(self._send_problem, False, "send_canceled")
                raise
            except Exception as error:
                rejected = atomic_message(message) and explicit_onebot_rejection(error)
                observe(self._send_problem, rejected, "platform_rejected" if rejected else "send_exception")
                raise
            else:
                observe(self._send_returned)
                return result

        send._delivery_observer_owner = self
        send._delivery_observer_original = original
        try:
            event.send = send
        except (AttributeError, TypeError):
            return  # An immutable event keeps the existing hook/timeout path.
        self.rows[event.get_extra(EVENT_KEY)]["send_observer"] = True

    def _row(self, event):
        return self.rows.get(event.get_extra(EVENT_KEY))

    def _phase(self, row, phase, reason):
        at = self.now()
        row.update(phase=phase, phase_at=at, observed_at=at, reason=reason)

    def _send_started(self, event):
        row = self._row(event)
        if row is not None:
            row["send_attempts"] = row.get("send_attempts", 0) + 1
            self._phase(row, "sending", "native_send_in_progress")

    def _send_problem(self, event, rejected, reason):
        row = self._row(event)
        if row is None:
            return
        row["send_problems"] = row.get("send_problems", 0) + 1
        row["api_accepted_observed"] = bool(row.get("api_accepted_observed")
                                             or getattr(event, "_has_send_oper", False))
        row["last_attempt"] = "rejected" if rejected else "unconfirmed"
        row["last_problem_reason"] = reason
        phase = "partially_accepted" if row["api_accepted_observed"] else row["last_attempt"]
        self._phase(row, phase, reason)

    def _send_returned(self, event):
        row = self._row(event)
        if row is None:
            return
        if getattr(event, "_has_send_oper", False):
            row["api_accepted_observed"] = True
            if row.get("send_problems"):
                self._phase(row, "partially_accepted", "some_sends_unconfirmed")
            else:
                self._phase(row, "api_accepted", "native_send_returned_with_flag")
        else:
            self._send_problem(event, False, "no_send_confirmation")

    def update(self, event, phase, reason):
        key = event.get_extra(EVENT_KEY)
        row = self.rows.get(key)
        if row is None:
            return  # No observation is preferable to inventing one after restart.
        if row.get("send_problems"):
            if (phase in ("api_accepted", "partially_accepted")
                    and getattr(event, "_has_send_oper", False) and not row.get("api_accepted_observed")):
                row["api_accepted_observed"] = True
                self._phase(row, "partially_accepted", "some_sends_unconfirmed")
            return
        if row["phase"] in ("api_accepted", "partially_accepted", "rejected"):
            return  # Repeated hooks/tool rounds must not roll back success.
        self._phase(row, phase, reason)

    def snapshot(self, umo=None, exclude_id=None):
        now = self.now()
        for row in self.rows.values():
            if row["phase"] in ACTIVE_PHASES and now - row["phase_at"] > OBSERVATION_TIMEOUT:
                row.update(phase="unconfirmed", phase_at=now, reason="success_hook_not_observed")
        return deepcopy([row for row in self.rows.values()
                         if row["id"] != exclude_id and (umo is None or row["umo"] == umo)])
