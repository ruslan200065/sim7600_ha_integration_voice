"""Authenticated add-on API, request validation and route registration."""

import asyncio
import logging
import secrets
from datetime import datetime, timezone

from aiohttp import ClientError, web
from llm import active_profile
from request_helpers import read_json

LOG = logging.getLogger(__name__)


class HttpApi:
    async def notify_event(self, request):
        data = await read_json(request)
        member = data.get("member", "")
        profile = (
            self.owner_key
            if not member
            else next(
                (
                    row["key"]
                    for row in self.family.values()
                    if row["key"] == member
                    or row["name"].casefold() == str(member).casefold()
                ),
                None,
            )
        )
        if profile not in self.reminder_numbers:
            raise web.HTTPBadRequest(text="Choose a configured recipient")
        text, source = data.get("text"), data.get("source")
        if not isinstance(text, str) or not 0 < len(text.strip()) <= 500:
            raise web.HTTPBadRequest(text="text must be 1..500 characters")
        camera = data.get("camera")
        if camera is not None:
            selected_member = next(
                (row for row in self.family.values() if row["key"] == profile), None
            )
            if not isinstance(camera, str) or not self.caller_brain(
                selected_member
            ).camera_allowed(camera):
                raise web.HTTPBadRequest(
                    text="Camera is not allowed for this recipient"
                )
        urgent = data.get("urgent", False)
        shared = data.get("shared", False)
        if type(shared) is not bool:
            raise web.HTTPBadRequest(text="shared must be boolean")
        if type(urgent) is not bool:
            raise web.HTTPBadRequest(text="urgent must be boolean")
        try:
            incident = await asyncio.to_thread(
                self.policies.coalesce,
                profile,
                source,
                {"text": text, "camera": data.get("camera")},
            )
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        await self.record_trace(
            profile,
            "event:" + incident["id"],
            "event",
            "grouped" if not incident["notify"] else "new",
            incident,
        )
        if not incident["notify"]:
            return web.json_response(
                {
                    "grouped": True,
                    "incident": incident["id"],
                    "count": incident["count"],
                }
            )
        now = datetime.now(timezone.utc)
        decision = await asyncio.to_thread(self.policies.decide, profile, now, urgent)
        if not decision["allowed"] or decision["channel"] == "call":
            due = (
                datetime.fromisoformat(decision["defer_until"])
                if decision["defer_until"]
                else now
            )
            context = {
                "kind": "time",
                "notification_urgent": urgent,
                "notification_shared": shared,
                "notification_event": True,
                "event_id": incident["id"],
                "source": source,
                "camera": data.get("camera"),
            }
            identifier = await asyncio.to_thread(
                self.personal.reminder,
                profile,
                member or "Владелец",
                text,
                due,
                context,
            )
            return web.json_response(
                {"queued": True, "id": identifier, "incident": incident["id"]}
            )
        try:
            await self.send_telegram_notice(decision["telegram_target"], text)
        except (RuntimeError, ClientError, TimeoutError):
            # Preserve a failed delivery as a bounded requester-scoped reminder.
            identifier = await asyncio.to_thread(
                self.personal.reminder,
                profile,
                member or "Владелец",
                text,
                now,
                {
                    "kind": "time",
                    "notification_urgent": urgent,
                    "notification_shared": shared,
                    "notification_event": True,
                    "event_id": incident["id"],
                    "source": source,
                    "camera": data.get("camera"),
                },
            )
            return web.json_response({"queued": True, "id": identifier})
        return web.json_response(
            {"sent": True, "channel": decision["channel"], "incident": incident["id"]}
        )

    @web.middleware
    async def auth(self, request, handler):
        if request.path == "/health" or request.path.startswith("/q1/shared-photo/"):
            return await handler(request)
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not self.api_token or not secrets.compare_digest(token, self.api_token):
            raise web.HTTPUnauthorized(text="Set api_token and supply Bearer token")
        return await handler(request)

    async def health(self, request):
        return web.json_response(
            {
                "ok": True,
                "modem": self.modem.call_state,
                "mode": self.mode,
            }
        )

    async def readiness(self, request):
        ready = self.modem.call_state != "offline"
        return web.json_response(
            {
                "ready": ready,
                "modem": self.modem.call_state,
                "speech_configured": self.speech is not None,
            },
            status=200 if ready else 503,
        )

    async def acknowledge_call_events(self, request):
        data = await read_json(request)
        sequence = data.get("sequence")
        if type(sequence) is not int or not 0 < sequence < 2**63:
            raise web.HTTPBadRequest(text="Invalid call event sequence")
        if self.call_outbox is None or not await asyncio.to_thread(
            self.call_outbox.acknowledge,
            data.get("stream"),
            sequence,
        ):
            raise web.HTTPConflict(text="Unknown call event stream")
        return web.json_response({"ok": True})

    async def status(self, request):
        call_events = (
            await asyncio.to_thread(self.call_outbox.batch)
            if self.call_outbox is not None
            else list(self.call_events)
        )
        return web.json_response(
            {
                "mode": self.mode,
                "call_state": self.modem.call_state,
                "active_number": self.modem.active_number,
                "signal": self.modem.signal,
                "model": self.modem.model,
                "firmware": self.modem.firmware,
                "last_error": self.last_error,
                "events": self.events[-10:],
                "call_event_stream": self.call_event_stream,
                "call_events": call_events,
                "call_events_durable": self.call_outbox is not None,
                "speaker_playing": self.media.playing,
                "capabilities": {
                    "camera_configured": bool(self.o.get("q1_video_device")),
                    "microphone_configured": bool(self.o.get("q1_mic_device")),
                    "speaker_configured": bool(self.o.get("q1_speaker_device")),
                    "speech_configured": self.speech is not None,
                    "assistant_configured": bool(
                        (profile := active_profile(self.o))
                        and profile["model"]
                        and (profile["id"] != "deepseek" or profile["api_key"])
                    ),
                },
            }
        )

    async def dial(self, request):
        data = await read_json(request)
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="Expected a JSON object")
        return await self.dial_number(
            data.get("number"),
            data.get("prompt", ""),
            data.get("camera_entity"),
            data.get("member"),
        )

    async def hangup(self, request):
        try:
            await self.modem.hangup()
        except (RuntimeError, OSError) as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        return web.json_response({"ok": True})

    async def send_dtmf(self, request):
        data = await read_json(request)
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="Expected a JSON object")
        try:
            await self.modem.send_dtmf(data.get("digits"))
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        except (RuntimeError, OSError) as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        return web.json_response({"ok": True})

    async def answer(self, request):
        # Explicit answer still requires caller ID.
        if not self.caller_allowed():
            raise web.HTTPForbidden(text="Caller not allowlisted")
        try:
            await self.modem.answer()
        except (RuntimeError, OSError) as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        return web.json_response({"ok": True})

    async def chat(self, request):
        data = await read_json(request)
        text = str(data.get("text", "")).strip()
        if not text or len(text) > 2000:
            raise web.HTTPBadRequest(text="text must be 1..2000 chars")
        response = await self.room_reply(text)
        return web.json_response({"response": response})

    async def speak(self, request):
        data = await read_json(request)
        text = str(data.get("text", "")).strip()
        if not text or len(text) > 2000:
            raise web.HTTPBadRequest(text="text must be 1..2000 chars")
        if not self.audio.running:
            raise web.HTTPConflict(text="No active PCM audio session")
        await self._say(text)
        return web.json_response({"ok": True})

    def app(self):
        app = web.Application(middlewares=[self.auth], client_max_size=256 * 1024)
        app.on_shutdown.append(self.camera_panel.shutdown)
        for method, path, fn in (
            ("POST", "/call-events/ack", self.acknowledge_call_events),
            ("GET", "/health", self.health),
            ("GET", "/ready", self.readiness),
            ("GET", "/status", self.status),
            ("POST", "/notify", self.notify_event),
            ("POST", "/dial", self.dial),
            ("POST", "/hangup", self.hangup),
            ("POST", "/answer", self.answer),
            ("POST", "/chat", self.chat),
            ("POST", "/dtmf", self.send_dtmf),
            ("POST", "/speak", self.speak),
            ("POST", "/q1/listen", self.q1_listen),
            ("POST", "/q1/speak", self.q1_speak),
            ("GET", "/q1/probe", self.q1_probe),
            ("GET", "/q1/snapshot", self.q1_snapshot),
            ("GET", "/q1/camera/live", self.camera_panel.mjpeg),
            ("GET", "/q1/shared-photo/{ticket}", self.shared_photo),
            ("POST", "/q1/camera/apply_original", self.q1_camera_setup),
            ("POST", "/q1/led", self.q1_led),
            ("POST", "/q1/play_media", self.q1_play_media),
            ("POST", "/q1/stop_media", self.q1_stop_media),
            ("GET", "/q1/microphone/stream", self.q1_mic_stream),
            ("GET", "/q1/microphone", self.microphone_status),
            ("POST", "/q1/microphone", self.microphone_switch),
            ("POST", "/q1/transcribe", self.q1_transcribe),
        ):
            app.router.add_route(method, path, fn)
        return app
