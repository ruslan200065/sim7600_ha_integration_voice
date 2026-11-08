import os,sys,tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
with patch.dict(sys.modules,{'speech':SimpleNamespace(SpeechKit=object)}):
 from main import Server
 import main as server_module
from brain import Brain
from setup_panel import validate_changes

class Response:
 content_type='application/json'
 def __init__(self,status):self.status=status
 async def __aenter__(self):return self
 async def __aexit__(self,*args):return False
 async def json(self):return {'choices':[{'message':{'role':'assistant','content':'Резерв ответил'}}]}

class V070IntegrationTests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)
 def tearDown(self):self.tmp.cleanup()
 def options(self):return {'llm_active':'a','llm_fallback_ids':['b'],'llm_profiles':[{'id':i,'name':i,'base_url':'https://'+i+'.test/v1','api_key':'test','model':'model','effort':'off'} for i in ('a','b')]}
 def service(self):
  with patch.dict(os.environ,{'OPTIONS_PATH':str(self.path/'options.json')}):
   return Server({'api_token':'test','allowed_number':'+12025550101','family_members':['Мама | +12025550102']})
 async def test_model_fallback_never_replays_executed_tools(self):
  calls=[]
  class Http:
   def post(self,url,**kwargs):calls.append(url);return Response(500 if 'a.test' in url else 200)
  b=Brain(self.options(),Http(),None)
  profile=self.options()['llm_profiles'][0]
  data,chosen=await b._model_request(profile,[],[],None,False)
  self.assertEqual(chosen['id'],'b');self.assertEqual(len(calls),2)
  calls.clear()
  with self.assertRaises(RuntimeError):await b._model_request(profile,[],[],None,True)
  self.assertEqual(len(calls),1)
 async def test_private_reminder_never_escalates(self):
  s=self.service();owner=s.owner_key;member=next(iter(s.family.values()))['key']
  s.escalation.set('motion.yard',[owner,member])
  from datetime import datetime,timezone
  private=s.personal.reminder(owner,'Owner','private',datetime.now(timezone.utc))
  s.personal.query("UPDATE reminders SET delivered=1,delivery_status='undelivered' WHERE id=?",(private,))
  await s.escalate_undelivered_events()
  self.assertEqual(len(s.personal.dashboard(member)['reminders']),0)
 async def test_shared_event_can_escalate_only_once(self):
  s=self.service();owner=s.owner_key;member=next(iter(s.family.values()))['key']
  s.escalation.set('motion.yard',[owner,member]);s.caller_brain=lambda _:SimpleNamespace(camera_allowed=lambda camera:False)
  from datetime import datetime,timezone
  identifier=s.personal.reminder(owner,'Owner','shared',datetime.now(timezone.utc),{'kind':'time','source':'motion.yard','notification_event':True,'notification_shared':True,'event_id':'e1'})
  s.personal.query("UPDATE reminders SET delivered=1,delivery_status='undelivered' WHERE id=?",(identifier,))
  await s.escalate_undelivered_events();await s.escalate_undelivered_events()
  self.assertEqual(len(s.personal.dashboard(member)['reminders']),1)
 async def test_inbox_is_acknowledged_only_after_complete_playback(self):
  s=self.service();profile=s.owner_key;s.modem.active_number=s.o['allowed_number'];s.modem.call_state='connected'
  s.start_call_audio=AsyncMock();s.audio.stop=AsyncMock();s._say=AsyncMock()
  from datetime import datetime,timedelta,timezone
  message=s.inbox.send(next(iter(s.family.values()))['key'],profile,'Семейное сообщение',datetime.now(timezone.utc)+timedelta(days=1))
  async def reply(_phrase,*,phone_actions,**kwargs):return await phone_actions['read_family_message']({'id':message['id']})
  s.caller_brain=lambda _:SimpleNamespace(profile=profile,reply=reply)
  async def dialog(_speech,reply,_source,play,*args,**kwargs):
   result=await reply('Озвучь',on_progress=lambda text:None)
   self.assertEqual(len(s.inbox.pending(profile)),1)
   await play(result.text)
  with patch.object(server_module,'run_dialog',dialog):await s._dialog('',None)
  self.assertEqual(s.inbox.pending(profile),[])
 def test_new_secret_not_forwarded_on_url_change(self):
  current={'speech_fallback_api_key':'private','speech_fallback_base_url':'https://old.test/v1'}
  with self.assertRaises(ValueError):validate_changes({'speech_fallback_base_url':'https://new.test/v1'},current)

 async def test_stt_failure_is_reported_to_call_diagnostics(self):
  import asyncio
  from voice_dialog import run_dialog, CallOutput
  class Speech:
   turn=0
   async def recognize_live(self,*args,**kwargs):
    self.turn+=1
    if self.turn==1:raise RuntimeError('STT unavailable')
    if self.turn==2:return 'До связи'
    await asyncio.Future()
  diagnostic=AsyncMock()
  async with asyncio.timeout(3):
   await run_dialog(Speech(),AsyncMock(return_value=CallOutput('Пока',True)),asyncio.Queue(),AsyncMock(),AsyncMock(),'Привет',on_diagnostic=diagnostic)
  self.assertEqual(diagnostic.await_args_list[0].args[:2],('stt','failed'))
