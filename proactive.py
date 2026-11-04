"""Cheap, persistent admission rules for occasional group participation."""

from datetime import datetime, timedelta, timezone
import json
import random
import re
import time

CHINA_TIME = timezone(timedelta(hours=8))


class ProactivePolicy:
    def __init__(self, config, path, now=time.time, draw=random.random):
        self.config, self.path = config, path
        self.now, self.draw = now, draw
        self.started_at = now()
        self.history = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(self.history, dict):
            raise ValueError("proactive history must be an object")

    def select(self, umo, text, *, last_bot_ts=0.0, other_mention=False, quoted=False):
        cfg, now = self.config, self.now()
        # Random participation remains a group-only feature. Private follow-ups
        # enter through an explicit model plan, never a 5% draw per message.
        parts = umo.split(":", 2) if isinstance(umo, str) else []
        if len(parts) != 3 or parts[1] != "GroupMessage":
            return False, "not_group_session"
        admitted, reason = self.admit(umo, last_bot_ts=last_bot_ts)
        if not admitted:
            return False, reason
        if other_mention or quoted:
            return False, "other_addressee"
        plain = re.sub(r"\s+", "", text or "")
        if plain.startswith(("/", "!", "！")) or len(plain) < 6 or len(set(plain)) < 3:
            return False, "low_information"
        if self.draw() >= float(cfg.get("proactive_probability", 0.05)):
            return False, "proactive_not_selected"
        self.record_attempt(umo)
        return True, "proactive(偶尔加入群聊)"

    def admit(self, umo, *, last_bot_ts=0.0):
        """Shared admission for random and model-planned attempts; no random draw."""
        cfg, now = self.config, self.now()
        if not cfg.get("proactive_enabled", False):
            return False, "proactive_disabled"
        parts = umo.split(":", 2) if isinstance(umo, str) else []
        if len(parts) != 3 or not parts[0].strip() or not parts[2].strip():
            return False, "not_group_session"
        if parts[1] == "FriendMessage":
            if not (cfg.get("private_self_initiated_enabled", True)
                    and cfg.get("self_initiated_enabled", False)):
                return False, "private_self_initiated_disabled"
        elif parts[1] != "GroupMessage":
            return False, "not_group_session"
        elif not cfg.get("proactive_all_groups", True) and umo not in cfg.get("proactive_group_whitelist", []):
            return False, "outside_proactive_groups"
        if now - self.started_at < float(cfg.get("proactive_startup_grace_seconds", 300)):
            return False, "proactive_startup_grace"
        hour = datetime.fromtimestamp(now, CHINA_TIME).hour
        start, end = int(cfg.get("proactive_quiet_start", 23)), int(cfg.get("proactive_quiet_end", 8))
        quiet = (start <= hour < end) if start < end else (hour >= start or hour < end) if start > end else False
        if quiet:
            return False, "quiet_hours"
        attempts = [float(t) for t in self.history.get(umo, []) if now - 48 * 3600 < float(t) <= now]
        cooldown = float(cfg.get("proactive_cooldown_seconds", 300))
        if (last_bot_ts and now - last_bot_ts < cooldown) or (attempts and now - attempts[-1] < cooldown):
            return False, "proactive_cooldown"
        if sum(t > now - 3600 for t in attempts) >= int(cfg.get("proactive_max_per_hour", 3)):
            return False, "proactive_hour_cap"
        today = datetime.fromtimestamp(now, CHINA_TIME).date()
        if sum(datetime.fromtimestamp(t, CHINA_TIME).date() == today for t in attempts) >= int(cfg.get("proactive_max_per_day", 12)):
            return False, "proactive_day_cap"
        return True, "admitted"

    def record_attempt(self, umo):
        now = self.now()
        attempts = [float(t) for t in self.history.get(umo, []) if now - 48 * 3600 < float(t) <= now]
        # Count attempts before generation, so failed deliveries cannot cause a burst.
        attempts.append(now)
        self.history[umo] = attempts
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.history, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
