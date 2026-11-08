from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from notifications import NotificationPolicies, validate_policy


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory()
        self.store=NotificationPolicies(Path(self.tmp.name)/"notifications.sqlite3")
    def tearDown(self):
        self.tmp.cleanup()
    def test_default_call_and_profile_isolation_and_persistence(self):
        self.assertEqual(self.store.get("one")["channel"],"call")
        self.store.set("one",{"channel":"telegram","telegram_target":-10012345})
        self.assertEqual(self.store.get("two")["channel"],"call")
        again=NotificationPolicies(self.store.path)
        self.assertEqual(again.decide("one")["telegram_target"],"-10012345")
        self.store.set("one",{"quiet_start":"23:00","quiet_end":"08:00"})
        self.assertEqual(self.store.get("one")["channel"],"telegram")
    def test_overnight_quiet_hours_boundary_and_urgent_opt_in(self):
        self.store.set("one",{"quiet_start":"23:00","quiet_end":"08:00"})
        tz=ZoneInfo("Europe/Moscow")
        late=datetime(2026,11,8,23,30,tzinfo=tz)
        decision=self.store.decide("one",late,urgent=True)
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["defer_until"],"2026-11-09T08:00:00+03:00")
        self.store.set("one",{"urgent_bypass":True})
        self.assertTrue(self.store.decide("one",late,urgent=True)["allowed"])
        self.assertFalse(self.store.decide("one",late)["allowed"])
        self.assertTrue(self.store.decide("one",late.replace(hour=8,minute=0))["allowed"])
        self.assertFalse(self.store.decide("one",late.replace(hour=7))["allowed"])
    def test_same_day_quiet_and_repeated_dst_hour(self):
        self.store.set("one",{"quiet_start":"12:00","quiet_end":"14:00"})
        now=datetime(2026,11,8,10,30,tzinfo=timezone.utc)
        self.assertEqual(self.store.decide("one",now)["defer_until"],"2026-11-08T14:00:00+03:00")
        self.store.set("one",{"timezone":"America/New_York","quiet_start":"00:00","quiet_end":"01:45"})
        now=datetime(2026,11,1,1,30,tzinfo=ZoneInfo("America/New_York"),fold=1)
        until=datetime.fromisoformat(self.store.decide("one",now)["defer_until"])
        self.assertGreater(until.timestamp(),now.timestamp())
        self.assertEqual(until.utcoffset(),timedelta(hours=-5))
    def test_policy_validation_and_disable_quiet(self):
        for changes in ({"channel":"email"},{"channel":[]},{"quiet_start":"25:00","quiet_end":"08:00"},
                        {"quiet_start":"23:00"},{"quiet_start":"08:00","quiet_end":"08:00"},
                        {"telegram_target":"secret / ../"},{"channel":"telegram"},{"urgent_bypass":"yes"},
                        {"timezone":"Bogus/Zone"},{"api_key":"hidden"}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                validate_policy(changes)
        policy=validate_policy({"quiet_start":"","quiet_end":""})
        self.assertEqual(policy["quiet_end"],"")
    def test_event_coalescing_fixed_window_scoped_by_recipient_and_source(self):
        now=datetime(2026,11,8,10,tzinfo=timezone.utc)
        first=self.store.coalesce("one","camera.yard",{"event":"motion"},now)
        next_event=self.store.coalesce("one","camera.yard",{"event":"person"},now+timedelta(seconds=59))
        self.assertEqual(first["id"],next_event["id"])
        self.assertEqual(next_event["count"],2)
        self.assertEqual(next_event["context"],{"event":"person"})
        self.assertFalse(next_event["notify"])
        renewed=self.store.coalesce("one","camera.yard",{},now+timedelta(seconds=60))
        self.assertNotEqual(first["id"],renewed["id"])
        self.assertTrue(renewed["notify"])
        self.assertTrue(self.store.coalesce("two","camera.yard",{},now)["notify"])
        self.assertTrue(self.store.coalesce("one","camera.room",{},now)["notify"])
    def test_invalid_episode_and_naive_clock_rejected(self):
        with self.assertRaises(ValueError):self.store.decide("one",datetime(2026,11,8))
        with self.assertRaises(ValueError):self.store.coalesce("one","",{})
        with self.assertRaises(ValueError):self.store.coalesce("one","camera.room",{},window_seconds=0)
        with self.assertRaises(ValueError):self.store.coalesce("one","camera.room",{"huge":"x"*9000})
