from datetime import datetime,timedelta,timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from timeline import Timeline


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory()
        self.store=Timeline(Path(self.tmp.name)/"timeline.sqlite3")
    def tearDown(self):self.tmp.cleanup()
    def test_profile_call_filter_order_and_pagination(self):
        self.store.record("one",1,"stt","recognized",23,{"text":"hello"})
        self.store.record("one",1,"llm","answered",100,{"text":"hi"})
        self.store.record("one",2,"tool","accepted",20,{"entity_id":"light.kitchen"})
        self.store.record("two",1,"stt","recognized",2,{"text":"private"})
        page=self.store.list("one",limit=2)
        self.assertEqual(page["total"],3)
        self.assertEqual(page["next_offset"],2)
        self.assertEqual(page["items"][0]["call_id"],"2")
        tail=self.store.list("one",limit=2,offset=2)
        self.assertEqual(len(tail["items"]),1)
        self.assertIsNone(tail["next_offset"])
        calls=self.store.list("one",call_id=1)
        self.assertEqual(calls["total"],2)
        self.assertEqual(calls["items"][0]["duration_ms"],100)
        self.assertEqual(self.store.list("three")["items"],[])
    def test_secret_fields_and_dtmf_never_stored(self):
        self.store.record("one",1,"tool","accepted",details={"api_key":"abc","nested":{"pin":"1234"},"text":"normal"})
        self.store.record("one",1,"dtmf_received","input",details={"digit":"1"})
        rows=self.store.list("one")["items"]
        self.assertEqual(rows[0]["details"],{})
        self.assertEqual(rows[1]["details"]["api_key"],"[скрыто]")
        self.assertEqual(rows[1]["details"]["nested"]["pin"],"[скрыто]")
        self.assertEqual(rows[1]["details"]["text"],"normal")
    def test_old_trace_retention_and_invalid_duration(self):
        now=datetime(2026,11,8,tzinfo=timezone.utc)
        self.store.record("one",1,"call","started",at=now-timedelta(days=31))
        self.store.record("one",2,"call","started",at=now)
        self.assertEqual(self.store.list("one")["total"],1)
        for duration in (-1,float('nan'),True):
            with self.assertRaises(ValueError):self.store.record("one",1,"call","ok",duration)
        with self.assertRaises(ValueError):self.store.list("one",limit=0)
        with self.assertRaises(ValueError):self.store.record("",1,"call","ok")
