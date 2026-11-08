"""Integrated LLM smart tools keep confirmation, preview and family boundaries."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from autonomy import Autonomy
from brain import Brain
from personal import Personal
from rules import Rules


class InboxFake:
    def __init__(self):
        self.rows=[]
    def send(self,sender,recipient,text,expiry):
        row={'id':str(len(self.rows)+1),'sender':sender,'recipient':recipient,'text':text,'expires_at':expiry.isoformat()}
        self.rows.append(row)
        return {'saved':True,'id':row['id']}
    def sent(self,sender):
        return [row.copy() for row in self.rows if row['sender']==sender]
    def cancel(self,sender,identifier):
        row=next((row for row in self.rows if row['sender']==sender and row['id']==identifier),None)
        if row is None:return False
        self.rows.remove(row)
        return True


class SmartFeatureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)
        self.inbox=InboxFake()
    def tearDown(self):self.tmp.cleanup()
    def brain(self,profile='one'):
        b=Brain({'allowed_entities':['light.room','climate.room'],'observed_entities':['sensor.lux'],
                 'allow_domains':['light','climate'],'confirm_risky_actions':False},None,None,
                profile=profile,owner=False,personal=Personal(self.path/'personal.sqlite3'),
                autonomy=Autonomy(self.path/'autonomy.sqlite3'),test_rules=Rules(self.path/'rules.sqlite3'),
                inbox=self.inbox,events=SimpleNamespace(),contacts={'one':'Первый','two':'Второй'})
        return b
    async def test_explicit_correction_needs_previous_turn_not_llm_self_confirmation(self):
        b=self.brain();b.turn=1
        args={'phrase':'Большой свет!','entity_id':'light.room','confirmed':True}
        self.assertTrue((await b._tool('learn_device_correction',args))['needs_confirmation'])
        self.assertTrue((await b._tool('learn_device_correction',args))['needs_confirmation'])
        self.assertEqual(b.autonomy.corrections('one'),[])
        b.turn=2
        self.assertTrue((await b._tool('learn_device_correction',args))['saved'])
        self.assertEqual(b.autonomy.resolve('one','большой свет'),'light.room')
        self.assertEqual(b.autonomy.resolve('two','большой свет'),'')
        b.autonomy.learn('one','Скрытый свет','light.secret',True)
        rules=await b._tool('get_autonomy_rules',{})
        self.assertEqual(len(rules['corrections']),1)
    async def test_observe_bounds_and_confirm_enforced_before_post(self):
        b=self.brain();b._ha=AsyncMock(return_value={'entity_id':'light.room','state':'on','attributes':{}})
        b.autonomy.set_rule('one','light.room','observe')
        result=await b._tool('call_ha_service',{'entity_id':'light.room','service':'turn_off'})
        self.assertFalse(result['accepted'])
        self.assertFalse(any(call.args[0]=='POST' for call in b._ha.await_args_list))
        b.autonomy.set_rule('one','climate.room','allow',{'temperature':{'min':18,'max':24}})
        result=await b._tool('call_ha_service',{'entity_id':'climate.room','service':'set_temperature','temperature':30})
        self.assertFalse(result['accepted'])
        self.assertFalse(any(call.args[0]=='POST' for call in b._ha.await_args_list))
        b.autonomy.set_rule('one','light.room','confirm');b.turn=1
        args={'entity_id':'light.room','service':'turn_on','confirmed':True}
        self.assertTrue((await b._tool('call_ha_service',args))['needs_confirmation'])
        self.assertFalse(any(call.args[0]=='POST' for call in b._ha.await_args_list))
        b.turn=2
        self.assertTrue((await b._tool('call_ha_service',args))['accepted'])
        self.assertEqual(sum(call.args[0]=='POST' for call in b._ha.await_args_list),1)
    async def test_simulation_normalizes_old_and_canonical_shapes_without_post(self):
        b=self.brain();b._ha=AsyncMock(return_value=[{'entity_id':'sensor.lux','state':'10','attributes':{}}])
        condition={'kind':'state','entity':'sensor.lux','operator':'below','value':'20'}
        flat={'entity_id':'light.room','service':'turn_on'}
        for name,action in [('Flat',flat),('Canonical',{'tool':'call_ha_service','arguments':flat})]:
            result=await b._tool('save_test_rule',{'name':name,'condition':condition,'action':action})
            self.assertTrue(result['would_trigger']);self.assertFalse(result['executed'])
            self.assertEqual(result['recipient'],'one')
        self.assertTrue(all(call.args[0]=='GET' for call in b._ha.await_args_list))
        b.autonomy.set_rule('one','light.room','observe')
        result=await b._tool('preview_test_rule',{'name':'Canonical'})
        self.assertTrue(result['would_trigger']);self.assertFalse(result['would_execute'])
    async def test_preview_and_listing_hide_revoked_or_private_sources(self):
        b=self.brain();b._ha=AsyncMock(return_value=[])
        args={'name':'Secret','condition':{'kind':'state','entity':'sensor.private','operator':'equals','value':'on'},
              'action':{'tool':'call_ha_service','arguments':{'entity_id':'light.room','service':'turn_on'}}}
        self.assertIn('error',await b._tool('save_test_rule',args));b._ha.assert_not_awaited()
        b.test_rules.save('one',args['name'],args['condition'],args['action'])
        self.assertEqual((await b._tool('list_test_rules',{}))['rules'],[])
        self.assertIn('error',await b._tool('preview_test_rule',{'name':'Secret'}));b._ha.assert_not_awaited()
        args['condition']={'kind':'time','when':'2030-01-01T00:00:00+00:00'}
        args['recipient']='two'
        self.assertIn('error',await b._tool('save_test_rule',args))
    async def test_one_sided_range_and_private_family_messages(self):
        b=self.brain();other=self.brain('two')
        result=await b._tool('set_autonomy_rule',{'target':'climate.room','mode':'allow','parameter':'temperature','maximum':24})
        self.assertEqual(result['ranges'],{'temperature':{'max':24}})
        result=await b._tool('send_family_message',{'recipient':'Второй','text':'Купи хлеб','expires_at':'2035-01-01T00:00:00+00:00'})
        self.assertTrue(result['saved'])
        self.assertEqual((await other._tool('sent_family_messages',{}))['messages'],[])
        self.assertFalse((await other._tool('cancel_family_message',{'id':result['id']}))['cancelled'])
        recipients=await b._tool('message_recipients',{})
        self.assertEqual(recipients['recipients'][0],{'id':'one','name':'Первый'})
        self.assertIn('error',await b._tool('send_family_message',{'recipient':'stranger','text':'secret'}))

    async def test_automation_preconfirmation_cannot_bypass_new_confirm_rule(self):
        b=self.brain()
        b.autonomy.set_rule('one','light.room','confirm')
        b._ha=AsyncMock(return_value={'entity_id':'light.room','state':'off','attributes':{}})
        result=await b._tool('call_ha_service',{'entity_id':'light.room','service':'turn_on'},preconfirmed=True)
        self.assertTrue(result['needs_confirmation'])
        self.assertFalse(any(call.args[0]=='POST' for call in b._ha.await_args_list))
