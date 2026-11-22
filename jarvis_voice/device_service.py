"""Q1 audio, camera, LED and room voice operations."""

import asyncio
import logging
import secrets
import socket
import time

from aiohttp import ClientError, web
from audio import rms
from brain import Brain
from processes import stop_process
from q1_audio import (
    audio_route,
    capture_pcm,
    play_stream,
    recognize_microphone_live,
    start_pcm_process,
)
from q1_hardware import (
    apply_original_camera_controls,
    camera_device,
    diagnostics,
    snapshot,
)
from q1_protocol import led_set
from request_helpers import read_json

LOG = logging.getLogger(__name__)


class DeviceService:
    def room_brain(self):
        """Restricted profile for unauthenticated room speech (/chat, Q1 microphone)."""
        if getattr(self, "_room_brain", None) is None:
            entities = list(self.o.get("room_entities") or [])
            options = {
                **self.o,
                "camera_entity": "",
                "reminder_calendar": "",
                "reminder_person": "",
                "allowed_entities": entities,
                "observed_entities": entities,
                "observe_all_entities": False,
                "control_all_entities": False,
            }
            self._room_brain = Brain(
                options,
                self.http,
                None,
                share_photo=self.share_photo,
                knowledge=self.knowledge,
                policies=self.policies,
                timeline=self.timeline,
                autonomy=self.autonomy,
                test_rules=self.test_rules,
                inbox=self.inbox,
                events=self.events_journal,
                contacts=self.contacts,
                personal=self.personal,
                profile="room",
                profile_name="Комната",
                timezone_name=self.brain.timezone.key,
                restricted_entities=self.gates,
                registry_cache=self.brain.registry_cache,
                caller_number="",
                owner=False,
            )
        return self._room_brain

    async def room_reply(self, text):
        """Answer a room phrase with JARVIS (room profile) or Home Assistant Assist."""
        if str(self.o.get("room_agent") or "jarvis") != "assist":
            return await self.room_brain().reply(text)
        payload = {"text": text, "language": "ru"}
        agent = str(self.o.get("assist_agent_id") or "").strip()
        if agent:
            payload["agent_id"] = agent
        conversation = getattr(self, "assist_conversation", None)
        if conversation and time.monotonic() - conversation[1] < 300:
            payload["conversation_id"] = conversation[0]
        try:
            result = await self.brain._ha(
                "POST", "/api/conversation/process", payload, timeout=30
            )
        except (ClientError, TimeoutError, RuntimeError, ValueError) as exc:
            LOG.warning("Assist request failed: %s", exc)
            return "Home Assistant Assist сейчас недоступен."
        if not isinstance(result, dict):
            return "Assist не вернул ответ."
        if result.get("conversation_id"):
            self.assist_conversation = (result["conversation_id"], time.monotonic())
        speech = ((result.get("response") or {}).get("speech") or {}).get("plain") or {}
        return str(speech.get("speech") or "").strip()[:2000] or "Готово."

    async def listen_room(self, seconds):
        device = self.o.get("q1_mic_device")
        if not device or not self.speech:
            return {"error": "Микрофон Q1 или SpeechKit не настроен"}
        async with self.mic_lock:
            pcm = await capture_pcm(device, seconds)
        level = rms(pcm)
        if not pcm or level == 0:
            return {
                "seconds": seconds,
                "rms": level,
                "error": "Микрофон не передал звуковой сигнал",
            }
        phrase = await self.speech.recognize(pcm)
        return {"seconds": seconds, "rms": level, "speech": phrase}

    async def share_photo(self, target, picture, mime, recipient=None):
        ticket = secrets.token_urlsafe(32)
        self.shared_photos[ticket] = (picture, mime)
        try:
            result = await self.brain._ha(
                "POST",
                "/api/services/telegram_bot/send_photo?return_response",
                {
                    "entity_id": target,
                    **({"target": [recipient]} if recipient else {}),
                    "url": f"http://{socket.gethostname()}:8099/q1/shared-photo/{ticket}",
                },
                timeout=25,
            )
            chats = result.get("service_response", {}).get("chats", [])
            if not any(chat.get("message_id") for chat in chats):
                raise RuntimeError("Telegram did not return a message ID")
        finally:
            self.shared_photos.pop(ticket, None)

    async def shared_photo(self, request):
        shared = self.shared_photos.pop(request.match_info["ticket"], None)
        if shared is None:
            raise web.HTTPNotFound()
        picture, mime = shared
        return web.Response(
            body=picture, content_type=mime, headers={"Cache-Control": "no-store"}
        )

    async def q1_listen(self, request):
        if not self.speech:
            raise web.HTTPConflict(text="SpeechKit API key missing")
        if not self.o.get("q1_mic_device"):
            raise web.HTTPConflict(text="No Q1 microphone ALSA device configured")
        data = await read_json(request)
        try:
            seconds = min(30, max(3, int(data.get("seconds", 12))))
        except (TypeError, ValueError) as exc:
            raise web.HTTPBadRequest(text="seconds must be an integer") from exc
        async with self.mic_lock:
            phrase = await recognize_microphone_live(
                self.speech, self.o["q1_mic_device"], seconds
            )
        if not phrase:
            return web.json_response({"recognized": "", "response": ""})
        answer = await self.room_reply(phrase)
        if self.o.get("q1_speaker_device"):
            async with self.speaker_lock:
                await self.media.stop()
                await play_stream(
                    self.o["q1_speaker_device"], self.speech.synthesize(answer)
                )
        return web.json_response({"recognized": phrase, "response": answer})

    async def q1_speak(self, request):
        if not self.speech:
            raise web.HTTPConflict(text="SpeechKit API key missing")
        if not self.o.get("q1_speaker_device"):
            raise web.HTTPConflict(text="No Q1 speaker ALSA device configured")
        data = await read_json(request)
        text = str(data.get("text", "")).strip()
        if not text or len(text) > 2000:
            raise web.HTTPBadRequest(text="text must be 1..2000 chars")
        async with self.speaker_lock:
            await self.media.stop()
            await play_stream(self.o["q1_speaker_device"], self.speech.synthesize(text))
        return web.json_response({"ok": True})

    async def q1_probe(self, request):
        result = await diagnostics(self.o.get("q1_video_device", ""))
        result["modem"] = {
            "state": self.modem.call_state,
            "model": self.modem.model,
            "signal": self.modem.signal,
            "configured_at_port": self.modem.port,
        }
        return web.json_response(result)

    async def capture_camera(self, device=None):
        if device is None:
            device = self.o.get("q1_video_device", "")
        if not device:
            raise RuntimeError("Configure q1_video_device")
        device = camera_device(device)
        async with self.camera_lock:
            now = asyncio.get_running_loop().time
            if self.camera_panel.device == device:
                current = self.camera_panel.last_frame
                if current and now() - current[0] < 2:
                    return current[1]
                raise RuntimeError("Предпросмотр ещё не получил кадр камеры.")
            if (
                self.camera_frame
                and self.camera_frame[0] == device
                and now() - self.camera_frame[1] < 1
            ):
                return self.camera_frame[2]
            content = await snapshot(device)
            self.camera_frame = (device, now(), content)
            return content

    async def q1_snapshot(self, request):
        try:
            content = await self.capture_camera()
        except (ValueError, RuntimeError) as exc:
            raise web.HTTPConflict(text=str(exc)[:250]) from exc
        return web.Response(
            body=content,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    async def q1_camera_setup(self, request):
        if not self.o.get("q1_video_device"):
            raise web.HTTPConflict(text="Configure q1_video_device")
        async with self.camera_lock:
            result = await apply_original_camera_controls(self.o["q1_video_device"])
            self.camera_frame = None
        return web.json_response(result)

    async def q1_led(self, request):
        data = await read_json(request)
        try:
            return web.json_response(
                await led_set(self.modem, str(data.get("color", "")))
            )
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        except (RuntimeError, ConnectionError) as exc:
            raise web.HTTPConflict(text=str(exc)[:250])

    async def q1_play_media(self, request):
        data = await read_json(request)
        try:
            async with self.speaker_lock:
                await self.media.play(
                    self.http,
                    str(data.get("url", "")),
                    self.o.get("q1_speaker_device", ""),
                    self.o.get(
                        "media_allowed_hosts", ["homeassistant", "homeassistant.local"]
                    ),
                )
        except (ValueError, RuntimeError) as exc:
            raise web.HTTPBadRequest(text=str(exc))
        return web.json_response({"ok": True, "playing": True})

    async def q1_stop_media(self, request):
        async with self.speaker_lock:
            await self.media.stop()
        return web.json_response({"ok": True})

    async def microphone_status(self, _request):
        return web.json_response(self.microphone.status())

    async def microphone_switch(self, request):
        data = await read_json(request)
        if not isinstance(data, dict) or not isinstance(data.get("enabled"), bool):
            raise web.HTTPBadRequest(text="Expected enabled: true or false")
        try:
            self.microphone.set_enabled(data["enabled"])
        except ValueError as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        return web.json_response(self.microphone.status())

    async def q1_mic_stream(self, request):
        device = self.o.get("q1_mic_device", "")
        if not device:
            raise web.HTTPConflict(text="No Q1 microphone configured")
        try:
            seconds = min(120, max(1, int(request.query.get("seconds", "15"))))
        except ValueError:
            raise web.HTTPBadRequest(text="seconds must be integer")
        if self.mic_lock.locked() and not self.microphone.sampling:
            raise web.HTTPConflict(text="Microphone already in use")
        async with self.mic_lock:
            proc = await start_pcm_process(
                audio_route(device, "capture"),
                "capture",
                seconds=seconds,
            )
            response = web.StreamResponse(
                status=200,
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Audio-Format": "s16le;rate=16000;channels=1",
                    "Cache-Control": "no-store",
                },
            )
            try:
                try:
                    frame = await asyncio.wait_for(proc.stdout.read(3200), timeout=4)
                except TimeoutError as exc:
                    raise web.HTTPBadGateway(
                        text="Microphone did not produce PCM"
                    ) from exc
                if not frame:
                    raise web.HTTPBadGateway(
                        text="Microphone did not produce PCM. Check /q1/probe and q1_mic_device."
                    )
                await response.prepare(request)
                while frame:
                    await asyncio.wait_for(response.write(frame), 4)
                    frame = await asyncio.wait_for(proc.stdout.read(3200), timeout=4)
            except (ConnectionResetError, TimeoutError):
                pass
            finally:
                await stop_process(proc)
                if response.prepared:
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
        data = await read_json(request)
        try:
            seconds = min(30, max(3, int(data.get("seconds", 12))))
        except (TypeError, ValueError) as exc:
            raise web.HTTPBadRequest(text="seconds must be an integer") from exc
        if self.mic_lock.locked() and not self.microphone.sampling:
            raise web.HTTPConflict(text="Microphone is already in use")
        async with self.mic_lock:
            text = await recognize_microphone_live(
                self.speech, self.o["q1_mic_device"], seconds
            )
        return web.json_response({"text": text})
