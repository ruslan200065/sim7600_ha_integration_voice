import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, Mock
from aiohttp import web
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from autonomy import Autonomy
from rules import Rules
from family_inbox import FamilyInbox
from event_journal import EventJournal
from escalation import EscalationPolicies
from timeline import Timeline
from intelligence_panel import intelligence

class IntelligencePanelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();root=Path(self.tmp.name)
        self.brain=SimpleNamespace(_visible=lambda entity:entity in {'light.member','sensor.member'},
            _control_actions=lambda entity:{'turn_on','turn_off'} if entity=='light.member' else set(),
            camera_allowed=lambda entity:entity=='camera.member',
            _ha=AsyncMock(return_value=[{'entity_id':'sensor.member','state':'on','attributes':{}},
                                        {'entity_id':'sensor.owner','state':'private','attributes':{}}]))
        self.service=SimpleNamespace(owner_key='owner',family={'phone':{'key':'member','name':'Member'}},brain=self.brain,
            caller_brain=lambda member:self.brain,autonomy=Autonomy(root/'autonomy.db'),test_rules=Rules(root/'rules.db'),
            inbox=FamilyInbox(root/'inbox.db'),events_journal=EventJournal(root/'events.db'),
            escalation=EscalationPolicies(root/'escalation.db'),timeline=Timeline(root/'timeline.db'))
        self.panel=SimpleNamespace(service=self.service,personal_profiles=lambda:{'owner':'Owner','member':'Member'})
    def tearDown(self):self.tmp.cleanup()
    def request(self,profile='member',body=None,method='GET',**query):
        return Mock(query={'profile':profile,**query},method=method,json=AsyncMock(return_value=body))
    async def act(self,action,profile='member',**data):
        return json.loads((await intelligence(self.panel,self.request(profile,{'action':action,**data},'POST'))).text)
    async def test_profile_boundary_messages_and_facts(self):
        self.service.inbox.send('owner','member','private family message')
        self.service.events_journal.record('sensor.owner','private')
        self.service.events_journal.record('sensor.member','22')
        data=json.loads((await intelligence(self.panel,self.request())).text)
        self.assertEqual(len(data['inbox']),1)
        self.assertEqual(data['sent'],[])
        self.assertEqual(data['home_changes']['total'],1)
        self.assertEqual(data['home_changes']['events'][0]['entity_id'],'sensor.member')
        with self.assertRaises(web.HTTPNotFound):await intelligence(self.panel,self.request('stranger'))
        with self.assertRaises(web.HTTPBadRequest):await self.act('send_message',recipient='stranger',text='x')
    async def test_autonomy_and_corrections_scoped(self):
        self.service.autonomy.learn('owner','свет','light.owner',True)
        result=await self.act('set_autonomy',target='light.member',mode='observe')
        self.assertEqual(result['mode'],'observe')
        self.assertEqual(self.service.autonomy.rules('owner'),[])
        self.assertFalse((await self.act('delete_correction',phrase='свет'))['deleted'])
        self.assertTrue((await self.act('remove_rule',target='light.member'))['deleted'])
    async def test_simulation_preview_only_reads_and_checks_permissions(self):
        condition={'kind':'state','entity':'sensor.member','operator':'equals','value':'on'}
        operation={'tool':'call_ha_service','arguments':{'entity_id':'light.member','service':'turn_on'}}
        saved=await self.act('save_simulation',name='Test',condition=condition,operation=operation)
        self.assertTrue(saved['dry_run'])
        result=await self.act('preview_simulation',name='Test')
        self.assertTrue(result['preview']['would_trigger']);self.assertFalse(result['preview']['executed'])
        self.brain._ha.assert_awaited_once_with('GET','/api/states')
        with self.assertRaises(web.HTTPBadRequest):
            await self.act('save_simulation',name='Forbidden',condition={**condition,'entity':'sensor.owner'},operation=operation)
        with self.assertRaises(web.HTTPBadRequest):
            await self.act('save_simulation',name='Wrong recipient',condition=condition,operation=operation,recipient='owner')
        self.assertTrue((await self.act('delete_simulation',name='Test'))['deleted'])
    async def test_escalation_owner_only_known_profiles_and_private_message_cancel(self):
        with self.assertRaises(web.HTTPForbidden):await self.act('set_escalation',source='alarm',chain=['owner','member'])
        with self.assertRaises(web.HTTPBadRequest):await self.act('set_escalation',profile='owner',source='alarm',chain=['owner','unknown'])
        result=await self.act('set_escalation',profile='owner',source='alarm',chain=['owner','member'])
        self.assertTrue(result['enabled'])
        message=await self.act('send_message',profile='owner',recipient='member',text='Hello')
        self.assertFalse((await self.act('cancel_message',id=message['id']))['cancelled'])
        self.assertTrue((await self.act('cancel_message',profile='owner',id=message['id']))['cancelled'])
    async def test_diagnostics_selected_call_and_bad_simulation_input(self):
        self.service.timeline.record('member','1','playback','failed')
        self.service.timeline.record('member','2','playback','sent')
        data=json.loads((await intelligence(self.panel,self.request(call_id='1'))).text)
        self.assertEqual(data['diagnostics']['status'],'failure_observed')
        for operation in ({'tool':'call_ha_service','arguments':{}},{'tool':'shell','arguments':{}},[]):
            with self.subTest(operation=operation),self.assertRaises(web.HTTPBadRequest):
                await self.act('save_simulation',name='Wrong',condition={'kind':'time','when':'2026-12-01T12:00:00+03:00'},operation=operation)

    async def test_latest_call_cannot_inherit_older_call_audio_stages(self):
        self.service.timeline.record('member','old','connection','connected')
        self.service.timeline.record('member','old','playback','sent')
        self.service.timeline.record('member','new','connection','connected')
        self.service.timeline.record('member','room','reply','ready')
        self.service.timeline.record('member','event:alarm','playback','sent')
        data=json.loads((await intelligence(self.panel,self.request())).text)
        self.assertEqual(data['diagnostics_call_id'],'new')
        self.assertEqual(data['diagnostics']['status'],'incomplete')
        playback=next(row for row in data['diagnostics']['stages'] if row['stage']=='playback')
        self.assertEqual(playback['status'],'unknown')
        self.assertEqual([row['id'] for row in data['calls']],['new','old'])
