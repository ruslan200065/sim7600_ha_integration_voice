from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from family import family_profiles
from personal import Personal, condition_matches
from speech_cache import SpeechCache


class PersonalTests(unittest.TestCase):
    def test_old_device_choices_migrate_without_becoming_personal_notes(self):
        import sqlite3
        with TemporaryDirectory() as directory:
            path = Path(directory) / "data.sqlite3"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE preferences (profile TEXT, phrase TEXT, entity TEXT, count INTEGER, PRIMARY KEY(profile,phrase,entity))")
                db.execute("INSERT INTO preferences VALUES ('owner','включи обратно','light.test',2)")
            db.close()
            store = Personal(path)
            self.assertEqual(store.notes("owner"), [])
            self.assertEqual(store.learned("owner", "включи обратно"), "light.test")
            self.assertEqual(len(store.dashboard("owner")["preferences"]), 1)
            self.assertEqual(store.query("SELECT * FROM preferences"), [])
            self.assertEqual(store.learned("other", "включи обратно"), "")

    def test_private_profiles_and_one_shot_delivery(self):
        profiles = family_profiles([
            "Первый | +12025550101 | sensor.test | | | person.first",
            "Второй | +12025550102",
        ])
        first, second = [profile["key"] for profile in profiles.values()]
        with TemporaryDirectory() as directory:
            store = Personal(Path(directory) / "data.sqlite3")
            store.remember(first, "private")
            self.assertEqual(store.notes(second), [])
            store.record(first, "sensor", "sensor.test")
            store.record(first, "sensor", "sensor.test")
            self.assertEqual(store.learned(first, "sensor"), "sensor.test")
            self.assertEqual(store.learned(second, "sensor"), "")
            now = datetime.now(timezone.utc)
            condition = {"kind": "state", "entity": "sensor.test", "operator": "below", "value": "15"}
            reminder = store.reminder(first, "First", "private", condition=condition)
            store.sample(reminder, True, False, now)
            self.assertEqual(store.due(now, [first]), [])
            store.sample(reminder, False, False, now)
            store.sample(reminder, condition_matches(condition, {"sensor.test": {"state": "14"}}), True, now)
            self.assertEqual(store.due(now, [second]), [])
            self.assertFalse(store.cancel(second, reminder))
            self.assertEqual(store.due(now, [first]), [(reminder, first, "private")])
            store.retry_after(reminder, now + timedelta(minutes=15))
            self.assertEqual(store.due(now, [first]), [])
            store.delivered(reminder)
            self.assertEqual(store.due(now + timedelta(hours=1), [first]), [])

    def test_legacy_reminders_migrate_with_delivery_status_and_expiry(self):
        import sqlite3
        from contextlib import closing
        with TemporaryDirectory() as directory:
            path = Path(directory) / "data.sqlite3"
            now = datetime.now(timezone.utc)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE reminders (id TEXT PRIMARY KEY, profile TEXT, name TEXT, text TEXT, due REAL, delivered INTEGER DEFAULT 0, retry_at REAL DEFAULT 0, condition TEXT, armed INTEGER DEFAULT 0)")
                db.execute("INSERT INTO reminders(id,profile,name,text,due,delivered) VALUES ('old','first','First','old',?,1)", (now.timestamp(),))
            store = Personal(path)
            row = store.dashboard("first")["reminders"][0]
            self.assertEqual(row["status"], "delivered")
            self.assertEqual(row["delivery_attempts"], 0)
            self.assertGreater(row["expires_at"], now.timestamp())
            self.assertEqual(store.due(now, ["first"]), [])

    def test_delivery_attempts_are_bounded_persistent_and_recipient_scoped(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "data.sqlite3"
            store = Personal(path)
            now = datetime.now(timezone.utc)
            identifier = store.reminder("first", "First", "private", now)
            self.assertFalse(store.reserve_delivery(identifier, "second", now))
            for attempt in range(3):
                current = now + timedelta(minutes=15 * attempt)
                self.assertEqual(store.due(current, ["first"]), [(identifier, "first", "private")])
                self.assertTrue(store.reserve_delivery(identifier, "first", current))
                self.assertFalse(store.reserve_delivery(identifier, "first", current))
                store = Personal(path)
            self.assertEqual(store.due(now + timedelta(minutes=45), ["first"]), [])
            item = store.dashboard("first")["reminders"][0]
            self.assertEqual(item["status"], "undelivered")
            self.assertEqual(item["delivery_attempts"], 3)
            self.assertTrue(item["completed"])

    def test_expiry_decline_and_defer_do_not_create_repeat_tasks(self):
        with TemporaryDirectory() as directory:
            store = Personal(Path(directory) / "data.sqlite3")
            now = datetime.now(timezone.utc)
            store.reminder("first", "First", "stale", now - timedelta(days=2))
            self.assertEqual(store.due(now, ["first"]), [])
            self.assertEqual(store.dashboard("first")["reminders"][0]["status"], "expired")
            identifier = store.reminder("first", "First", "later", now)
            self.assertTrue(store.reserve_delivery(identifier, "first", now))
            later = now + timedelta(hours=2)
            self.assertFalse(store.defer_reminder("second", identifier, later))
            self.assertTrue(store.defer_reminder("first", identifier, later))
            self.assertEqual(store.due(now + timedelta(minutes=30), ["first"]), [])
            self.assertEqual(store.due(later, ["first"]), [(identifier, "first", "later")])
            self.assertFalse(store.decline_reminder("second", identifier))
            self.assertTrue(store.decline_reminder("first", identifier))
            self.assertEqual(store.due(later, ["first"]), [])
            row = next(r for r in store.dashboard("first")["reminders"] if r["id"] == identifier)
            self.assertEqual(row["status"], "declined")
            store.delivered(identifier)
            self.assertEqual(next(r for r in store.dashboard("first")["reminders"] if r["id"] == identifier)["status"], "declined")

    def test_action_can_only_be_claimed_once_and_never_dials(self):
        with TemporaryDirectory() as directory:
            store = Personal(Path(directory) / "data.sqlite3")
            now = datetime.now(timezone.utc)
            condition = {"kind": "time", "action": {"entity_id": "light.test", "service": "turn_on"}}
            identifier = store.reminder("first", "First", "Action", now, condition)
            self.assertEqual(store.due(now, ["first"]), [])
            self.assertEqual(store.due_actions(now), [(identifier, "first", condition)])
            self.assertTrue(store.claim_action(identifier))
            self.assertFalse(store.claim_action(identifier))
            self.assertEqual(store.due_actions(now), [])
            store.action_status(identifier, "completed")
            self.assertEqual(store.action_history("first"), [{"id": identifier, "text": "Action", "status": "completed"}])

    def test_dashboard_edits_are_scoped_and_finished_rules_are_immutable(self):
        with TemporaryDirectory() as directory:
            store = Personal(Path(directory) / "data.sqlite3")
            store.remember("first", "draft")
            note = store.dashboard("first")["notes"][0]
            self.assertFalse(store.edit_note("second", note["id"], "foreign"))
            self.assertTrue(store.edit_note("first", note["id"], "updated"))
            now = datetime.now(timezone.utc)
            identifier = store.reminder("first", "First", "original", now)
            self.assertTrue(store.edit_reminder("first", identifier, "changed", now.timestamp() + 60, None))
            self.assertEqual(store.dashboard("first")["reminders"][0]["text"], "changed")
            store.delivered(identifier)
            self.assertFalse(store.edit_reminder("first", identifier, "different", None, None))
            self.assertTrue(store.remove_record("first", "reminders", identifier))


class SpeechCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_reuses_complete_pcm(self):
        calls = []

        pcm = bytes(range(256)) * 25  # 6400 bytes, 0.2 s of 16 kHz audio

        async def synthesize(text):
            calls.append(text)
            yield pcm

        with TemporaryDirectory() as directory:
            cache = SpeechCache(directory)
            for _ in range(2):
                result = [chunk async for chunk in cache.stream("hello", "denis", "neutral", synthesize)]
                self.assertEqual(b"".join(result), pcm)
        self.assertEqual(calls, ["hello"])

    async def test_truncated_stream_is_not_cached(self):
        calls = []

        async def synthesize(text):
            calls.append(text)
            yield b"pcm"

        with TemporaryDirectory() as directory:
            cache = SpeechCache(directory)
            for _ in range(2):
                result = [chunk async for chunk in cache.stream("hello world", "denis", "neutral", synthesize)]
                self.assertEqual(result, [b"pcm"])
        self.assertEqual(calls, ["hello world", "hello world"])
