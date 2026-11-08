from datetime import datetime,timezone
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from rules import Rules

class RulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Rules(Path(self.tmp.name)/'rules.sqlite3')
        self.action={'tool':'call_ha_service','arguments':{'entity_id':'light.hall','service':'turn_on'}}
    def tearDown(self):self.tmp.cleanup()
    def test_preview_never_executes_and_does_not_write(self):
        self.store.save('one','Свет',{'kind':'state','entity':'sensor.lux','operator':'below','value':'20'},self.action)
        before=self.store.path.read_bytes()
        result=self.store.preview('one','Свет',{'sensor.lux':{'state':'10'}})
        self.assertTrue(result['would_trigger']);self.assertFalse(result['executed']);self.assertFalse(result['enabled'])
        self.assertEqual(result['recipient'],'one')
        self.assertEqual(before,self.store.path.read_bytes())
        self.assertFalse(self.store.preview('one','Свет',{'sensor.lux':{'state':'30'}})['would_trigger'])
        self.assertFalse(self.store.preview('one','Свет',{})['condition_known'])
    def test_profile_boundary_delete_and_time(self):
        self.store.save('one','Позже',{'kind':'time','when':'2030-01-01T12:00:00+00:00'},self.action,recipient='two')
        self.assertEqual(self.store.list('two'),[])
        with self.assertRaises(ValueError):self.store.preview('two','Позже',{})
        self.assertFalse(self.store.delete('two','Позже'))
        self.assertTrue(self.store.preview('one','Позже',{},datetime(2030,1,1,13,tzinfo=timezone.utc))['would_trigger'])
        self.assertTrue(self.store.delete('one','Позже'))
    def test_invalid_condition_and_executable_payload_rejected(self):
        for condition in ({'kind':'state','entity':'sensor.x','operator':'script','value':'1'},
                          {'kind':'time','when':'2030-01-01T00:00:00'},
                          {'kind':'state','entity':'sensor.x','operator':'above','value':'nan'}):
            with self.assertRaises(ValueError):self.store.save('one','Bad',condition,self.action)
        with self.assertRaises(ValueError):self.store.save('one','Bad',{'kind':'time','when':'2030-01-01T00:00:00+00:00'},{'tool':'shell','arguments':{}})
