"""Real files, bounded retention and passive topic lifecycle; no framework/model."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "open_threads.py"
SPEC = importlib.util.spec_from_file_location("open_threads_store_subject", SOURCE)
subject = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(subject)
Store = subject.OpenThreadStore
A = "default:GroupMessage:100"
B = "default:FriendMessage:100"
P = "alice"
S = '["conversation","persona"]'


class OpenThreadStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "open-threads.json"
        self.clock = [1000.0]
        self.store = self.open()
        self.turn = 0

    def open(self):
        return Store(self.path, now=lambda: self.clock[0])

    @staticmethod
    def note(topic="电影结尾", **changes):
        row = {"kind": "deferred", "topic": topic, "point": "结尾的猜想还没说完",
               "user_quote": "这个猜想明天继续说", "assistant_quote": ""}
        row.update(changes)
        return row

    def rows(self, umo=A, person=P, scope=S):
        return self.store.list_threads(umo, person, scope)

    def apply(self, operations, umo=A, person=P, scope=S, turn=None, visible=None, **snapshot):
        self.turn += 1
        if visible is None:
            visible = [row["id"] for row in self.rows(umo, person, scope)]
        return self.store.apply(umo, person, scope, turn or "turn-" + str(self.turn),
                                operations, visible, **snapshot)

    def add(self, topic="电影结尾", **changes):
        self.apply({"open": [self.note(topic, **changes)]})
        return next(row for row in self.rows() if row["topic"] == topic)

    def test_empty_passive_read_and_empty_turn_do_not_create_file(self):
        self.assertEqual(self.rows(), [])
        self.assertFalse(self.apply({}))
        status = self.store.status()
        self.assertEqual({key: status[key] for key in ("thread_count", "session_count", "per_session_limit", "ttl_seconds")},
                         {"thread_count": 0, "session_count": 0, "per_session_limit": 2, "ttl_seconds": 432000})
        self.assertFalse(self.path.exists())

    def test_cross_day_restart_preserves_note_and_read_does_not_touch(self):
        row = self.add()
        original = self.path.read_bytes()
        self.clock[0] += 2 * 24 * 60 * 60
        self.assertEqual(self.open().list_threads(A, P, S), [row])
        self.assertEqual(self.rows(), [row])
        self.assertEqual(self.path.read_bytes(), original)

    def test_five_day_expiry_is_extended_by_real_progress(self):
        row = self.add()
        self.clock[0] += 4 * 24 * 60 * 60
        self.apply({"progress": [{"id": row["id"], "point": "只剩最后一个细节"}]})
        current = self.rows()[0]
        self.assertEqual(current["expires_at"], self.clock[0] + subject.TTL_SECONDS)
        self.assertEqual(current["created_at"], row["created_at"])
        self.assertGreater(current["last_touch"], row["last_touch"])
        self.clock[0] = row["expires_at"]
        self.assertEqual(self.rows(), [current])
        self.clock[0] = current["expires_at"]
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.open().status()["thread_count"], 0)

    def test_third_note_replaces_least_recently_touched_not_oldest_created(self):
        first = self.add("旧但还在聊")
        self.clock[0] += 1
        second = self.add("新的但被放下")
        self.clock[0] += 1
        self.apply({"progress": [{"id": first["id"]}]})
        self.clock[0] += 1
        third = self.add("第三个新话题")
        self.assertEqual({row["id"] for row in self.rows()}, {first["id"], third["id"]})
        self.assertNotIn(second["id"], {row["id"] for row in self.rows()})

    def test_topic_dedupe_normalizes_width_spacing_case_without_sliding_ttl(self):
        first = self.add("Ａ  Test")
        self.clock[0] += 20
        self.apply({"open": [self.note("A Test", point="有了一半答案，剩下一个细节")]})
        [row] = self.rows()
        self.assertEqual(row["id"], first["id"])
        self.assertEqual(row["created_at"], first["created_at"])
        self.assertEqual(row["expires_at"], first["expires_at"])
        self.assertEqual(row["last_touch"], first["last_touch"])
        before = self.path.read_bytes()
        self.clock[0] += 20
        self.apply({"open": [self.note("A Test", point="有了一半答案，剩下一个细节")]})
        self.assertEqual(self.path.read_bytes(), before)

    def test_first_mention_waits_then_one_unanswered_same_person_turn_removes(self):
        row = self.add()
        self.apply({"mention": [{"id": row["id"]}]}, turn="mention")
        [waiting] = self.rows()
        self.assertTrue(waiting["awaiting_followup"])
        self.assertEqual(waiting["awaiting_turn_id"], "mention")
        self.apply({}, turn="next-real-response", awaiting_ids=[row["id"]],
                   awaiting_turns={row["id"]: "mention"})
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.open().list_threads(A, P, S), [])

    def test_source_verified_progress_keeps_topic_and_clears_waiting(self):
        row = self.add()
        self.apply({"mention": [{"id": row["id"]}]})
        self.clock[0] += 3
        self.apply({"progress": [{"id": row["id"], "point": "只剩最后的动机"}]})
        [current] = self.rows()
        self.assertFalse(current["awaiting_followup"])
        self.assertEqual(current["awaiting_turn_id"], "")
        self.assertEqual(current["point"], "只剩最后的动机")
        self.assertEqual(current["last_touch"], self.clock[0])

    def test_snapshot_cannot_remove_a_later_concurrent_mention_even_same_clock(self):
        row = self.add()
        self.apply({"mention": [{"id": row["id"]}]}, turn="old-mention")
        self.apply({"progress": [{"id": row["id"]}]}, turn="intervening-progress")
        self.apply({"mention": [{"id": row["id"]}]}, turn="new-mention")
        self.apply({}, turn="old-request-finishes", awaiting_ids=[row["id"]],
                   awaiting_turns={row["id"]: "old-mention"})
        self.assertEqual(self.rows()[0]["awaiting_turn_id"], "new-mention")

    def test_stale_progress_cannot_clear_newer_waiting_but_fresh_snapshot_can(self):
        row = self.add()
        self.apply({"mention": [{"id": row["id"]}]}, turn="later-mention")
        current = self.rows()[0]
        before = self.path.read_bytes()
        self.assertFalse(self.apply({"progress": [{"id": row["id"], "point": "早先请求的旧进展"}]},
                                    turn="stale-progress", awaiting_ids=[], awaiting_turns={}))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.rows(), [current])
        self.assertTrue(self.apply({"progress": [{"id": row["id"], "point": "真正后来的回答"}]},
                                   turn="fresh-progress", awaiting_ids=[row["id"]],
                                   awaiting_turns={row["id"]: "later-mention"}))
        self.assertFalse(self.rows()[0]["awaiting_followup"])
        self.assertEqual(self.rows()[0]["point"], "真正后来的回答")

    def test_stale_resolve_cannot_delete_newer_waiting_but_fresh_snapshot_can(self):
        row = self.add()
        self.apply({"mention": [{"id": row["id"]}]}, turn="later-mention")
        current = self.rows()[0]
        before = self.path.read_bytes()
        self.assertFalse(self.apply({"resolve": [{"id": row["id"]}]},
                                    turn="stale-resolve", awaiting_ids=[], awaiting_turns={}))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.rows(), [current])
        self.assertTrue(self.apply({"resolve": [{"id": row["id"]}]},
                                   turn="fresh-resolve", awaiting_ids=[row["id"]],
                                   awaiting_turns={row["id"]: "later-mention"}))
        self.assertEqual(self.rows(), [])

    def test_stale_open_cannot_upsert_newer_waiting_but_fresh_snapshot_can(self):
        row = self.add()
        self.apply({"mention": [{"id": row["id"]}]}, turn="later-mention")
        current = self.rows()[0]
        before = self.path.read_bytes()
        self.assertFalse(self.apply({"open": [self.note(point="旧请求写出的旧卡点")]},
                                    turn="stale-open", awaiting_ids=[], awaiting_turns={}))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.rows(), [current])
        self.assertTrue(self.apply({"progress": [{"id": row["id"]}],
                                   "open": [self.note(point="本轮接上后剩下的卡点")]},
                                   turn="fresh-open", awaiting_ids=[row["id"]],
                                   awaiting_turns={row["id"]: "later-mention"}))
        self.assertEqual(self.rows()[0]["point"], "本轮接上后剩下的卡点")
        self.assertEqual(self.rows()[0]["id"], row["id"])

    def test_other_participant_scope_and_conversation_cannot_inject_or_remove(self):
        row = self.add()
        for umo, person, scope in ((B, P, S), (A, "bob", S), (A, P, "new-persona")):
            with self.subTest(context=(umo, person, scope)):
                self.assertEqual(self.rows(umo, person, scope), [])
                self.apply({"resolve": [{"id": row["id"]}]}, umo=umo, person=person,
                           scope=scope, visible=[row["id"]])
                self.assertEqual(self.rows()[0]["id"], row["id"])
        self.apply({}, person="bob", visible=[])
        self.assertEqual(self.rows()[0]["id"], row["id"])

    def test_two_slots_are_per_conversation_not_per_participant(self):
        self.add("alice话题")
        self.clock[0] += 1
        self.apply({"open": [self.note("bob话题")]}, person="bob")
        self.clock[0] += 1
        self.apply({"open": [self.note("bob第二话题")]}, person="bob")
        self.assertEqual(self.rows(), [])
        self.assertEqual(len(self.rows(person="bob")), 2)
        self.assertEqual(self.store.status()["thread_count"], 2)

    def test_resolve_is_atomic_and_conflicting_open_does_not_resurrect(self):
        row = self.add()
        self.apply({"resolve": [{"id": row["id"]}], "open": [self.note()]}, turn="closed")
        self.assertEqual(self.rows(), [])
        original = self.path.read_bytes()
        self.assertFalse(self.apply({"open": [self.note()]}, turn="closed"))
        self.assertEqual(self.path.read_bytes(), original)

    def test_retry_same_turn_does_not_consume_waiting_or_duplicate(self):
        row = self.add()
        self.apply({"mention": [{"id": row["id"]}]}, turn="same-send")
        original = self.path.read_bytes()
        self.assertFalse(self.apply({}, turn="same-send"))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.rows()[0]["awaiting_turn_id"], "same-send")
        self.assertFalse(self.open().apply(A, P, S, "same-send", {}, [row["id"]]))

    def test_turn_id_history_is_bounded(self):
        row = self.add()
        for index in range(40):
            self.clock[0] += 1
            self.apply({"progress": [{"id": row["id"], "point": "进度" + str(index)}]})
        state = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(state["applied_turns"][A]), 32)
        self.assertEqual(len(state["threads"][A]), 1)

    def test_input_and_return_values_are_not_retained(self):
        operations = {"open": [self.note()]}
        original = deepcopy(operations)
        self.apply(operations)
        operations["open"][0]["point"] = "外部修改"
        rows = self.rows()
        rows[0]["point"] = "另一个外部修改"
        self.assertEqual(self.rows()[0]["point"], original["open"][0]["point"])

    def test_failed_atomic_replace_preserves_memory_disk_and_cleans_temp(self):
        self.add()
        original = self.path.read_bytes()
        previous = self.rows()
        with patch.object(subject.os, "replace", side_effect=OSError("readonly file")):
            with self.assertRaises(OSError):
                self.add("新话题写不进去")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.rows(), previous)
        self.assertEqual(self.open().list_threads(A, P, S), previous)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_failed_expiry_prune_preserves_existing_file(self):
        row = self.add()
        original = self.path.read_bytes()
        self.clock[0] = row["expires_at"]
        with patch.object(self.store, "_commit", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.rows()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.store._state["threads"][A][0], row)

    def test_corrupt_or_invalid_file_is_rejected_without_overwrite(self):
        self.add()
        good = json.loads(self.path.read_text(encoding="utf-8"))
        invalid_states = [b"{corrupt file"]
        for change in ({"expires_at": 42}, {"unknown": "field"}, {"awaiting_followup": True}):
            state = deepcopy(good)
            state["threads"][A][0].update(change)
            invalid_states.append(json.dumps(state).encode())
        for raw in invalid_states:
            with self.subTest(raw=raw[:30]):
                self.path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    self.open()
                self.assertEqual(self.path.read_bytes(), raw)

    def test_invalid_operations_never_modify_file(self):
        row = self.add()
        original = self.path.read_bytes()
        for operations in ({"new": []}, {"open": [self.note(topic="x" * 41)]},
                           {"progress": [{"id": row["id"], "point": ""}]},
                           {"resolve": [{"id": "wrong-id"}]},
                           {"mention": [{"id": row["id"], "credential": "unwanted"}]}):
            with self.subTest(operations=operations), self.assertRaises(ValueError):
                self.apply(operations)
            self.assertEqual(self.path.read_bytes(), original)

    def test_legacy_empty_v1_is_read_without_forced_migration(self):
        raw = b'{"version":1,"threads":{}}'
        self.path.write_bytes(raw)
        self.assertEqual(self.open().list_threads(A, P, S), [])
        self.assertEqual(self.path.read_bytes(), raw)

    def test_invalid_snapshot_is_rejected_before_any_write(self):
        row = self.add()
        original = self.path.read_bytes()
        for snapshot in ({"awaiting_ids": [[]]}, {"awaiting_ids": ["invalid"]},
                         {"awaiting_turns": {row["id"]: True}},
                         {"awaiting_turns": {"wrong-id": "turn"}}):
            with self.subTest(snapshot=snapshot), self.assertRaises(ValueError):
                self.apply({}, **snapshot)
            self.assertEqual(self.path.read_bytes(), original)

    def crash(self, after):
        first = self.add()
        original = self.path.read_bytes()
        program = '''
import importlib.util,json,os,sys
spec=importlib.util.spec_from_file_location("child_threads",sys.argv[1])
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
store=module.OpenThreadStore(sys.argv[2],now=lambda:1001.0)
commit=store._commit
def interrupted(state):
    if sys.argv[3]=="after":
        commit(state)
        os._exit(22)
    os._exit(21)
store._commit=interrupted
note={"kind":"deferred","topic":"新的未完话题","point":"等下一次说细节","user_quote":"下次再说","assistant_quote":""}
store.apply("default:GroupMessage:100","alice",'["conversation","persona"]',"crash-turn",{"open":[note]},[])
'''
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", program, str(SOURCE),
                                 str(self.path), "after" if after else "before"],
                                capture_output=True, timeout=15, check=False)
        self.assertEqual(result.returncode, 22 if after else 21,
                         result.stderr.decode("utf-8", errors="replace"))
        reopened = self.open()
        rows = reopened.list_threads(A, P, S)
        if after:
            self.assertEqual({row["topic"] for row in rows}, {first["topic"], "新的未完话题"})
            self.assertFalse(reopened.apply(A, P, S, "crash-turn", {}, []))
        else:
            self.assertEqual(rows, [first])
            self.assertEqual(self.path.read_bytes(), original)

    def test_process_exit_before_commit_preserves_old_file(self):
        self.crash(False)

    def test_process_exit_after_commit_preserves_exactly_one_application(self):
        self.crash(True)


if __name__ == "__main__":
    unittest.main()
