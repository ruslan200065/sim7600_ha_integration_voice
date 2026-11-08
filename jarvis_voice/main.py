from knowledge import Knowledge
from notifications import NotificationPolicies
from timeline import Timeline
"""JARVIS Voice Bridge: HA add-on HTTP API, SIM7600 call management, SpeechKit and DeepSeek."""
import asyncio
import json
import hmac
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import logging
import os
import secrets
import socket
import time
from contextlib import aclosing
from pathlib import Path
import signal
from aiohttp import web, ClientError, ClientSession, ClientTimeout

from modem import Modem
from audio import PCMPlaybackError, PCMTransport, rms
from voice_dialog import CallOutput, run_dialog
from speech import SpeechKit
from brain import Brain
from q1_audio import audio_route, capture_pcm, recognize_microphone_live, play_stream
from q1_protocol import gps_status, gps_start, led_set
from q1_hardware import diagnostics, snapshot, apply_original_camera_controls
from q1_media import Q1Media
from setup_panel import SetupPanel
from history import History
from family import GATE_ACTIONS, family_profiles, lock_entities, profile_key
from personal import Personal, condition_matches
from speech_cache import SpeechCache
from microphone import MicrophoneMonitor
from llm import active_profile

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOG = logging.getLogger(__name__)

def phone_key(number, country="7"):
    """Normalise caller numbers so 8XXXXXXXXXX, +7XXXXXXXXXX and 7XXXXXXXXXX compare equal."""
    digits = re.sub(r"[^0-9]", "", number or "")
    if country == "7" and len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif country and len(digits) == 10 and not digits.startswith(country):
        digits = country + digits
    return digits


