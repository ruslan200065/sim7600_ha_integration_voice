import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
with patch.dict(sys.modules,{'speech':SimpleNamespace(SpeechKit=object)}):
    from main import Server
from brain import Brain, TOOLS
from personal import Personal
from knowledge import Knowledge
from notifications import NotificationPolicies

class FeaturesIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def brain(self,profile='first'):
        return Brain({'control_all_entities':True,'observe_all_entities':True},None,None,
            personal=Personal(self.path/'personal.sqlite3'),knowledge=Knowledge(self.path/'knowledge.sqlite3'),
            policies=NotificationPolicies(self.path/'notify.sqlite3'),profile=profile)
    async def test_changed_timestamp_is_not_proof_action_succeeded(self):
        b=self.brain()
        b._ha=AsyncMock(return_value={'entity_id':'light.test','state':'off','last_updated':'new','attributes':{}})
        with patch('brain.STATE_SETTLE_DELAY',0):
            result=await b._tool('call_ha_service',{'entity_id':'light.test','service':'turn_on'})
        self.assertTrue(result['accepted']);self.assertFalse(result['verified'])
        self.assertEqual(sum(c.args[0]=='POST' for c in b._ha.await_args_list),1)
    async def test_alias_room_and_memory_metadata_integrated(self):
        b=self.brain();b.knowledge.set_entity('sensor.test',['Мамин термометр'],'Кабинет','Температура у окна')
        b._ha=AsyncMock(return_value=[{'entity_id':'sensor.test','state':'22','attributes':{'unit_of_measurement':'°C'}}])
        result=await b._find_devices('мамин',room='Кабинет')
        self.assertEqual(result['total'],1)
        await b._tool('remember',{'text':'Люблю прохладу','kind':'preference'})
        self.assertEqual((await b._tool('list_memories',{}))['metadata'][0]['kind'],'preference')
        self.assertEqual((await self.brain('second')._tool('list_memories',{}))['notes'],[])
        await b._tool('forget',{'text':'Люблю прохладу'})
        self.assertEqual(b.knowledge.memories('first'),[])
    async def test_scenario_tools_reuse_permissions_and_are_private(self):
        b=self.brain();steps=[{'tool':'get_entity_state','arguments':{'entity_id':'sensor.test'}}]
        self.assertTrue((await b._tool('save_scenario',{'name':'Проверка','steps':steps}))['saved'])
        b._ha=AsyncMock(return_value={'entity_id':'sensor.test','state':'22','attributes':{}})
        result=await b._tool('run_scenario',{'name':'Проверка'})
        self.assertEqual(result['status'],'completed')
        self.assertEqual((await self.brain('second')._tool('list_scenarios',{}))['scenarios'],[])
    async def test_event_calls_are_queued_and_coalesced_without_real_call(self):
        with patch.dict(os.environ,{'OPTIONS_PATH':str(self.path/'options.json')}):
            s=Server({'api_token':'test','allowed_number':'+12025550101'})
        s.brain=self.brain(s.owner_key);s.modem.call_state='idle';s.dial_number=AsyncMock()
        data={'text':'Движение во дворе','source':'motion.yard'}
        request=SimpleNamespace(json=AsyncMock(return_value=data))
        first=json.loads((await s.notify_event(request)).text)
        second=json.loads((await s.notify_event(request)).text)
        self.assertTrue(first['queued']);self.assertTrue(second['grouped'])
        s.dial_number.assert_not_awaited()
        self.assertEqual(s.personal.count_pending(s.owner_key),1)
    def test_step_schema_has_items(self):
        schema=next(t['function']['parameters'] for t in TOOLS if t['function']['name']=='save_scenario')
        self.assertIn('items',schema['properties']['steps'])

    async def test_saved_scenario_cannot_reuse_confirmation(self):
        b=self.brain()
        result=await b._tool('save_scenario',{'name':'Свет','steps':[{'tool':'call_ha_service','arguments':{'entity_id':'light.test','service':'turn_on','confirmed':True}}]})
        self.assertTrue(result['saved'])
        stored=b.features.workflows.scenario('Свет')
        self.assertNotIn('confirmed',stored[0]['arguments'])
