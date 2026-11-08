from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from family_inbox import FamilyInbox
from event_journal import EventJournal
from escalation import EscalationPolicies

class FamilyEventsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.path=Path(self.tmp.name)
        self.now=datetime(2026,11,8,12,tzinfo=timezone.utc)
    def tearDown(self):self.tmp.cleanup()
    def test_message_visible_only_recipient_and_playback_ack_scoped(self):
        inbox=FamilyInbox(self.path/'inbox.db')
        item=inbox.send('dad','mom','Купи хлеб',now=self.now)
        self.assertEqual(inbox.pending('dad',self.now),[])
        self.assertEqual(inbox.pending('child',self.now),[])
        self.assertEqual(inbox.pending('mom',self.now)[0]['text'],'Купи хлеб')
        self.assertFalse(inbox.delivered('child',item['id'],self.now))
        # Merely reading/listing is not delivery.
        self.assertEqual(inbox.sent('dad',self.now)[0]['status'],'pending')
        self.assertTrue(inbox.delivered('mom',item['id'],self.now))
        self.assertFalse(inbox.delivered('mom',item['id'],self.now))
        self.assertEqual(inbox.pending('mom',self.now),[])
        self.assertEqual(inbox.sent('dad',self.now)[0]['status'],'delivered')
    def test_message_expiry_cancel_persistence_and_anonymous_denied(self):
        path=self.path/'inbox.db';inbox=FamilyInbox(path)
        item=inbox.send('dad','mom','Скоропортящееся',self.now+timedelta(minutes=1),self.now)
        self.assertFalse(inbox.cancel('child',item['id']))
        self.assertEqual(FamilyInbox(path).pending('mom',self.now)[0]['id'],item['id'])
        later=self.now+timedelta(minutes=2)
        self.assertEqual(inbox.pending('mom',later),[])
        self.assertFalse(inbox.delivered('mom',item['id'],later))
        self.assertEqual(inbox.sent('dad',later)[0]['status'],'expired')
        second=inbox.send('dad','mom','Отмена',now=self.now)
        self.assertTrue(inbox.cancel('dad',second['id']))
        self.assertEqual(inbox.pending('mom',self.now)[0]['id'],item['id'])
        with self.assertRaises(ValueError):inbox.send('room','mom','x',now=self.now)
        with self.assertRaises(ValueError):inbox.send('dad','mom','x',self.now,self.now)
    def test_event_history_filters_before_pagination_and_keeps_facts(self):
        journal=EventJournal(self.path/'events.db')
        for index in range(5):
            journal.record('sensor.allowed',str(index),'ha',self.now+timedelta(seconds=index),previous=str(index-1),
                           attributes={'unit_of_measurement':'°C','api_key':'private'})
            journal.record('sensor.private','secret','ha',self.now+timedelta(seconds=index))
        page=journal.list('mom',lambda entity:entity=='sensor.allowed',limit=2)
        self.assertEqual(page['total'],5);self.assertEqual(page['next_offset'],2)
        second=journal.list('mom',lambda entity:entity=='sensor.allowed',limit=2,offset=2)
        self.assertFalse({e['id'] for e in page['events']}&{e['id'] for e in second['events']})
        self.assertTrue(all(e['factual'] and e['source']=='ha' for e in page['events']))
        self.assertNotIn('api_key',page['events'][0]['attributes'])
        self.assertEqual(journal.list('child',lambda entity:False)['total'],0)
        self.assertEqual(journal.list('mom',lambda entity:True,since=self.now+timedelta(seconds=3))['total'],4)
    def test_private_reminder_never_escalates_even_if_source_has_policy(self):
        policies=EscalationPolicies(self.path/'escalation.db')
        policies.set('alarm',['dad','mom','child'])
        for flag in (True,None,0,'false'):
            self.assertEqual(policies.advance('private','alarm','dad',is_private=flag)['reason'],'private_reminder')
        self.assertEqual(policies.advance('public','alarm','dad',is_private=False)['recipient'],'mom')
        self.assertFalse(policies.advance('public','alarm','dad',is_private=False)['escalate'])
        self.assertEqual(policies.advance('public','alarm','mom',is_private=False)['recipient'],'child')
        self.assertEqual(policies.advance('public','alarm','child',is_private=False)['reason'],'exhausted')
    def test_escalation_persists_snapshot_bounds_attempts_and_rejects_wrong_source(self):
        path=self.path/'escalation.db';policies=EscalationPolicies(path)
        policies.set('alarm',['dad','mom','child'],max_attempts=1)
        self.assertFalse(policies.advance('evt','alarm','child',False)['escalate'])
        self.assertEqual(policies.advance('evt','alarm','dad',False)['recipient'],'mom')
        policies.set('alarm',['dad','other'])
        again=EscalationPolicies(path)
        self.assertEqual(again.advance('evt','changed','mom',False)['reason'],'event_source_mismatch')
        self.assertEqual(again.advance('evt','alarm','mom',False)['reason'],'exhausted')
        self.assertEqual(again.get('missing')['enabled'],False)
        for chain in (['dad','dad'],[['bad']],['room','dad'],['dad']):
            with self.assertRaises(ValueError):policies.set('alarm',chain)