async def read_json(request):
    """Parse a JSON object body or answer 400 instead of a 500 traceback."""
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise web.HTTPBadRequest(text="Body must be JSON") from exc
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="Body must be a JSON object")
    return data


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
        self.audio = PCMTransport(
            options.get("pcm_port", "/dev/ttyUSB4"),
            playback_volume=options.get("call_playback_volume", 50),
            on_lost=self.pcm_lost,
        )
        self.pcm_lost_task = None
        self.speech = None
        self.brain = None
        self.session = None
        self.keypad = None
        self.session_lock = asyncio.Lock()
        self.auto_answer_task = None
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
        self.camera_lock=asyncio.Lock()
        self.camera_frame=None
        self.speaking = asyncio.Lock()
        self.next_prompt = None
        self.shared_photos = {}
        data_dir = Path(os.environ.get("OPTIONS_PATH", "/data/options.json")).parent
        self.history = History(data_dir / "history.sqlite3")
        self.personal = Personal(data_dir / "personal.sqlite3")
        self.knowledge = Knowledge(data_dir / "knowledge.sqlite3")
        self.policies = NotificationPolicies(data_dir / "notifications.sqlite3")
        self.timeline = Timeline(data_dir / "timeline.sqlite3")
        self.speech_cache = SpeechCache(data_dir / "speech_cache")
        self.microphone = MicrophoneMonitor(options.get("q1_mic_device", ""), self.mic_lock,
                                            data_dir / "microphone_monitor")
        self.microphone_task = None
        self.country = str(options.get("country_code", "7") or "")
        self.family = {phone_key(number, self.country): member for number, member in
                       family_profiles(options.get("family_members", [])).items()}
        self.family.pop(phone_key(options.get("allowed_number"), self.country), None)
        self.owner_locks = lock_entities(options.get("owner_locks", []))
        self.gates = frozenset(self.owner_locks + [
            entity for member in self.family.values() for entity in member["locks"]
        ])
        self.family_brains = {}
        self.reminder_task = None
        self.registry_task = None
        self.reminder_call = None
        owner = self.o.get("allowed_number") or ""
        self.owner_key = profile_key("Владелец", owner) if owner else None
        self.reminder_numbers = {member["key"]: member["number"] for member in self.family.values()}
        if self.owner_key:
            self.reminder_numbers[self.owner_key] = owner
        self.sms_task = None
        self.sms_wake = asyncio.Event()
        self.sms_status = {}
        # The modem reader must never wait for SQLite: history is written in the background.
        self.history_queue = asyncio.Queue()
        self.history_task = None

    async def write_history(self):
        while True:
            name, data = await self.history_queue.get()
            try:
                await self.history.call(name, data)
            except Exception:
                LOG.exception("Could not save call history")
            finally:
                self.history_queue.task_done()

    async def setup(self):
        await self.history.start()
        self.history_task = asyncio.create_task(self.write_history())
        self.http = ClientSession()
        self.brain = Brain(self.o, self.http, self.capture_camera, self.listen_room, self.share_photo,
                           knowledge=self.knowledge, policies=self.policies, timeline=self.timeline,
                           personal=self.personal, profile=self.owner_key or "local",
                           profile_name=self.o.get("owner_name") or "Владелец",
                           restricted_entities=self.gates)
        if self.brain.hass_token:
            try:
                config = await self.brain._ha("GET", "/api/config", timeout=3)
                self.brain.timezone = ZoneInfo(config["time_zone"])
            except (RuntimeError, OSError, ValueError, KeyError, TimeoutError, ZoneInfoNotFoundError, ClientError):
                LOG.warning("Home Assistant time zone unavailable")
        self.reminder_task = asyncio.create_task(self.deliver_reminders())
        self.registry_task = asyncio.create_task(self.brain.watch_registry())
        if self.o.get("yandex_api_key"):
            self.speech = SpeechKit(
                self.o["yandex_api_key"],
                self.o.get("yandex_voice", "denis"),
                self.o.get("yandex_role", "neutral"),
                cache=self.speech_cache if self.o.get("speech_cache_enabled", True) else None,
            )
        self.microphone_task = asyncio.create_task(self.microphone.run())
        await self.modem.start()
        self.sms_task = asyncio.create_task(self.sync_sms())

    async def close(self):
        if self.registry_task:
            self.registry_task.cancel()
            await asyncio.gather(self.registry_task, return_exceptions=True)
        if self.microphone_task:
            self.microphone_task.cancel()
            await asyncio.gather(self.microphone_task, return_exceptions=True)
        if self.reminder_task:
            self.reminder_task.cancel()
            await asyncio.gather(self.reminder_task, return_exceptions=True)
        if self.sms_task:
            self.sms_task.cancel()
            await asyncio.gather(self.sms_task, return_exceptions=True)
        if self.auto_answer_task:
            self.auto_answer_task.cancel()
            await asyncio.gather(self.auto_answer_task, return_exceptions=True)
        if self.session:
            self.session.cancel()
            try:
                await self.session
            except asyncio.CancelledError:
                pass
        if self.modem.call_state not in ("idle", "offline"):
            try:
                await asyncio.wait_for(self.modem.hangup(), 5)
            except Exception as exc:
                LOG.warning("Hangup on shutdown failed: %s", exc)
        await self.audio.stop()
        await self.media.stop()
        await self.modem.stop()
        if self.history_task:
            try:
                await asyncio.wait_for(self.history_queue.join(), 3)
            except TimeoutError:
                LOG.warning("Some call history was not saved before shutdown")
            self.history_task.cancel()
            await asyncio.gather(self.history_task, return_exceptions=True)
        if self.speech:
            await self.speech.close()
        for brain in [getattr(self, "brain", None), *self.family_brains.values(), getattr(self, "_room_brain", None)]:
            if brain and getattr(brain, "features", None):
                await brain.features.workflows.close()
        if self.http:
            await self.http.close()

    def pcm_lost(self):
        """USB PCM vanished mid-call: stop the dialog and release the call now."""
        if self.pcm_lost_task and not self.pcm_lost_task.done():
            return
        self.pcm_lost_task = asyncio.create_task(self._end_call_without_audio())

    async def _end_call_without_audio(self):
        session = self.session
        if session and not session.done():
            session.cancel()
            await asyncio.gather(session, return_exceptions=True)
        if self.modem.call_state not in ("idle", "offline"):
            try:
                await asyncio.wait_for(self.modem.hangup(), 5)
            except Exception as exc:
                LOG.warning("Hangup after PCM loss failed: %s", exc)

    def known_number(self, number):
        key = phone_key(number, self.country)
        if not key:
            return False
        owner = phone_key(self.o.get("allowed_number"), self.country)
        return key in self.family or (bool(owner) and key == owner)

    def fire_ha_event(self, name, data):
        """Send an event to the Home Assistant bus without blocking the caller."""
        if self.o.get("ha_events", True) is False or not getattr(self.brain, "hass_token", ""):
            return
        tasks = self.__dict__.setdefault("ha_event_tasks", set())

        async def send():
            try:
                await self.brain._ha("POST", "/api/events/" + name, data, timeout=5)
            except Exception as exc:
                LOG.warning("Could not send %s to Home Assistant: %s", name, exc)

        task = asyncio.create_task(send())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def _telephony_event(self, name, data):
        if name in ("incoming_call", "outgoing_call", "caller_id", "call_connected"):
            number = data.get("number") or self.modem.active_number
            if number:
                self.event_number = number
        if name == "incoming_call":
            self.event_number = data.get("number") or ""
        if name == "caller_id":
            number = getattr(self, "event_number", "")
            self.fire_ha_event("jarvis_call_incoming", {
                "number": number,
                "known": self.known_number(number),
                "call_id": self.modem.call_id,
            })
        elif name in ("call_ended", "call_failed"):
            self.fire_ha_event("jarvis_call_ended", {
                "number": getattr(self, "event_number", ""),
                "reason": data.get("reason") or name,
                "call_id": data.get("call_id"),
            })
            self.event_number = ""
        elif name == "dtmf_received" and not (
            self.keypad and self.keypad[0] == data.get("call_id")
        ):
            # Keys typed into an active JARVIS dialog may be the gate PIN:
            # never copy them to the Home Assistant event bus and logbook.
            self.fire_ha_event("jarvis_dtmf", {
                "digit": data.get("digit"), "call_id": data.get("call_id"),
            })

    async def event(self, name, data):
        self._telephony_event(name, data)
        if name == "dtmf_received":
            keypad = self.keypad
            if keypad and keypad[0] == data["call_id"]:
                keys = keypad[1]
                if keys.full():
                    while not keys.empty():
                        keys.get_nowait()
                    keys.put_nowait(None)
                else:
                    keys.put_nowait(data["digit"])
            return
        if name == "sms_received":
            self.sms_wake.set()
        self.events.append({"name":name,"data":data})
        self.events=self.events[-25:]
        LOG.info("JARVIS event: %s",name)
        if name == "modem_connected":
            self.mode = "ready"
        if (
            name == "caller_id"
            and self.o.get("auto_answer")
            and self.caller_allowed()
            and (not self.auto_answer_task or self.auto_answer_task.done())
        ):
            self.auto_answer_task = asyncio.create_task(
                self._maybe_answer(self.modem.call_id)
            )
        if name == "call_connected" and self.speech and self.caller_allowed():
            call_id = self.modem.call_id
            prompt, camera = "", None
            if self.next_prompt and self.next_prompt[0] == call_id:
                _, prompt, camera = self.next_prompt
            self.next_prompt = None
            reminder_id = None
            if self.reminder_call and self.reminder_call[0] == call_id:
                _, identifier, number = self.reminder_call
                if phone_key(number, self.country) == self.caller_key():
                    reminder_id = identifier
                else:
                    prompt = ""
            self.session = asyncio.create_task(self._start_session(call_id, prompt, camera, reminder_id))
        if name in ("call_ended","modem_offline"):
            self.next_prompt = None
            self.reminder_call = None
            if self.session and self.session is not asyncio.current_task() and not self.session.cancelling():
                self.session.cancel()
            self.mode="ready" if name=="call_ended" else "offline"
        if name in {
            "incoming_call", "outgoing_call", "caller_id", "call_connected",
            "call_ended", "call_failed", "modem_offline",
        }:
            self.history_queue.put_nowait((name, dict(data)))

    async def import_sms(self, lines):
        completed = await self.history.import_sms(lines) or []
        for message in completed:
            self.fire_ha_event("jarvis_sms_received", {
                "number": message["number"],
                "text": message["text"],
                "received_at": message["received_at"],
                "parts": message["parts"],
                "known": self.known_number(message["number"]),
            })
        return completed

    async def sync_sms(self):
        while True:
            self.sms_wake.clear()
            if self.modem.call_state == "idle":
                try:
                    usage = await self.modem.read_sms(self.import_sms)
                    self.sms_status = {"storage": usage}
                except (RuntimeError, OSError) as exc:
                    if self.modem.call_state == "idle":
                        self.sms_status = {"error": str(exc)}
                except Exception:
                    LOG.exception("Could not synchronize the SMS archive")
                    self.sms_status = {"error": "Не удалось сохранить SMS."}
            try:
                await asyncio.wait_for(self.sms_wake.wait(), timeout=15)
            except TimeoutError:
                pass

    async def _maybe_answer(self, call_id):
        if (
            self.modem.call_id != call_id
            or self.modem.call_state != "ringing"
            or not self.caller_allowed()
        ):
            return
        try:
            await self.modem.answer()
        except (RuntimeError, ConnectionError, OSError):
            LOG.exception("Automatic answer failed")

    async def deliver_reminders(self):
        clock = asyncio.get_running_loop().time
        next_sample = 0
        while True:
            if clock() >= next_sample:
                next_sample = clock() + 15
                try:
                    await self.sample_reminder_conditions()
                    await self.run_due_actions()
                except Exception as exc:
                    LOG.warning("Reminder condition check failed: %s", type(exc).__name__)
            if self.modem.call_state == "idle":
                try:
                    now = datetime.now(timezone.utc)
                    due = await asyncio.to_thread(self.personal.due, now, self.reminder_numbers)
                    if due:
                        identifier, profile, text = due[0]
                        context = await asyncio.to_thread(self.personal.reminder_context, profile, identifier)
                        decision = await asyncio.to_thread(self.policies.decide, profile, now, context.get("notification_urgent", False))
                        if not decision["allowed"]:
                            await asyncio.to_thread(self.personal.retry_after, identifier, datetime.fromisoformat(decision["defer_until"]))
                            continue
                        if not await asyncio.to_thread(self.personal.reserve_delivery, identifier, profile, now):
                            continue
                        if decision["channel"] == "telegram":
                            try:
                                await self.send_telegram_notice(decision["telegram_target"], "Напоминание. " + text)
                                await asyncio.to_thread(self.personal.delivered, identifier)
                            except (RuntimeError, ClientError, TimeoutError):
                                LOG.warning("Telegram reminder delivery not confirmed")
                            continue
                        self.reminder_call = (self.modem.call_id + 1, identifier, self.reminder_numbers[profile])
                        try:
                            await self.dial_number(self.reminder_numbers[profile], "Напоминание. " + text, context.get("camera"))
                        except (web.HTTPException, RuntimeError, OSError):
                            self.reminder_call = None
                            LOG.info("Reminder call deferred")
                except Exception as exc:
                    LOG.warning("Reminder dispatch failed: %s", type(exc).__name__)
            await asyncio.sleep(5)

    async def run_due_actions(self):
        profiles = {member["key"]: member for member in self.family.values()}
        personal = self.personal
        status = lambda identifier, value: asyncio.to_thread(personal.action_status, identifier, value)  # noqa: E731
        due = await asyncio.to_thread(personal.due_actions, datetime.now(timezone.utc))
        for identifier, profile, condition in due:
            if not await asyncio.to_thread(personal.claim_action, identifier):
                continue
            await status(identifier, "started")
            if profile not in self.reminder_numbers:
                await status(identifier, "profile_removed")
                continue
            brain = self.caller_brain(profiles.get(profile))
            condition_allowed = (
                condition["kind"] == "time"
                or (condition["kind"] == "zone" and condition["entity"] == brain.options.get("reminder_person"))
                or (condition["kind"] == "state" and brain._visible(condition["entity"]))
            )
            if not condition_allowed:
                await status(identifier, "blocked")
                continue
            try:
                async with brain.action_lock:
                    result = await brain._tool("call_ha_service", condition["action"], preconfirmed=True)
            except (ClientError, TimeoutError, RuntimeError, ValueError):
                await status(identifier, "unconfirmed")
                continue
            except Exception:
                LOG.exception("Scheduled action failed")
                await status(identifier, "failed")
                continue
            accepted = isinstance(result, dict) and result.get("accepted")
            await status(identifier, "accepted" if accepted else "blocked")

    async def sample_reminder_conditions(self):
        watches = await asyncio.to_thread(self.personal.watches)
        if not watches:
            return
        profiles = {member["key"]: member for member in self.family.values()}
        rows = await self.brain._ha("GET", "/api/states")
        states = {row["entity_id"]: row for row in rows}
        now = datetime.now(timezone.utc)
        for identifier, profile, condition, armed in watches:
            if profile not in self.reminder_numbers:
                continue
            brain = self.caller_brain(profiles.get(profile))
            if condition["kind"] == "zone":
                permitted = condition["entity"] == brain.options.get("reminder_person")
            else:
                permitted = brain._visible(condition["entity"])
            if permitted:
                matched = condition_matches(condition, states)
                await asyncio.to_thread(self.personal.sample, identifier, matched, armed, now)

    def caller_key(self):
        return phone_key(self.modem.active_number, self.country)

    def caller_allowed(self):
        caller = self.caller_key()
        owner = phone_key(self.o.get("allowed_number"), self.country)
        return bool(caller and (caller == owner or caller in self.family))

    def caller_brain(self, member):
        if not member:
            return self.brain
        key = member["key"]
        if key not in self.family_brains:
            entities = member["entities"]
            access = member["access"]
            options = {
                **self.o, "camera_entity": "",
                "reminder_calendar": member["calendar"], "reminder_person": member["person"],
            }
            if access != "owner":
                options.update(allowed_entities=entities, observed_entities=entities,
                               observe_all_entities=access == "read_all", control_all_entities=False)
            self.family_brains[key] = Brain(
                options, self.http, None, share_photo=self.share_photo, knowledge=self.knowledge, policies=self.policies, timeline=self.timeline,
                           personal=self.personal, profile=key,
                profile_name=member["name"], timezone_name=self.brain.timezone.key,
                restricted_entities=self.gates, registry_cache=self.brain.registry_cache,
                caller_number=member["number"], owner=False,
            )
        return self.family_brains[key]

    def room_brain(self):
        """Restricted profile for unauthenticated room speech (/chat, Q1 microphone)."""
        if getattr(self, "_room_brain", None) is None:
            entities = list(self.o.get("room_entities") or [])
            options = {
                **self.o, "camera_entity": "", "reminder_calendar": "", "reminder_person": "",
                "allowed_entities": entities, "observed_entities": entities,
                "observe_all_entities": False, "control_all_entities": False,
            }
            self._room_brain = Brain(
                options, self.http, None, share_photo=self.share_photo, knowledge=self.knowledge, policies=self.policies, timeline=self.timeline,
                           personal=self.personal, profile="room",
                profile_name="Комната", timezone_name=self.brain.timezone.key,
                restricted_entities=self.gates, registry_cache=self.brain.registry_cache,
                caller_number="", owner=False,
            )
        return self._room_brain

    async def _start_session(self, call_id, prompt, camera, reminder_id=None):
        async with self.session_lock:
            for attempt in range(2):
                if (self.modem.call_id, self.modem.call_state) != (call_id, "connected"):
                    return
                failed = await self._dialog(prompt, camera, reminder_id)
                if failed is not False:
                    return
                LOG.warning("Voice session recovery call=%s attempt=%s", call_id, attempt + 1)
                await asyncio.sleep(1)
            LOG.error("Voice session recovery exhausted call=%s", call_id)

    async def start_call_audio(self, call_id):
        for attempt in range(3):
            if (self.modem.call_id, self.modem.call_state) != (call_id, "connected"):
                raise RuntimeError("Call already ended")
            try:
                await self.modem.reset_pcm(call_id)
                await self.audio.start()
                await self.modem.enable_pcm()
                await self.audio.wait_ready()
                LOG.info("Call USB PCM verified in both directions")
                return
            except (RuntimeError, OSError, TimeoutError) as exc:
                LOG.warning("Call audio setup attempt %d: %s", attempt + 1, type(exc).__name__)
                await self.audio.stop()
                if attempt == 2:
                    raise
                await asyncio.sleep(0.25 * (attempt + 1))

    async def _say(self, text):
        if not self.speech:
            raise RuntimeError("Yandex SpeechKit API key is missing")
        call_id = self.modem.call_id
        async with self.speaking:
            for attempt in range(2):
                if (self.modem.call_id, self.modem.call_state) != (call_id, "connected"):
                    return
                try:
                    async with aclosing(self.speech.synthesize(text)) as chunks:
                        await self.audio.write_stream(chunks)
                    return
                except Exception as exc:
                    failures = [exc]
                    while any(isinstance(error, ExceptionGroup) for error in failures):
                        failures = [nested for error in failures for nested in
                                    (error.exceptions if isinstance(error, ExceptionGroup) else [error])]
                    kinds = [str(error) if isinstance(error, PCMPlaybackError) else type(error).__name__
                             for error in failures]
                    LOG.warning("Call playback attempt %d: %s", attempt + 1, ", ".join(kinds))
                    if attempt or (self.modem.call_id, self.modem.call_state) != (call_id, "connected"):
                        raise
                    await asyncio.sleep(0.2)
                    if any(isinstance(error, PCMPlaybackError) for error in failures):
                        await self.audio.stop()
                        await self.start_call_audio(call_id)

    async def _dialog(self, prompt, camera, reminder_id=None):
        brain = None
        caller = ""
        initial_reminder = reminder_id
        call_id = self.modem.call_id
        try:
            self.mode="starting_audio"
            await self.start_call_audio(self.modem.call_id)
            self.mode="talking"
            caller = self.caller_key()
            member = self.family.get(caller)
            brain = self.caller_brain(member)
            reset = getattr(brain, "reset_history", None)
            if callable(reset) and getattr(brain, "session_call", None) != self.modem.call_id:
                reset()
                brain.session_call = self.modem.call_id
            pending_pin = 0.0
            digits = ""
            allowed_locks = member["locks"] if member else self.owner_locks
            gate = ""
            gate_name = ""
            pin = self.o.get("gate_pin", "")
            clock = asyncio.get_running_loop().time
            call_id = self.modem.call_id
            keypad = asyncio.Queue(maxsize=32)
            self.keypad = (call_id, keypad)

            async def open_gate():
                if (self.modem.call_id, self.modem.call_state) != (call_id, "connected"):
                    return ""
                domain = gate.split(".", 1)[0]
                try:
                    await self.brain._ha("POST", f"/api/services/{domain}/{GATE_ACTIONS[domain]}",
                                         {"entity_id": gate})
                except (RuntimeError, TimeoutError, ClientError):
                    return "Home Assistant не подтвердил команду открытия."
                return "Команда открытия отправлена."

            async def check_pin(candidate):
                nonlocal pending_pin, digits
                pending_pin = 0.0
                digits = ""
                now = datetime.now(timezone.utc)
                # Failures persist in /data, so brute force across calls or restarts is locked out.
                if await asyncio.to_thread(self.personal.pin_locked, caller, now):
                    return "Открытие по телефону временно заблокировано."
                if not hmac.compare_digest(candidate.encode(), pin.encode()):
                    await asyncio.to_thread(self.personal.pin_failed, caller, now)
                    LOG.warning("Wrong gate PIN from caller")
                    return "Код не подошёл."
                await asyncio.to_thread(self.personal.pin_succeeded, caller)
                return await open_gate()

            async def press(key):
                nonlocal pending_pin, digits
                if pending_pin <= clock():
                    return ""
                if key is None:
                    pending_pin, digits = 0.0, ""
                    return "Ввод прерван. Попроси открыть замок ещё раз."
                if key == "#":
                    return await check_pin(digits)
                if key == "*":
                    digits = ""
                elif key in "0123456789" and len(digits) <= len(pin):
                    digits += key
                pending_pin = clock() + 30
                return ""

            async def end_call(args):
                goodbye = args.get("goodbye")
                goodbye = " ".join(goodbye.split())[:160] if isinstance(goodbye, str) else ""
                return CallOutput(goodbye or "До связи.", end_call=True)

            async def read_reminder(_args):
                if not reminder_id:
                    return "Это напоминание уже озвучено."
                return CallOutput(prompt)

            async def handle_call_reminder(args):
                nonlocal reminder_id
                if not reminder_id:
                    return {"error": "Это напоминание уже обработано."}
                profile = member["key"] if member else self.owner_key
                action = args.get("action")
                if action == "decline":
                    changed = await asyncio.to_thread(self.personal.decline_reminder, profile, reminder_id)
                elif action == "defer":
                    when = args.get("when")
                    if not isinstance(when, str):
                        return {"error": "Уточни новое время напоминания."}
                    try:
                        due = brain.future_time(when)
                    except (ValueError, TypeError, OverflowError):
                        return {"error": "Уточни будущее время напоминания."}
                    changed = await asyncio.to_thread(self.personal.defer_reminder, profile, reminder_id, due)
                else:
                    return {"error": "Выбери decline или defer по смыслу просьбы."}
                if not changed:
                    return {"error": "Напоминание уже обработано или принадлежит другому участнику."}
                reminder_id = None
                return {"ok": True, "action": action}

            async def reply(phrase, *, on_progress):
                await self.record_trace(member["key"] if member else self.owner_key or "local", call_id,
                                        "recognized", "received", {"text": "[код не сохраняется]" if pending_pin > clock() else phrase})
                started = clock()
                async def list_locks(_args):
                    devices = []
                    for entity in allowed_locks:
                        try:
                            row = await self.brain._ha("GET", "/api/states/" + entity)
                        except (RuntimeError, TimeoutError, ClientError):
                            continue
                        devices.append({"entity_id": entity, "name": row.get("attributes", {}).get("friendly_name") or entity,
                                        "state": row.get("state"), "service": GATE_ACTIONS[entity.split(".", 1)[0]]})
                    return {"locks": devices}

                async def request_gate(args):
                    nonlocal pending_pin, digits, gate, gate_name
                    selected = args.get("entity_id") or (allowed_locks[0] if len(allowed_locks) == 1 else "")
                    if not isinstance(selected, str) or selected not in allowed_locks:
                        return {"error": "Выбери назначенный замок по названию из списка.", **await list_locks({})}
                    row = await self.brain._ha("GET", "/api/states/" + selected)
                    if row.get("state") in {"unavailable", "unknown"}:
                        return "Выбранный замок сейчас недоступен."
                    if pending_pin > clock() and gate != selected:
                        return "Сначала заверши ввод кода для уже выбранного замка."
                    gate = selected
                    gate_name = row.get("attributes", {}).get("friendly_name") or "назначенный замок"
                    if not pin:
                        on_progress(f"Открываю «{gate_name}».")
                        return await open_gate()
                    if not re.fullmatch(r"[0-9]+", pin):
                        return "Открытие по телефону недоступно: код должен состоять из цифр."
                    if await asyncio.to_thread(self.personal.pin_locked, caller, datetime.now(timezone.utc)):
                        return "Открытие по телефону временно заблокировано после неверных кодов."
                    digits = ""
                    pending_pin = clock() + 30
                    return {"lock": gate_name, "instruction": "Для открытия нужен код. Попроси набрать его на клавиатуре и нажать решётку."}

                if pending_pin > clock() and re.fullmatch(r"[0-9][0-9 ,\-]*", phrase.strip().rstrip(".!?")):
                    # Spoken digits would be sent to cloud STT and the LLM: accept the PIN only via DTMF.
                    return "Код нужно набрать на клавиатуре телефона и нажать решётку."
                actions = {"end_phone_call": end_call}
                if reminder_id:
                    actions["read_call_reminder"] = read_reminder
                    actions["handle_call_reminder"] = handle_call_reminder
                if allowed_locks:
                    actions["list_assigned_locks"] = list_locks
                    actions["open_assigned_gate"] = request_gate
                answer = await brain.reply(phrase, camera_entity=camera, on_progress=on_progress,
                                           phone_actions=actions)
                await self.record_trace(member["key"] if member else self.owner_key or "local", call_id,
                                        "reply", "ready", {"text": answer.text if isinstance(answer, CallOutput) else answer}, (clock()-started)*1000)
                return answer

            async def play(text):
                nonlocal reminder_id
                if initial_reminder and text == prompt and not reminder_id:
                    return
                started = clock()
                try:
                    await self._say(text)
                except Exception:
                    await self.record_trace(member["key"] if member else self.owner_key or "local", call_id, "playback", "failed", duration_ms=(clock()-started)*1000)
                    raise
                await self.record_trace(member["key"] if member else self.owner_key or "local", call_id, "playback", "sent", duration_ms=(clock()-started)*1000)
                if (reminder_id and text == prompt
                    and (self.modem.call_id, self.modem.call_state) == (call_id, "connected")):
                    self.personal.delivered(reminder_id)
                    reminder_id = None

            await run_dialog(
                self.speech, reply, self.audio.queue, play, self.modem.hangup,
                ("У меня для вас напоминание. Можно озвучить?"
                 if reminder_id else prompt or "Здравствуйте. Это Джарвис. Слушаю вас."),
                background_tools=True, keypad=keypad, on_key=press,
                echo_filter=self.o.get("call_echo_filter", False),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error=str(exc)[:250]
            self.mode="error"
            LOG.exception("Voice session failed call=%s", self.modem.call_id)
            return False
            # Do not hang up just because the speech service fails: leave the call under user control.
        finally:
            self.keypad = None
            if brain is not None:
                self.schedule_call_summary(brain, caller, call_id)
            await self.audio.stop()

    def schedule_call_summary(self, brain, caller, call_id):
        """After a call, ask the LLM for a short summary and post it to HA in the background."""
        if not self.o.get("call_summary", False) or not callable(getattr(brain, "summarize", None)):
            return
        if not getattr(self.brain, "hass_token", None):
            return

        async def run():
            try:
                summary = await brain.summarize()
            except Exception:
                LOG.exception("Call summary failed")
                return
            if not summary:
                return
            member = self.family.get(caller)
            who = member["name"] if member else ("Владелец" if caller == phone_key(self.o.get("allowed_number", ""), self.country) else caller or "неизвестный")
            try:
                await self.brain._ha("POST", "/api/services/persistent_notification/create", {
                    "title": f"JARVIS: звонок ({who})",
                    "message": summary,
                    "notification_id": f"jarvis_call_{call_id}",
                }, timeout=8)
            except (RuntimeError, TimeoutError, ClientError) as exc:
                LOG.warning("Could not post call summary: %s", exc)
            self.fire_ha_event("jarvis_call_summary", {"call_id": call_id, "caller": who, "summary": summary})

        tasks = self.__dict__.setdefault("summary_tasks", set())
        task = asyncio.create_task(run())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def record_trace(self, profile, call_id, stage, status, details=None, duration_ms=None):
        try:
            await asyncio.to_thread(self.timeline.record, profile, str(call_id), stage, status, duration_ms, details)
        except Exception:
            LOG.warning("Could not store conversation trace")

    async def send_telegram_notice(self, target, text):
        payload = {"target": [target], "message": text}
        bot = self.o.get("telegram_photo_target", "")
        if bot:
            payload["entity_id"] = bot
        result = await self.brain._ha("POST", "/api/services/telegram_bot/send_message?return_response", payload, timeout=20)
        chats = result.get("service_response", {}).get("chats", [])
        if not any(chat.get("message_id") for chat in chats):
            raise RuntimeError("Telegram did not confirm delivery")

    async def notify_event(self, request):
        data = await read_json(request)
        member = data.get("member", "")
        profile = self.owner_key if not member else next((row["key"] for row in self.family.values()
                    if row["key"] == member or row["name"].casefold() == str(member).casefold()), None)
        if profile not in self.reminder_numbers:
            raise web.HTTPBadRequest(text="Choose a configured recipient")
        text, source = data.get("text"), data.get("source")
        if not isinstance(text, str) or not 0 < len(text.strip()) <= 500:
            raise web.HTTPBadRequest(text="text must be 1..500 characters")
        camera = data.get("camera")
        if camera is not None:
            selected_member = next((row for row in self.family.values() if row["key"] == profile), None)
            if not isinstance(camera, str) or not self.caller_brain(selected_member).camera_allowed(camera):
                raise web.HTTPBadRequest(text="Camera is not allowed for this recipient")
        urgent = data.get("urgent", False)
        if type(urgent) is not bool:
            raise web.HTTPBadRequest(text="urgent must be boolean")
        try:
            incident = await asyncio.to_thread(self.policies.coalesce, profile, source, {"text": text, "camera": data.get("camera")})
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        await self.record_trace(profile, "event:" + incident["id"], "event", "grouped" if not incident["notify"] else "new", incident)
        if not incident["notify"]:
            return web.json_response({"grouped": True, "incident": incident["id"], "count": incident["count"]})
        now = datetime.now(timezone.utc)
        decision = await asyncio.to_thread(self.policies.decide, profile, now, urgent)
        if not decision["allowed"] or decision["channel"] == "call":
            due = datetime.fromisoformat(decision["defer_until"]) if decision["defer_until"] else now
            context = {"kind": "time", "notification_urgent": urgent, "source": source, "camera": data.get("camera")}
            identifier = await asyncio.to_thread(self.personal.reminder, profile, member or "Владелец", text, due, context)
            return web.json_response({"queued": True, "id": identifier, "incident": incident["id"]})
        try:
            await self.send_telegram_notice(decision["telegram_target"], text)
        except (RuntimeError, ClientError, TimeoutError):
            # Preserve a failed delivery as a bounded requester-scoped reminder.
            identifier = await asyncio.to_thread(self.personal.reminder, profile, member or "Владелец", text, now,
                {"kind": "time", "notification_urgent": urgent, "source": source, "camera": data.get("camera")})
            return web.json_response({"queued": True, "id": identifier})
        return web.json_response({"sent": True, "channel": decision["channel"], "incident": incident["id"]})

    @web.middleware
    async def auth(self, request, handler):
        if request.path == "/health" or request.path.startswith("/q1/shared-photo/"):
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
                "speaker_configured":bool(self.o.get("q1_speaker_device")),
                "speech_configured":self.speech is not None,
                "assistant_configured":bool((profile := active_profile(self.o)) and profile["model"] and
                                            (profile["id"] != "deepseek" or profile["api_key"])),
            }})

    async def dial(self, request):
        data=await request.json()
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="Expected a JSON object")
        return await self.dial_number(
            data.get("number"), data.get("prompt", ""), data.get("camera_entity"), data.get("member"),
        )

    async def dial_number(self, number=None, prompt="", camera=None, member=None):
        owner = self.o.get("allowed_number", "")
        family = getattr(self, "family", {})
        if member:
            if not isinstance(member, str) or number:
                raise web.HTTPBadRequest(text="Select one family recipient")
            wanted = member.strip()
            # Prefer the stable profile key; names are kept as a fallback for older clients.
            profile = next((entry for entry in family.values() if entry.get("key") == wanted), None) or next(
                (entry for entry in family.values() if entry["name"].casefold() == wanted.casefold()), None)
            if not profile:
                raise web.HTTPBadRequest(text="Unknown family recipient")
            number = profile["number"]
        number = number or owner
        country = getattr(self, "country", "7")
        if not isinstance(number, str) or not number or (
            phone_key(number, country) != phone_key(owner, country) and phone_key(number, country) not in family
        ):
            raise web.HTTPForbidden(text="Number not allowlisted")
        prompt=str(prompt).strip()
        if len(prompt)>1200:
            raise web.HTTPBadRequest(text="prompt too long")
        if camera and not self.brain.camera_allowed(camera):
            raise web.HTTPForbidden(text="Camera is not allowlisted")
        if self.modem.call_state != "idle":
            raise web.HTTPConflict(text="Modem is busy or offline")
        pending_prompt = self.next_prompt = (self.modem.call_id + 1, prompt, camera)
        try:
            await self.modem.dial(number)
            return web.json_response({"ok":True,"state":self.modem.call_state})
        except (RuntimeError, ValueError, OSError) as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        finally:
            if self.next_prompt is pending_prompt and (
                self.modem.call_id, self.modem.call_state
            ) != (pending_prompt[0], "dialing"):
                self.next_prompt = None

    async def hangup(self, request):
        try:
            await self.modem.hangup()
        except (RuntimeError, OSError) as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        return web.json_response({"ok":True})

    async def send_dtmf(self, request):
        data = await request.json()
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
        return web.json_response({"ok":True})

    async def chat(self, request):
        data=await read_json(request)
        text=str(data.get("text","")).strip()
        if not text or len(text)>2000:
            raise web.HTTPBadRequest(text="text must be 1..2000 chars")
        response=await self.room_reply(text)
        return web.json_response({"response":response})

    async def room_reply(self, text):
        """Answer a room phrase with JARVIS (room profile) or Home Assistant Assist."""
        if str(self.o.get("room_agent") or "jarvis") != "assist":
            return await self.room_brain().reply(text)
        payload={"text":text,"language":"ru"}
        agent=str(self.o.get("assist_agent_id") or "").strip()
        if agent:
            payload["agent_id"]=agent
        conversation=getattr(self, "assist_conversation", None)
        if conversation and time.monotonic()-conversation[1] < 300:
            payload["conversation_id"]=conversation[0]
        try:
            result=await self.brain._ha("POST","/api/conversation/process",payload,timeout=30)
        except (ClientError, TimeoutError, RuntimeError, ValueError) as exc:
            LOG.warning("Assist request failed: %s",exc)
            return "Home Assistant Assist сейчас недоступен."
        if not isinstance(result, dict):
            return "Assist не вернул ответ."
        if result.get("conversation_id"):
            self.assist_conversation=(result["conversation_id"],time.monotonic())
        speech=((result.get("response") or {}).get("speech") or {}).get("plain") or {}
        return str(speech.get("speech") or "").strip()[:2000] or "Готово."

    async def speak(self, request):
        data=await request.json()
        text=str(data.get("text","")).strip()
        if not text or len(text)>2000:
            raise web.HTTPBadRequest(text="text must be 1..2000 chars")
        if not self.audio.running:
            raise web.HTTPConflict(text="No active PCM audio session")
        await self._say(text)
        return web.json_response({"ok":True})


    async def listen_room(self, seconds):
        device = self.o.get("q1_mic_device")
        if not device or not self.speech:
            return {"error": "Микрофон Q1 или SpeechKit не настроен"}
        async with self.mic_lock:
            pcm = await capture_pcm(device, seconds)
        level = rms(pcm)
        if not pcm or level == 0:
            return {"seconds": seconds, "rms": level, "error": "Микрофон не передал звуковой сигнал"}
        phrase = await self.speech.recognize(pcm)
        return {"seconds": seconds, "rms": level, "speech": phrase}

    async def share_photo(self, target, picture, mime, recipient=None):
        ticket = secrets.token_urlsafe(32)
        self.shared_photos[ticket] = (picture, mime)
        try:
            result = await self.brain._ha("POST", "/api/services/telegram_bot/send_photo?return_response", {
                "entity_id": target,
                **({"target": [recipient]} if recipient else {}),
                "url": f"http://{socket.gethostname()}:8099/q1/shared-photo/{ticket}",
            }, timeout=25)
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
        return web.Response(body=picture, content_type=mime, headers={"Cache-Control": "no-store"})

    async def q1_listen(self, request):
        if not self.speech:
            raise web.HTTPConflict(text="SpeechKit API key missing")
        if not self.o.get("q1_mic_device"):
            raise web.HTTPConflict(text="No Q1 microphone ALSA device configured")
        data=await read_json(request)
        try:
            seconds=min(30,max(3,int(data.get("seconds",12))))
        except (TypeError, ValueError) as exc:
            raise web.HTTPBadRequest(text="seconds must be an integer") from exc
        async with self.mic_lock:
            phrase=await recognize_microphone_live(self.speech,self.o["q1_mic_device"],seconds)
        if not phrase:
            return web.json_response({"recognized":"","response":""})
        answer=await self.room_reply(phrase)
        if self.o.get("q1_speaker_device"):
            async with self.speaker_lock:
                await self.media.stop()
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
            await self.media.stop()
            await play_stream(self.o["q1_speaker_device"],self.speech.synthesize(text))
        return web.json_response({"ok":True})


    async def q1_probe(self,request):
        result=await diagnostics(self.o.get("q1_video_device",""))
        result["modem"]={"state":self.modem.call_state,
                         "model":self.modem.model,"signal":self.modem.signal,
                         "configured_at_port":self.modem.port}
        return web.json_response(result)

    async def capture_camera(self, device=None):
        if device is None:
            device = self.o.get("q1_video_device", "")
        if not device:
            raise RuntimeError("Configure q1_video_device")
        async with self.camera_lock:
            now = asyncio.get_running_loop().time
            if self.camera_frame and self.camera_frame[0] == device and now() - self.camera_frame[1] < 1:
                return self.camera_frame[2]
            content = await snapshot(device)
            self.camera_frame = (device, now(), content)
            return content

    async def q1_snapshot(self,request):
        try:
            content=await self.capture_camera()
        except (ValueError,RuntimeError) as exc:
            raise web.HTTPConflict(text=str(exc)[:250]) from exc
        return web.Response(body=content,content_type="image/jpeg",
                            headers={"Cache-Control":"no-store"})

    async def q1_camera_setup(self,request):
        if not self.o.get("q1_video_device"):
            raise web.HTTPConflict(text="Configure q1_video_device")
        async with self.camera_lock:
            result = await apply_original_camera_controls(self.o["q1_video_device"])
            self.camera_frame = None
        return web.json_response(result)

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
            async with self.speaker_lock:
                await self.media.play(self.http, str(data.get("url","")),
                    self.o.get("q1_speaker_device",""),
                    self.o.get("media_allowed_hosts",["homeassistant","homeassistant.local"]))
        except (ValueError,RuntimeError) as exc:
            raise web.HTTPBadRequest(text=str(exc))
        return web.json_response({"ok":True,"playing":True})

    async def q1_stop_media(self,request):
        async with self.speaker_lock:
            await self.media.stop()
        return web.json_response({"ok":True})

    async def microphone_status(self, _request):
        return web.json_response(self.microphone.status())

    async def microphone_switch(self, request):
        data = await request.json()
        if not isinstance(data, dict) or not isinstance(data.get("enabled"), bool):
            raise web.HTTPBadRequest(text="Expected enabled: true or false")
        try:
            self.microphone.set_enabled(data["enabled"])
        except ValueError as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        return web.json_response(self.microphone.status())

    async def q1_mic_stream(self,request):
        device=self.o.get("q1_mic_device","")
        if not device:
            raise web.HTTPConflict(text="No Q1 microphone configured")
        try:
            seconds=min(120,max(1,int(request.query.get("seconds","15"))))
        except ValueError:
            raise web.HTTPBadRequest(text="seconds must be integer")
        if self.mic_lock.locked() and not self.microphone.sampling:
            raise web.HTTPConflict(text="Microphone already in use")
        async with self.mic_lock:
            device, env = audio_route(device, "capture")
            proc=await asyncio.create_subprocess_exec(
                "arecord","-q","-D",device,"-f","S16_LE","-r","16000","-c","1","-t","raw",
                "-d",str(seconds),
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,env=env)
            response=web.StreamResponse(status=200,headers={
                "Content-Type":"application/octet-stream",
                "X-Audio-Format":"s16le;rate=16000;channels=1",
                "Cache-Control":"no-store"})
            try:
                try:
                    frame = await asyncio.wait_for(proc.stdout.read(3200), timeout=4)
                except TimeoutError as exc:
                    raise web.HTTPBadGateway(text="Microphone did not produce PCM") from exc
                if not frame:
                    raise web.HTTPBadGateway(
                        text="Microphone did not produce PCM. Check /q1/probe and q1_mic_device."
                    )
                await response.prepare(request)
                while frame:
                    await response.write(frame)
                    frame = await asyncio.wait_for(proc.stdout.read(3200), timeout=4)
            except (ConnectionResetError, TimeoutError):
                pass
            finally:
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                await proc.communicate()
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
        data=await read_json(request)
        try:
            seconds=min(30,max(3,int(data.get("seconds",12))))
        except (TypeError, ValueError) as exc:
            raise web.HTTPBadRequest(text="seconds must be an integer") from exc
        if self.mic_lock.locked() and not self.microphone.sampling:
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
                timeout=ClientTimeout(total=8)) as resp:
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
            ("POST","/notify",self.notify_event),("POST","/dial",self.dial),("POST","/hangup",self.hangup),
            ("POST","/answer",self.answer),("POST","/chat",self.chat),
            ("POST","/dtmf",self.send_dtmf),
            ("POST","/speak",self.speak),
            ("POST","/q1/listen",self.q1_listen),("POST","/q1/speak",self.q1_speak),
            ("GET","/q1/probe",self.q1_probe),
            ("GET","/q1/snapshot",self.q1_snapshot),
            ("GET","/q1/shared-photo/{ticket}",self.shared_photo),
            ("POST","/q1/camera/apply_original",self.q1_camera_setup),
            ("GET","/q1/gps",self.q1_gps),
            ("POST","/q1/gps/start",self.q1_gps_start),
            ("POST","/q1/led",self.q1_led),
            ("POST","/q1/play_media",self.q1_play_media),
            ("POST","/q1/stop_media",self.q1_stop_media),
            ("GET","/q1/microphone/stream",self.q1_mic_stream),
            ("GET","/q1/microphone",self.microphone_status),
            ("POST","/q1/microphone",self.microphone_switch),
            ("POST","/q1/transcribe",self.q1_transcribe)):
            app.router.add_route(method,path,fn)
        return app


async def main():
    o=load_options()
    service=Server(o)
    await service.setup()
    runner=web.AppRunner(service.app())
    panel = web.AppRunner(SetupPanel(service).app())
    try:
        await runner.setup()
        await web.TCPSite(runner, host="0.0.0.0", port=8099).start()
        await panel.setup()
        await web.TCPSite(panel, host="0.0.0.0", port=8098).start()
        await service.discover_companion()
        LOG.info("JARVIS API listening on 8099 and HA ingress on 8098")
        stop=asyncio.Event()
        loop=asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(signum, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        await stop.wait()
        LOG.info("Shutdown requested, releasing modem and audio")
    finally:
        await panel.cleanup()
        await runner.cleanup()
        await service.close()


if __name__=="__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
