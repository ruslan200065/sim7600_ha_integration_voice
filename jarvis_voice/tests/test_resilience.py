import array
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from resilience import (OpenAICompatibleSpeech, SpeechRouter, fallback_allowed,
                        fallback_profiles, pcm24_to_16, validate_speech_fallback)
from call_diagnostics import analyze_timeline


class Response:
    status=200
    def __init__(self,data=None,raw=b''):
        self.data=data
        self.raw=raw
        self.content=self
    async def __aenter__(self):return self
    async def __aexit__(self,*args):return False
    async def json(self):return self.data
    async def iter_chunked(self,_size):
        yield self.raw


class ResilienceTests(unittest.IsolatedAsyncioTestCase):
    def config(self):
        return {'base_url':'http://speech.local/v1','api_key':'test',
                'stt_model':'transcribe','tts_model':'speak','voice':'friendly'}
    def test_fallback_profiles_keep_independent_keys_and_do_not_select_random_provider(self):
        options={'deepseek_api_key':'primary','llm_fallback_ids':['backup','backup','missing'],
                 'llm_profiles':[{'id':'backup','name':'Backup','base_url':'http://llm.local/v1',
                                  'api_key':'secondary','model':'demo','effort':'off'},
                                 {'id':'unused','model':'unused','api_key':'unused'}]}
        profiles=fallback_profiles(options)
        self.assertEqual([p['id'] for p in profiles],['deepseek','backup'])
        self.assertEqual(profiles[1]['api_key'],'secondary')
        self.assertFalse(fallback_allowed(True,False));self.assertFalse(fallback_allowed(False,True))
        self.assertTrue(fallback_allowed())
    def test_external_plain_http_and_incomplete_speech_config_rejected(self):
        with self.assertRaises(ValueError):validate_speech_fallback({**self.config(),'base_url':'http://example.com/v1'})
        with self.assertRaises(ValueError):validate_speech_fallback({**self.config(),'voice':''})
        with self.assertRaises(ValueError):validate_speech_fallback({**self.config(),'unknown':'secret'})
    def test_resample_has_correct_duration_and_preserves_sample_range(self):
        source=array.array('h',[1000,-1000,1000]*240)
        raw=pcm24_to_16(source.tobytes())
        self.assertEqual(len(raw),len(source)*2*2//3)
        output=array.array('h');output.frombytes(raw)
        self.assertLessEqual(max(abs(v) for v in output),1000)
        with self.assertRaises(ValueError):pcm24_to_16(b'x')
    async def test_tts_fallback_only_before_any_audio(self):
        called=[]
        class Primary:
            async def synthesize(self,_text):
                raise RuntimeError('unavailable')
                yield b''
            close=AsyncMock()
        class Backup:
            async def synthesize(self,text):
                called.append(text);yield b'backup'
            close=AsyncMock()
        router=SpeechRouter(Primary(),Backup())
        self.assertEqual([chunk async for chunk in router.synthesize('hello')],[b'backup'])
        class PartialPrimary:
            async def synthesize(self,_text):
                yield b'first'
                raise RuntimeError('lost stream')
        router=SpeechRouter(PartialPrimary(),Backup())
        chunks=[]
        with self.assertRaises(RuntimeError):
            async for chunk in router.synthesize('again'):chunks.append(chunk)
        self.assertEqual(chunks,[b'first']);self.assertEqual(called,['hello'])
    async def test_live_stt_replays_captured_pcm_without_repeating_primary(self):
        pcm=array.array('h',[2000]*1600).tobytes()
        queue=asyncio.Queue();queue.put_nowait(pcm)
        class Primary:
            async def recognize_live(self,source,*args,**kwargs):
                await source.get();raise RuntimeError('offline')
        backup=SimpleNamespace(recognize=AsyncMock(return_value='Проверка'))
        router=SpeechRouter(Primary(),backup)
        async with asyncio.timeout(2):
            self.assertEqual(await router.recognize_live(queue),'Проверка')
        self.assertEqual(backup.recognize.await_args.args[0],pcm)
    async def test_cancellation_never_switches_provider(self):
        class Primary:
            async def recognize(self,_pcm):raise asyncio.CancelledError()
        backup=SimpleNamespace(recognize=AsyncMock())
        router=SpeechRouter(Primary(),backup)
        with self.assertRaises(asyncio.CancelledError):await router.recognize(b'data')
        backup.recognize.assert_not_awaited()
    async def test_compatible_http_audio_contract_and_shared_session(self):
        requests=[]
        class Http:
            def post(self,url,**kwargs):
                requests.append((url,kwargs))
                if url.endswith('transcriptions'):return Response({'text':' test '})
                return Response(raw=array.array('h',[1000]*240).tobytes())
        speech=OpenAICompatibleSpeech(Http(),self.config())
        self.assertEqual(await speech.recognize(b'\x00\x00'*160),'test')
        pcm=b''.join([chunk async for chunk in speech.synthesize('Hello')])
        self.assertEqual(len(pcm),320)
        self.assertEqual(requests[1][1]['json']['response_format'],'pcm')
        self.assertFalse(requests[0][1]['allow_redirects'])
        await speech.close()
    def test_diagnostics_never_claim_audibility_and_points_to_actual_failure(self):
        rows=[{'id':1,'call_id':'one','stage':'audio_rx','status':'ready','at':'1'},
              {'id':2,'call_id':'one','stage':'tts','status':'failed','at':'2'},
              {'id':3,'call_id':'other','stage':'playback','status':'sent','at':'3'}]
        result=analyze_timeline(rows,'one')
        self.assertEqual(result['failure']['stage'],'tts')
        self.assertFalse(result['audibility_confirmed'])
        self.assertEqual(analyze_timeline([rows[-1]],'other')['status'],'audio_sent')
        self.assertEqual(analyze_timeline([], 'none')['status'],'no_data')

    def test_diagnostics_distinguishes_recovered_from_unresolved_failure(self):
        rows=[{'id':1,'call_id':'one','stage':'tts','status':'failed','at':'1'},
              {'id':2,'call_id':'one','stage':'tts','status':'ready','at':'2'},
              {'id':3,'call_id':'one','stage':'playback','status':'sent','at':'3'}]
        result=analyze_timeline(rows,'one')
        self.assertEqual(result['status'],'recovered')
        self.assertIsNone(result['failure'])
        self.assertEqual(result['past_failures'][0]['stage'],'tts')
