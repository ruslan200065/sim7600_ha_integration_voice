import sys
from pathlib import Path
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from autonomy import Autonomy

class AutonomyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Autonomy(Path(self.tmp.name)/'autonomy.sqlite3')
    def tearDown(self):self.tmp.cleanup()
    def test_only_explicitly_confirmed_correction_is_saved_and_private(self):
        self.assertFalse(self.store.learn('one','Включи большой свет!','light.hall')['saved'])
        self.assertEqual(self.store.resolve('one','включи большой свет'),'')
        self.store.learn('one','Включи большой свет!','light.hall',confirmed=True)
        self.assertEqual(self.store.resolve('one','ВКЛЮЧИ большой свет'),'light.hall')
        self.assertEqual(self.store.resolve('two','включи большой свет'),'')
        self.assertFalse(self.store.forget('two','включи большой свет'))
        self.assertTrue(self.store.forget('one','включи большой свет'))
    def test_default_and_per_device_override_domain(self):
        self.assertEqual(self.store.evaluate('one','light.hall','turn_on',{})['decision'],'default')
        self.store.set_rule('one','light','confirm')
        self.assertEqual(self.store.evaluate('one','light.hall','turn_on',{})['decision'],'confirm')
        self.store.set_rule('one','light.hall','allow')
        self.assertEqual(self.store.evaluate('one','light.hall','turn_on',{})['decision'],'allow')
        self.assertEqual(self.store.evaluate('two','light.hall','turn_on',{})['decision'],'default')
        self.store.remove_rule('one','light.hall')
        self.assertEqual(self.store.evaluate('one','light.hall','turn_on',{})['decision'],'confirm')
    def test_observe_and_numeric_bounds_cannot_be_bypassed(self):
        self.store.set_rule('one','climate','allow',{'temperature':{'min':18,'max':24}})
        self.assertEqual(self.store.evaluate('one','climate.room','set_temperature',{'temperature':22})['decision'],'allow')
        for value in (25,True,float('nan'),'22'):
            self.assertEqual(self.store.evaluate('one','climate.room','set_temperature',{'temperature':value})['decision'],'deny')
        self.store.set_rule('one','climate.room','observe')
        self.assertEqual(self.store.evaluate('one','climate.room','turn_off',{})['decision'],'deny')
    def test_persistence_and_validation(self):
        self.store.set_rule('one','switch','confirm')
        self.assertEqual(Autonomy(self.store.path).rules('one')[0]['mode'],'confirm')
        for values in ({'min':24,'max':18},{'min':True},{'max':float('inf')}):
            with self.assertRaises(ValueError):self.store.set_rule('one','climate','allow',{'temperature':values})
        with self.assertRaises(ValueError):self.store.set_rule('one','light','sometimes')
