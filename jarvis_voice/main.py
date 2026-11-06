"""JARVIS Voice Bridge: HA add-on HTTP API, SIM7600 call management, SpeechKit and DeepSeek."""
import asyncio
import json
import logging
import os
import secrets
import socket
from pathlib import Path
from aiohttp import web, ClientSession

from modem import Modem
from audio import PCMTransport
from speech import SpeechKit
from brain import Brain
from q1_audio import recognize_microphone_live, play_stream
from q1_protocol import gps_status, gps_start, led_set
from q1_hardware import diagnostics, snapshot, apply_original_camera_controls
from q1_media import Q1Media

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOG = logging.getLogger(__name__)

def load_options():
    path = Path(os.environ.get("OPTIONS_PATH", "/data/options.json"))
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    # Container-only development; never check credentials into git.
    return {k.lower():v for k,v in os.environ.items() if k.startswith(("YANDEX_", "DEEPSEEK_"))}


class Server:
    def __init__(self, options):
        self.o = options
        self.http = None
        self.modem = Modem(options.get("at_port","/dev/ttyUSB2"),
                           options.get("serial_baud",115200), self.event,
                           options.get("max_calls_per_hour",4))
        self.audio = PCMTransport(options.get("pcm_port","/dev/ttyUSB4"))
        self.speech = None
        self.brain = None
        self.session = None
        self.mode = "booting"
        self.last_error = ""
        self.events = []
        self.api_token = options.get("api_token","")
        if not self.api_token:
            token_path=Path("/data/companion_api_token")
            if token_path.exists():
                self.api_token=token_path.read_text(encoding="ascii").strip()
            else:
                self.api_token=secrets.token_urlsafe(40)
                token_path.parent.mkdir(parents=True,exist_ok=True)
                token_path.write_text(self.api_token,encoding="ascii")
                token_path.chmod(0o600)
        self.media=Q1Media()
        self.mic_lock=asyncio.Lock()
        self.speaker_lock=asyncio.Lock()
        self.speaking = asyncio.Lock()
        self.next_prompt = ""

    async def setup(self):
        self.http = ClientSession()
        self.brain = Brain(self.o,self.http)
        if self.o.get("yandex_api_key"):
            self.speech = SpeechKit(self.o["yandex_api_key"],self.o.get("yandex_voice","jane"))
        await self.modem.start()
        self.mode = "ready"

    async def close(self):
        if self.session:
            self.session.cancel()
            try:
                await self.session
            except asyncio.CancelledError:
                pass
        await self.audio.stop()
        await self.media.stop()
        await self.modem.stop()
        if self.speech:
            await self.speech.close()
        if self.http:
            await self.http.close()

    async def event(self, name, data):
        self.events.append({"name":name,"data":data})
        self.events=self.events[-25:]
        LOG.info("JARVIS event: %s",name)
        if name == "incoming_call":
            # Caller ID often arrives a moment after RING.
            if self.o.get("auto_answer",False):
                asyncio.create_task(self._maybe_answer())
        if name == "call_connected":
            asyncio.create_task(self._start_session())
        if name in ("call_ended","modem_offline"):
            if self.session:
                self.session.cancel()
                self.session = None
            await self.audio.stop()
            self.mode="ready" if name=="call_ended" else "offline"

    async def _maybe_answer(self):
        await asyncio.sleep(1)
        number=self.modem.active_number
        allowed=self.o.get("allowed_number","")
        if not allowed or number!=allowed:
            LOG.warning("Ignoring incoming call: caller not on allowlist")
            return
        if self.modem.call_state=="ringing":
            await self.modem.answer()

    async def _start_session(self):
        if self.session and not self.session.done():
            return
        self.session=asyncio.create_task(self._dialog())

    async def _say(self, text):
        if not self.speech:
            raise RuntimeError("Yandex SpeechKit API key is missing")
        async with self.speaking:
            async for pcm in self.speech.synthesize(text):
                await self.audio.write(pcm)

    async def _dialog(self):
        try:
            self.mode="starting_audio"
            await self.modem.enable_pcm()
            await self.audio.start()
            if not self.speech:
                raise RuntimeError("Configure Yandex API key for dialog")
            self.mode="talking"
            prompt=self.next_prompt or "Здравствуйте. Это Джарвис. Слушаю вас."
            self.next_prompt = ""
            await self._say(prompt)
            # Half-duplex prevents false STT caused by remote playback echo.
            while self.modem.call_state=="connected":
                while not self.audio.queue.empty():
                    self.audio.queue.get_nowait()
                phrase=await self.speech.recognize_live(self.audio.queue)
                if not phrase:
                    continue
                LOG.info("Live STT recognized %d chars",len(phrase))
                if phrase.lower().strip(" .!") in ("пока","до свидания","заверши звонок"):
                    await self._say("До связи.")
                    await self.modem.hangup()
                    return
                answer=await self.brain.reply(phrase)
                LOG.info("LLM answer length=%d",len(answer))
                await self._say(answer)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error=str(exc)[:250]
            self.mode="error"
            LOG.exception("Voice session failed")
            # Do not hang up just because the speech service fails: leave the call under user control.
        finally:
            await self.audio.stop()

    @web.middleware
    async def auth(self, request, handler):
        if request.path in ("/health",):
            return await handler(request)
        token=request.headers.get("Authorization","").removeprefix("Bearer ").strip()
        if not self.api_token or not secrets.compare_digest(token,self.api_token):
            raise web.HTTPUnauthorized(text="Set api_token and supply Bearer token")
        return await handler(request)

    async def health(self, request):
        return web.json_response({"ok":True,"modem":self.modem.call_state,"mode":self.mode})

    async def status(self, request):
        return web.json_response({"mode":self.mode,"call_state":self.modem.call_state,
             "active_number":self.modem.active_number,"signal":self.modem.signal,
             "model":self.modem.model,"firmware":self.modem.firmware,
             "last_error":self.last_error,"events":self.events[-10:],
            "speaker_playing":self.media.playing,
            "capabilities":{
                "camera_configured":bool(self.o.get("q1_video_device")),
                "microphone_configured":bool(self.o.get("q1_mic_device")),
                "speaker_configured":bool(self.o.get("q1_speaker_device"))
            }})

    async def dial(self, request):
        data=await request.json()
        number=data.get("number") or self.o.get("allowed_number","")
        if not self.o.get("allowed_number") or number != self.o["allowed_number"]:
            raise web.HTTPForbidden(text="Number not allowlisted")
        prompt=str(data.get("prompt","")).strip()
        if len(prompt)>1200:
            raise web.HTTPBadRequest(text="prompt too long")
        self.next_prompt=prompt
        try:
            await self.modem.dial(number)
            return web.json_response({"ok":True,"state":self.modem.call_state})
        except (RuntimeError,ValueError,ConnectionError) as exc:
            raise web.HTTPConflict(text=str(exc))

    async def hangup(self, request):
        await self.modem.hangup()
        return web.json_response({"ok":True})

    async def answer(self, request):
        # Explicit answer still requires caller ID.
        if not self.o.get("allowed_number") or self.modem.active_number != self.o["allowed_number"]:
            raise web.HTTPForbidden(text="Caller not allowlisted")
        await self.modem.answer()
        return web.json_response({"ok":True})

    async def chat(self, request):
        data=await request.json()
        text=str(data.get("text","")).strip()
        if not text or len(text)>2000:
            raise web.HTTPBadRequest(text="text must be 1..2000 chars")
        response=await self.brain.reply(text)
        return web.json_response({"response":response})

    async def speak(self, request):
        data=await request.json()
        text=str(data.get("text","")).strip()
        if not text or len(text)>2000:
            raise web.HTTPBadRequest(text="text must be 1..2000 chars")
        if not self.audio.running:
            raise web.HTTPConflict(text="No active PCM audio session")
        await self._say(text)
        return web.json_response({"ok":True})


    async def q1_listen(self, request):
        if not self.speech:
            raise web.HTTPConflict(text="SpeechKit API key missing")
        if not self.o.get("q1_mic_device"):
            raise web.HTTPConflict(text="No Q1 microphone ALSA device configured")
        data=await request.json()
        seconds=min(30,max(3,int(data.get("seconds",12))))
        async with self.mic_lock:
            phrase=await recognize_microphone_live(self.speech,self.o["q1_mic_device"],seconds)
        if not phrase:
            return web.json_response({"recognized":"","response":""})
        answer=await self.brain.reply(phrase)
        if self.o.get("q1_speaker_device"):
            async with self.speaker_lock:
                await play_stream(self.o["q1_speaker_device"],self.speech.synthesize(answer))
        return web.json_response({"recognized":phrase,"response":answer})

    async def q1_speak(self, request):
        if not self.speech:
            raise web.HTTPConflict(text="SpeechKit API key missing")
        if not self.o.get("q1_speaker_device"):
            raise web.HTTPConflict(text="No Q1 speaker ALSA device configured")
        data=await request.json()
        text=str(data.get("text","")).strip()
        if not text or len(text)>2000:
            raise web.HTTPBadRequest(text="text must be 1..2000 chars")
        async with self.speaker_lock:
            await play_stream(self.o["q1_speaker_device"],self.speech.synthesize(text))
        return web.json_response({"ok":True})


    async def q1_probe(self,request):
        result=await diagnostics(self.o.get("q1_video_device",""))
        result["modem"]={"state":self.modem.call_state,
                         "model":self.modem.model,"signal":self.modem.signal,
                         "configured_at_port":self.modem.port}
        return web.json_response(result)

    async def q1_snapshot(self,request):
        device=self.o.get("q1_video_device","")
        if not device:
            raise web.HTTPConflict(text="Configure q1_video_device")
        try:
            content=await snapshot(device)
        except (ValueError,RuntimeError) as exc:
            raise web.HTTPConflict(text=str(exc)[:250])
        return web.Response(body=content,content_type="image/jpeg",
                            headers={"Cache-Control":"no-store"})

    async def q1_camera_setup(self,request):
        if not self.o.get("q1_video_device"):
            raise web.HTTPConflict(text="Configure q1_video_device")
        return web.json_response(await apply_original_camera_controls(self.o["q1_video_device"]))

    async def q1_gps(self,request):
        try:
            return web.json_response(await gps_status(self.modem))
        except (RuntimeError,ConnectionError) as exc:
            raise web.HTTPConflict(text=str(exc)[:250])

    async def q1_gps_start(self,request):
        data=await request.json()
        try:
            return web.json_response(await gps_start(self.modem,assisted=data.get("assisted") is True))
        except (RuntimeError,ConnectionError) as exc:
            raise web.HTTPConflict(text=str(exc)[:250])

    async def q1_led(self,request):
        data=await request.json()
        try:
            return web.json_response(await led_set(self.modem,str(data.get("color",""))))
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        except (RuntimeError,ConnectionError) as exc:
            raise web.HTTPConflict(text=str(exc)[:250])

    async def q1_play_media(self,request):
        data=await request.json()
        try:
            await self.media.play(str(data.get("url","")),
                self.o.get("q1_speaker_device",""),
                self.o.get("media_allowed_hosts",["homeassistant","supervisor","homeassistant.local"]))
        except (ValueError,RuntimeError) as exc:
            raise web.HTTPBadRequest(text=str(exc))
        return web.json_response({"ok":True,"playing":True})

    async def q1_stop_media(self,request):
        await self.media.stop()
        return web.json_response({"ok":True})

    async def q1_mic_stream(self,request):
        device=self.o.get("q1_mic_device","")
        if not device:
            raise web.HTTPConflict(text="No Q1 microphone configured")
        try:
            seconds=min(120,max(1,int(request.query.get("seconds","15"))))
        except ValueError:
            raise web.HTTPBadRequest(text="seconds must be integer")
        if self.mic_lock.locked():
            raise web.HTTPConflict(text="Microphone already in use")
        async with self.mic_lock:
            proc=await asyncio.create_subprocess_exec(
                "arecord","-q","-D",device,"-f","S16_LE","-r","16000","-c","1","-t","raw",
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
            response=web.StreamResponse(status=200,headers={
                "Content-Type":"application/octet-stream",
                "X-Audio-Format":"s16le;rate=16000;channels=1",
                "Cache-Control":"no-store"})
            await response.prepare(request)
            try:
                end=asyncio.get_running_loop().time()+seconds
                while asyncio.get_running_loop().time()<end:
                    frame=await asyncio.wait_for(proc.stdout.read(3200),timeout=4)
                    if not frame:
                        break
                    await response.write(frame)
            except (ConnectionResetError,asyncio.TimeoutError):
                pass
            finally:
                if proc.returncode is None:
                    proc.kill()
                await proc.communicate()
                try:
                    await response.write_eof()
                except ConnectionResetError:
                    pass
            return response


    async def q1_transcribe(self, request):
        if not self.speech:
            raise web.HTTPConflict(text="Configure SpeechKit API key")
        if not self.o.get("q1_mic_device"):
            raise web.HTTPConflict(text="Configure Q1 microphone device")
        data=await request.json()
        seconds=min(30,max(3,int(data.get("seconds",12))))
        if self.mic_lock.locked():
            raise web.HTTPConflict(text="Microphone is already in use")
        async with self.mic_lock:
            text=await recognize_microphone_live(self.speech,self.o["q1_mic_device"],seconds)
        return web.json_response({"text":text})

    async def discover_companion(self):
        supervisor_token=os.environ.get("SUPERVISOR_TOKEN","")
        if not supervisor_token:
            LOG.warning("Supervisor token unavailable; manual companion setup required")
            return
        payload={"service":"jarvis_q1","config":{
            "host":socket.gethostname(),"port":8099,"token":self.api_token}}
        try:
            async with self.http.post("http://supervisor/discovery",json=payload,
                headers={"Authorization":"Bearer "+supervisor_token},
                timeout=__import__("aiohttp").ClientTimeout(total=8)) as resp:
                if resp.status not in (200,201):
                    LOG.warning("Supervisor discovery HTTP %s: %s",resp.status,(await resp.text())[:100])
                else:
                    LOG.info("JARVIS Q1 companion discovery sent")
        except Exception as exc:
            LOG.warning("Supervisor discovery unavailable: %s",exc)

    def app(self):
        app=web.Application(middlewares=[self.auth],client_max_size=256*1024)
        for method,path,fn in (
            ("GET","/health",self.health),("GET","/status",self.status),
            ("POST","/dial",self.dial),("POST","/hangup",self.hangup),
            ("POST","/answer",self.answer),("POST","/chat",self.chat),
            ("POST","/speak",self.speak),
            ("POST","/q1/listen",self.q1_listen),("POST","/q1/speak",self.q1_speak),
            ("GET","/q1/probe",self.q1_probe),
            ("GET","/q1/snapshot",self.q1_snapshot),
            ("POST","/q1/camera/apply_original",self.q1_camera_setup),
            ("GET","/q1/gps",self.q1_gps),
            ("POST","/q1/gps/start",self.q1_gps_start),
            ("POST","/q1/led",self.q1_led),
            ("POST","/q1/play_media",self.q1_play_media),
            ("POST","/q1/stop_media",self.q1_stop_media),
            ("GET","/q1/microphone/stream",self.q1_mic_stream),
            ("POST","/q1/transcribe",self.q1_transcribe)):
            app.router.add_route(method,path,fn)
        return app


async def main():
    o=load_options()
    service=Server(o)
    await service.setup()
    runner=web.AppRunner(service.app())
    await runner.setup()
    site=web.TCPSite(runner,host="0.0.0.0",port=8099)
    await site.start()
    await service.discover_companion()
    LOG.info("JARVIS API listening on port 8099; HTTP auth=%s",bool(service.api_token))
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await service.close()


if __name__=="__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
