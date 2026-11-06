import asyncio
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


class SpeechCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_reuses_complete_pcm(self):
        calls = []

        async def synthesize(text):
            calls.append(text)
            yield b"pcm"

        with TemporaryDirectory() as directory:
            cache = SpeechCache(directory)
            for _ in range(2):
                result = [chunk async for chunk in cache.stream("hello", "denis", "neutral", synthesize)]
                self.assertEqual(result, [b"pcm"])
        self.assertEqual(calls, ["hello"])
