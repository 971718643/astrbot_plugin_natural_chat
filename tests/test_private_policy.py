"""Private scheduling scope and durable isolation; no AstrBot/network stubs."""

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SOURCE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


Policy = load("private_policy", "proactive.py").ProactivePolicy
Store = load("private_store", "initiative.py").InitiativeStore
TZ = timezone(timedelta(hours=8))
A = "default:FriendMessage:123"
B = "default:FriendMessage:456"
GROUP = "default:GroupMessage:123"


def stamp(hour=12, day=31):
    return datetime(2026, 10, day, hour, tzinfo=TZ).timestamp()


class PrivatePolicyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.clock = [stamp()]
        self.config = {"proactive_enabled": True, "self_initiated_enabled": True,
                       "proactive_all_groups": True, "proactive_group_whitelist": [],
                       "proactive_probability": 1.0, "proactive_startup_grace_seconds": 0,
                       "proactive_cooldown_seconds": 300, "proactive_max_per_hour": 3,
                       "proactive_max_per_day": 12, "proactive_quiet_start": 23,
                       "proactive_quiet_end": 8}
        self.path = self.folder / "attempts.json"
        self.store_path = self.folder / "plans.json"
        self.policy = Policy(self.config, self.path, now=lambda: self.clock[0], draw=lambda: 0)
        self.store = Store(self.config, self.store_path, now=lambda: self.clock[0])

    def plan(self, umo=A, delay=300):
        return self.store.schedule(umo, delay, "稍后根据最新对话再考虑接一句", "conv-" + umo)

    def test_private_scope_defaults_enabled_without_affecting_group(self):
        for umo in (A, B, GROUP):
            with self.subTest(umo=umo):
                self.assertTrue(self.policy.admit(umo)[0])
                self.assertTrue(self.store.allowed(umo))
        self.assertNotIn("private_self_initiated_enabled", self.config)

    def test_private_switch_is_independent_of_group_whitelist(self):
        self.config["private_self_initiated_enabled"] = False
        self.assertEqual(self.policy.admit(A)[1], "private_self_initiated_disabled")
        self.assertFalse(self.store.allowed(A))
        self.assertTrue(self.policy.admit(GROUP)[0])
        self.assertTrue(self.store.allowed(GROUP))

    def test_existing_global_switches_also_disable_private_plans(self):
        for key in ("proactive_enabled", "self_initiated_enabled"):
            self.config[key] = False
            self.assertFalse(self.policy.admit(A)[0])
            self.assertFalse(self.store.allowed(A))
            with self.assertRaisesRegex(ValueError, "scope"):
                self.plan()
            self.config[key] = True

    def test_group_all_and_whitelist_modes_remain_unchanged(self):
        self.config["proactive_all_groups"] = False
        self.assertFalse(self.policy.admit(GROUP)[0])
        self.assertFalse(self.store.allowed(GROUP))
        self.assertTrue(self.policy.admit(A)[0])
        self.assertTrue(self.store.allowed(A))
        self.config["proactive_group_whitelist"] = [GROUP]
        self.assertTrue(self.policy.admit(GROUP)[0])
        self.assertTrue(self.store.allowed(GROUP))
        self.assertFalse(self.policy.admit("default:GroupMessage:999")[0])
        self.assertFalse(self.store.allowed("default:GroupMessage:999"))

    def test_private_messages_never_run_random_draw_or_consume_attempt(self):
        self.policy.draw = lambda: (_ for _ in ()).throw(AssertionError("private random draw"))
        self.assertTrue(self.policy.admit(A)[0])
        self.assertEqual(self.policy.select(A, "正常的一条私聊消息")[1], "not_group_session")
        self.assertEqual(self.policy.history, {})

    def test_malformed_and_other_message_types_are_rejected(self):
        for umo in (None, 123, "", "default", "default:FriendMessage:",
                    ":FriendMessage:123", " :FriendMessage:123", "default:FriendMessage: ",
                    "default:GroupMessage:", "default:Unknown:123", "default:GuildMessage:123"):
            with self.subTest(umo=umo):
                self.assertFalse(self.policy.admit(umo)[0])
                self.assertFalse(self.store.allowed(umo))

    def test_cooldown_and_successful_normal_reply_apply_to_private(self):
        self.assertEqual(self.policy.admit(A, last_bot_ts=self.clock[0] - 10)[1], "proactive_cooldown")
        self.policy.record_attempt(A)
        self.assertEqual(self.policy.admit(A)[1], "proactive_cooldown")
        self.clock[0] += 301
        self.assertTrue(self.policy.admit(A)[0])

    def test_attempt_counts_are_separate_for_users_and_same_id_group(self):
        self.policy.history[A] = [self.clock[0] - 1200, self.clock[0] - 800, self.clock[0] - 400]
        self.assertEqual(self.policy.admit(A)[1], "proactive_hour_cap")
        self.assertTrue(self.policy.admit(B)[0])
        self.assertTrue(self.policy.admit(GROUP)[0])

    def test_daily_limit_resets_at_beijing_date(self):
        self.clock[0] = stamp(hour=22)
        self.policy.history[A] = [stamp(hour=8) + index * 2000 for index in range(12)]
        self.assertEqual(self.policy.admit(A)[1], "proactive_day_cap")
        self.clock[0] = datetime(2026, 11, 1, 8, tzinfo=TZ).timestamp()
        self.assertTrue(self.policy.admit(A)[0])

    def test_startup_grace_and_quiet_hours_are_not_relaxed(self):
        self.config["proactive_startup_grace_seconds"] = 300
        self.assertEqual(self.policy.admit(A)[1], "proactive_startup_grace")
        self.clock[0] += 300
        self.assertTrue(self.policy.admit(A)[0])
        for hour in (23, 0, 7):
            self.clock[0] = stamp(hour=hour)
            self.policy.started_at = self.clock[0] - 301
            self.assertEqual(self.policy.admit(A)[1], "quiet_hours")
        self.clock[0] = stamp(hour=22) + 59 * 60
        with self.assertRaisesRegex(ValueError, "quiet"):
            self.plan(delay=300)

    def test_attempt_ledger_survives_restart_for_private(self):
        self.policy.record_attempt(A)
        restarted = Policy(self.config, self.path, now=lambda: self.clock[0])
        self.assertEqual(restarted.admit(A)[1], "proactive_cooldown")
        self.assertTrue(restarted.admit(B)[0])

    def test_private_pending_and_recent_messages_are_isolated_and_durable(self):
        jobs = {umo: self.plan(umo) for umo in (A, B, GROUP)}
        for umo, text in ((A, "甲的私聊"), (B, "乙的私聊"), (GROUP, "同ID的群聊")):
            self.store.remember(umo, "user", text, message_id="same-message-id")
        restarted = Store(self.config, self.store_path, now=lambda: self.clock[0])
        self.assertEqual({job["umo"]: job for job in restarted.list_pending()}, jobs)
        self.assertEqual([row["text"] for row in restarted.recent(A)], ["甲的私聊"])
        self.assertEqual([row["text"] for row in restarted.recent(B)], ["乙的私聊"])
        self.assertEqual([row["text"] for row in restarted.recent(GROUP)], ["同ID的群聊"])
        restarted.cancel(A)
        self.assertEqual({job["umo"] for job in restarted.list_pending()}, {B, GROUP})

    def test_private_due_claim_is_once_and_disabled_plan_is_discarded(self):
        self.plan()
        self.clock[0] += 300
        self.assertEqual([job["umo"] for job in self.store.pop_due()], [A])
        self.assertEqual(self.store.pop_due(), [])
        self.plan(B)
        self.clock[0] += 300
        self.config["private_self_initiated_enabled"] = False
        self.assertEqual(self.store.pop_due(), [])
        self.assertEqual(self.store.list_pending(), [])

    def test_expired_private_plan_is_not_replayed_after_restart(self):
        self.plan()
        self.clock[0] += 421
        restarted = Store(self.config, self.store_path, now=lambda: self.clock[0])
        self.assertEqual(restarted.list_pending(), [])
        self.assertEqual(restarted.pop_due(), [])


if __name__ == "__main__":
    unittest.main()
