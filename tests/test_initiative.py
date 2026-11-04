from datetime import datetime
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("initiative", ROOT.parent / "initiative.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Store = module.InitiativeStore
UMO = "default:GroupMessage:100001"


def stamp(hour=12, minute=0):
    return datetime(2026, 10, 31, hour, minute, tzinfo=module.CHINA_TIME).timestamp()


class InitiativeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "state.json"
        self.clock = [stamp()]
        self.config = {"private_self_initiated_enabled": False, "proactive_all_groups": False, "proactive_enabled": True, "self_initiated_enabled": True,
                       "proactive_group_whitelist": [UMO],
                       "proactive_quiet_start": 23, "proactive_quiet_end": 8}
        self.store = Store(self.config, self.path, now=lambda: self.clock[0])

    def plan(self, **changes):
        args = {"umo": UMO, "delay_seconds": 300, "reason": "等大家把话说完，稍后再接一句",
                "conversation_id": "conv-1"}
        args.update(changes)
        return self.store.schedule(**args)

    def reload(self):
        return Store(self.config, self.path, now=lambda: self.clock[0])

    def test_scope_and_all_switches(self):
        for key in ("proactive_enabled", "self_initiated_enabled"):
            self.config[key] = False
            with self.assertRaisesRegex(ValueError, "scope"):
                self.plan()
            self.config[key] = True
        for umo in ("default:GroupMessage:other", "default:FriendMessage:100001"):
            with self.assertRaisesRegex(ValueError, "scope"):
                self.plan(umo=umo)
        self.assertFalse(self.store.remember("other:GroupMessage:1", "user", "hello"))

    def test_delay_type_and_bounds(self):
        for value in (True, "300", 300.0, 299, 7201, float("nan")):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "integer"):
                self.plan(delay_seconds=value)
        self.assertEqual(self.plan(delay_seconds=7200)["due_at"], stamp() + 7200)

    def test_reason_and_current_conversation_required(self):
        for reason in ("", "  ", "a" * 201, None):
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, "reason"):
                self.plan(reason=reason)
        for cid in (None, "", " ", "x" * 201):
            with self.subTest(cid=cid), self.assertRaisesRegex(ValueError, "conversation"):
                self.plan(conversation_id=cid)
        self.assertEqual(self.plan(conversation_id="active-conv")["conversation_id"], "active-conv")

    def test_new_plan_replaces_old_and_cancel_checks_identity(self):
        old = self.plan()
        new = self.plan(delay_seconds=600)
        self.assertNotEqual(old["id"], new["id"])
        self.assertEqual(self.store.list_pending(), [new])
        self.assertFalse(self.store.cancel(UMO, old["id"]))
        self.assertTrue(self.store.cancel(UMO, new["id"]))
        self.assertFalse(self.store.cancel(UMO))
        self.assertEqual(self.reload().list_pending(), [])

    def test_pending_survives_restart_and_is_claimed_at_most_once(self):
        job = self.plan()
        self.clock[0] += 299
        restarted = self.reload()
        self.assertEqual(restarted.list_pending(UMO), [job])
        self.assertEqual(restarted.pop_due(), [])
        self.clock[0] += 1
        self.assertEqual(restarted.pop_due(), [job])
        # A simulated crash before sending cannot cause a replay after reload.
        self.assertEqual(self.reload().pop_due(), [])
        self.assertEqual(restarted.pop_due(), [])

    def test_overdue_grace_and_expiry_on_restart(self):
        self.plan()
        self.clock[0] += 420
        self.assertEqual(len(self.reload().pop_due()), 1)
        self.clock[0] = stamp()
        self.plan()
        self.clock[0] += 421
        restarted = self.reload()
        self.assertEqual(restarted.list_pending(), [])
        self.assertEqual(json.loads(self.path.read_text())["jobs"], {})

    def test_live_disable_whitelist_and_quiet_rechecked_when_due(self):
        for mutation in (lambda: self.config.update(self_initiated_enabled=False),
                         lambda: self.config.update(proactive_group_whitelist=[]),
                         lambda: self.config.update(proactive_quiet_start=12, proactive_quiet_end=13)):
            self.config.update(self_initiated_enabled=True, proactive_group_whitelist=[UMO],
                               proactive_quiet_start=23, proactive_quiet_end=8)
            self.clock[0] = stamp()
            self.plan()
            mutation()
            self.clock[0] += 300
            self.assertEqual(self.store.pop_due(), [])
            self.assertEqual(self.store.list_pending(), [])

    def test_due_quiet_boundaries_china_timezone(self):
        for hour, minute, delay, accepted in ((22, 55, 300, False), (22, 54, 300, True),
                                               (7, 55, 300, True), (7, 54, 300, False)):
            self.clock[0] = stamp(hour, minute)
            if accepted:
                self.plan(delay_seconds=delay)
            else:
                with self.assertRaisesRegex(ValueError, "quiet"):
                    self.plan(delay_seconds=delay)

    def test_recent_bounded_deduplicated_persistent_and_copied(self):
        for index in range(25):
            self.assertTrue(self.store.remember(UMO, "user", str(index) + "x" * 700,
                                                speaker_id="u1", speaker_name="昵称",
                                                message_id=f"message-{index}"))
        self.assertFalse(self.store.remember(UMO, "user", "重复消息", message_id="message-24"))
        self.assertTrue(self.store.remember(UMO, "assistant", "实际发出的回复", message_id="message-24"))
        rows = self.reload().recent(UMO)
        self.assertEqual(len(rows), 20)
        self.assertTrue(all(len(row["text"]) <= 600 for row in rows))
        self.assertEqual(rows[-1]["role"], "assistant")
        self.assertEqual(rows[-2]["speaker_name"], "昵称")
        rows.clear()
        self.assertEqual(len(self.store.recent(UMO)), 20)

    def test_corrupt_file_is_not_overwritten(self):
        for text in ("{bad json", '{"version":1,"jobs":[],"recent":{}}',
                     '{"version":1,"jobs":{"somewhere":{}},"recent":{}}'):
            self.path.write_text(text, encoding="utf-8")
            with self.assertRaises((ValueError, json.JSONDecodeError)):
                self.reload()
            self.assertEqual(self.path.read_text(encoding="utf-8"), text)

    def test_failed_durable_claim_does_not_return_or_forget_job(self):
        job = self.plan()
        self.clock[0] += 300
        with patch.object(module.os, "replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.store.pop_due()
        self.assertEqual(self.store.list_pending(), [job])
        self.assertEqual(self.reload().pop_due(), [job])


if __name__ == "__main__":
    unittest.main()
