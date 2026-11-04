"""Rolling deadlines, atomic expiry evidence and old file compatibility."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import test_open_threads_store as old


class RollingTopicTTLTests(unittest.TestCase):
    setUp = old.OpenThreadStoreTests.setUp
    open = old.OpenThreadStoreTests.open
    rows = old.OpenThreadStoreTests.rows
    apply = old.OpenThreadStoreTests.apply
    add = old.OpenThreadStoreTests.add
    note = staticmethod(old.OpenThreadStoreTests.note)

    def events(self, umo=old.A, person=old.P, scope=old.S):
        return self.store.list_expiry_events(umo, person, scope)

    def test_day_four_mention_survives_day_five_and_expires_day_nine(self):
        original = self.add()
        self.clock[0] += 4 * 86400
        self.apply({"mention": [original["id"]]}, turn="real-mention")
        [touched] = self.rows()
        self.assertEqual(touched["expires_at"], original["expires_at"] + 4 * 86400)
        self.clock[0] = original["expires_at"]
        self.assertEqual(self.rows(), [touched])
        self.assertEqual(self.events(), [])
        self.clock[0] = touched["expires_at"]
        self.assertEqual(self.rows(), [])
        [event] = self.events()
        self.assertEqual(event, {key: touched[key] for key in
            ("id", "participant_id", "scope_key", "topic", "expires_at")} |
            {"umo": old.A, "recorded_at": self.clock[0], "reason": "ttl_expired"})
        self.assertFalse({"point", "user_quote", "assistant_quote"} & set(event))

    def test_reads_metadata_upserts_and_duplicate_turns_do_not_extend(self):
        original = self.add()
        self.clock[0] += 86400
        self.apply({"open": [self.note(point="修改描述而未实际提起")]}, turn="metadata")
        [row] = self.rows()
        self.assertEqual(row["last_touch"], original["last_touch"])
        self.assertEqual(row["expires_at"], original["expires_at"])
        before = self.path.read_bytes()
        self.clock[0] += 86400
        self.store.status()
        self.open().list_threads(old.A, old.P, old.S)
        self.events()
        self.assertFalse(self.apply({"progress": [row["id"]]}, turn="metadata"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_old_fixed_deadline_not_extended_on_load_or_resurrected_at_upgrade(self):
        original = self.add()
        state = deepcopy(self.store._state)
        state["threads"][old.A][0]["last_touch"] += 4 * 86400
        self.path.write_text(json.dumps(state), encoding="utf-8")
        self.clock[0] += 4 * 86400
        before = self.path.read_bytes()
        reloaded = self.open()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(reloaded.list_threads(old.A, old.P, old.S)[0]["expires_at"], original["expires_at"])
        self.clock[0] = original["expires_at"]
        reloaded = self.open()
        self.assertEqual(reloaded.list_threads(old.A, old.P, old.S), [])
        self.assertEqual(reloaded.list_expiry_events(old.A, old.P, old.S)[0]["id"], original["id"])

    def test_prune_and_event_commit_are_atomic_on_real_replace_failure(self):
        original = self.add()
        before = self.path.read_bytes()
        self.clock[0] = original["expires_at"]
        with patch.object(old.subject.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.events()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store._state["threads"][old.A], [original])
        self.assertEqual(self.store._state.get("expiry_events", []), [])
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(len(self.open().list_expiry_events(old.A, old.P, old.S)), 1)
        self.store.status()
        self.assertEqual(len(self.events()), 1)

    def test_expiry_record_is_bounded_to_100_and_seven_days_from_prune(self):
        for index in range(105):
            self.apply({"open": [self.note("话题" + str(index))]},
                       umo="default:GroupMessage:" + str(index))
        self.clock[0] += 20 * 86400
        status = self.store.status()
        self.assertEqual(status["expiry_event_count"], 100)
        self.assertEqual(status["expiry_events_dropped"], 5)
        self.assertEqual(status["thread_count"], 0)
        self.assertEqual(status["ttl_mode"], "since_last_touch")
        self.assertEqual(len(self.open()._state["expiry_events"]), 100)
        self.clock[0] += 7 * 86400 - 1
        self.assertEqual(self.store.status()["expiry_event_count"], 100)
        self.clock[0] += 1
        self.assertEqual(self.store.status()["expiry_event_count"], 0)
        self.assertEqual(self.store.status()["expiry_events_dropped"], 105)

    def test_expiry_trace_identity_isolation_and_deepcopy(self):
        self.add()
        self.clock[0] += 5 * 86400
        [event] = self.events()
        for umo, person, scope in ((old.B, old.P, old.S), (old.A, "bob", old.S), (old.A, old.P, "other-persona")):
            self.assertEqual(self.events(umo, person, scope), [])
        event["topic"] = "changed outside store"
        self.assertEqual(self.events()[0]["topic"], "电影结尾")
        self.assertEqual(self.events(), self.events())  # Reads never consume evidence.

    def test_malformed_expiry_data_preserved_without_repair(self):
        self.add()
        self.clock[0] += 5 * 86400
        self.events()
        good = deepcopy(self.store._state)
        for field, value in (("recorded_at", True), ("expires_at", float("inf")),
                             ("reason", "resolved"), ("point", "must not be retained")):
            with self.subTest(field=field):
                state = deepcopy(good)
                state["expiry_events"][0][field] = value
                raw = json.dumps(state).encode()
                self.path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    self.open()
                self.assertEqual(self.path.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
