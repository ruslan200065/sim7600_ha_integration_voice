"""JARVIS Voice Bridge: HA add-on HTTP API, SIM7600 call management, SpeechKit and DeepSeek."""
import asyncio
import json
import logging
import os
import secrets
from pathlib import Path
from aiohttp import web, ClientSession

from modem import Modem
from audio import PCMTransport
from speech import SpeechKit
from brain import Brain
from q1_audio import recognize_microphone_live, play_stream

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
             "last_error":self.last_error,"events":self.events[-10:]})

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
        phrase=await recognize_microphone_live(self.speech,self.o["q1_mic_device"],seconds)
        if not phrase:
            return web.json_response({"recognized":"","response":""})
        answer=await self.brain.reply(phrase)
        if self.o.get("q1_speaker_device"):
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
        await play_stream(self.o["q1_speaker_device"],self.speech.synthesize(text))
        return web.json_response({"ok":True})

    def app(self):
        app=web.Application(middlewares=[self.auth],client_max_size=256*1024)
        for method,path,fn in (("GET","/health",self.health),("GET","/status",self.status),
                ("POST","/dial",self.dial),("POST","/hangup",self.hangup),
                ("POST","/answer",self.answer),("POST","/chat",self.chat),
                ("POST","/speak",self.speak),
                ("POST","/q1/listen",self.q1_listen),("POST","/q1/speak",self.q1_speak)):
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
