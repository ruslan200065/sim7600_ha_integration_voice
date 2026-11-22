"""Build and run the JARVIS Voice Home Assistant add-on."""

import asyncio
import json
import logging
import os
import secrets
import signal
from collections import deque
from contextlib import AsyncExitStack
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import ClientError, ClientSession, web
from audio import PCMTransport
from automation_service import AutomationService
from autonomy import Autonomy
from brain import Brain
from call_outbox import CallOutbox
from camera_panel import CameraPanel
from device_service import DeviceService
from escalation import EscalationPolicies
from event_journal import EventJournal
from family import family_profiles, lock_entities, profile_key
from family_inbox import FamilyInbox
from history import RETENTION_DAYS, History
from http_api import HttpApi
from knowledge import Knowledge
from microphone import MicrophoneMonitor
from modem import Modem
from notifications import NotificationPolicies
from personal import Personal
from q1_media import Q1Media
from request_helpers import phone_key
from resilience import OpenAICompatibleSpeech, SpeechRouter
from rules import Rules
from setup_panel import SetupPanel
from speech import SpeechKit
from speech_cache import SpeechCache
from telephony_service import TelephonyService
from timeline import Timeline

LOG = logging.getLogger(__name__)


def load_options():
    path = Path(os.environ.get("OPTIONS_PATH", "/data/options.json"))
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    # Container-only development; never check credentials into git.
    return {
        k.lower(): v
        for k, v in os.environ.items()
        if k.startswith(("YANDEX_", "DEEPSEEK_"))
    }


class Server(TelephonyService, AutomationService, DeviceService, HttpApi):
    def __init__(self, options):
        self.o = options
        self.http = None
        self.closing = False
        self.ha_event_tasks = set()
        self.summary_tasks = set()
        self.call_event_lock = asyncio.Lock()
        self.modem = Modem(
            options.get("at_port", "/dev/ttyUSB2"),
            options.get("serial_baud", 115200),
            self.event,
            options.get("max_calls_per_hour", 4),
        )
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
        self.call_events = deque(maxlen=128)
        self.call_event_stream = secrets.token_hex(8)
        self.call_event_sequence = 0
        self.incoming_call_event = None
        self.incoming_call_timer = None
        self.api_token = options.get("api_token", "")
        if not self.api_token:
            token_path = Path("/data/companion_api_token")
            if token_path.exists():
                self.api_token = token_path.read_text(encoding="ascii").strip()
            else:
                self.api_token = secrets.token_urlsafe(40)
                token_path.parent.mkdir(parents=True, exist_ok=True)
                token_path.write_text(self.api_token, encoding="ascii")
                token_path.chmod(0o600)
        self.media = Q1Media()
        self.mic_lock = asyncio.Lock()
        self.speaker_lock = asyncio.Lock()
        self.camera_lock = asyncio.Lock()
        self.camera_frame = None
        self.speaking = asyncio.Lock()
        self.next_prompt = None
        self.shared_photos = {}
        data_dir = Path(os.environ.get("OPTIONS_PATH", "/data/options.json")).parent
        self.history = History(
            data_dir / "history.sqlite3",
            retention_days=options.get("history_retention_days", RETENTION_DAYS),
        )
        self.camera_settings_path = data_dir / "camera_settings.json"
        self.camera_panel = CameraPanel(self)
        self.call_outbox_path = data_dir / "call_outbox.sqlite3"
        self.call_outbox = None
        self.personal = Personal(data_dir / "personal.sqlite3")
        self.knowledge = Knowledge(data_dir / "knowledge.sqlite3")
        self.policies = NotificationPolicies(data_dir / "notifications.sqlite3")
        self.timeline = Timeline(data_dir / "timeline.sqlite3")
        self.autonomy = Autonomy(data_dir / "autonomy.sqlite3")
        self.test_rules = Rules(data_dir / "simulations.sqlite3")
        self.inbox = FamilyInbox(data_dir / "family_inbox.sqlite3")
        self.events_journal = EventJournal(data_dir / "events.sqlite3")
        self.escalation = EscalationPolicies(data_dir / "escalation.sqlite3")
        self.discovery_task = None
        self.events_task = None
        self.speech_cache = SpeechCache(data_dir / "speech_cache")
        self.microphone = MicrophoneMonitor(
            options.get("q1_mic_device", ""),
            self.mic_lock,
            data_dir / "microphone_monitor",
        )
        self.microphone_task = None
        self.country = str(options.get("country_code", "7") or "")
        self.family = {
            phone_key(number, self.country): member
            for number, member in family_profiles(
                options.get("family_members", [])
            ).items()
        }
        self.family.pop(phone_key(options.get("allowed_number"), self.country), None)
        self.owner_locks = lock_entities(options.get("owner_locks", []))
        self.gates = frozenset(
            self.owner_locks
            + [entity for member in self.family.values() for entity in member["locks"]]
        )
        self.family_brains = {}
        self.reminder_task = None
        self.registry_task = None
        self.reminder_call = None
        owner = self.o.get("allowed_number") or ""
        self.owner_key = profile_key("Владелец", owner) if owner else None
        self.reminder_numbers = {
            member["key"]: member["number"] for member in self.family.values()
        }
        if self.owner_key:
            self.reminder_numbers[self.owner_key] = owner
        self.contacts = {
            member["key"]: member["name"] for member in self.family.values()
        }
        if self.owner_key:
            self.contacts[self.owner_key] = options.get("owner_name") or "Владелец"
        self.sms_task = None
        self.sms_wake = asyncio.Event()
        self.sms_status = {}
        # The modem reader must never wait for SQLite: history is written in the background.
        self.history_queue = asyncio.Queue()
        self.history_task = None

    async def setup(self):
        await self.history.start()
        self.call_outbox = await asyncio.to_thread(CallOutbox, self.call_outbox_path)
        self.call_event_stream = self.call_outbox.stream
        self.history_task = asyncio.create_task(self.write_history())
        self.http = ClientSession()
        self.brain = Brain(
            self.o,
            self.http,
            self.capture_camera,
            self.listen_room,
            self.share_photo,
            knowledge=self.knowledge,
            policies=self.policies,
            timeline=self.timeline,
            autonomy=self.autonomy,
            test_rules=self.test_rules,
            inbox=self.inbox,
            events=self.events_journal,
            contacts=self.contacts,
            personal=self.personal,
            profile=self.owner_key or "local",
            profile_name=self.o.get("owner_name") or "Владелец",
            restricted_entities=self.gates,
        )
        if self.brain.hass_token:
            try:
                config = await self.brain._ha("GET", "/api/config", timeout=3)
                self.brain.timezone = ZoneInfo(config["time_zone"])
            except (
                RuntimeError,
                OSError,
                ValueError,
                KeyError,
                TimeoutError,
                ZoneInfoNotFoundError,
                ClientError,
            ):
                LOG.warning("Home Assistant time zone unavailable")
        self.reminder_task = asyncio.create_task(self.deliver_reminders())
        self.registry_task = asyncio.create_task(self.brain.watch_registry())
        if self.o.get("yandex_api_key"):
            self.speech = SpeechKit(
                self.o["yandex_api_key"],
                self.o.get("yandex_voice", "denis"),
                self.o.get("yandex_role", "neutral"),
                cache=self.speech_cache
                if self.o.get("speech_cache_enabled", True)
                else None,
            )
        if self.o.get("speech_fallback_enabled"):
            fallback = OpenAICompatibleSpeech(
                self.http,
                {
                    key: self.o.get("speech_fallback_" + key, "")
                    for key in (
                        "base_url",
                        "api_key",
                        "stt_model",
                        "tts_model",
                        "voice",
                    )
                },
            )
            self.speech = (
                SpeechRouter(self.speech, fallback, self.speech_fallback_used)
                if self.speech
                else fallback
            )
        self.events_task = asyncio.create_task(self.watch_home_events())
        self.microphone_task = asyncio.create_task(self.microphone.run())
        await self.modem.start()
        self.sms_task = asyncio.create_task(self.sync_sms())

    async def close(self):
        if self.closing:
            return
        self.closing = True
        # Release independent resources even when another cleanup step fails.
        async with AsyncExitStack() as cleanup:
            if self.http:
                cleanup.push_async_callback(self.http.close)
            cleanup.push_async_callback(self._close_speech_and_workflows)
            cleanup.push_async_callback(self._stop_event_delivery)
            cleanup.push_async_callback(self._stop_history)
            cleanup.push_async_callback(self.modem.stop)
            cleanup.push_async_callback(self.media.stop)
            cleanup.push_async_callback(self.audio.stop)
            await self._stop_call_and_polling()

    async def _stop_call_and_polling(self):
        tasks = [
            task
            for task in (
                self.discovery_task,
                self.events_task,
                self.registry_task,
                self.microphone_task,
                self.reminder_task,
                self.sms_task,
                self.auto_answer_task,
                self.pcm_lost_task,
                self.session,
                *self.summary_tasks,
            )
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.modem.call_state not in ("idle", "offline"):
            try:
                await asyncio.wait_for(self.modem.hangup(), 5)
            except Exception as exc:
                LOG.warning("Hangup on shutdown failed: %s", exc)

    async def _stop_history(self):
        if self.history_task:
            try:
                await asyncio.wait_for(self.history_queue.join(), 3)
            except TimeoutError:
                LOG.warning("Some call history was not saved before shutdown")
            self.history_task.cancel()
            await asyncio.gather(self.history_task, return_exceptions=True)

    async def _stop_event_delivery(self):
        if self.incoming_call_timer is not None:
            self._publish_incoming_call()
        tasks = tuple(self.ha_event_tasks)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=2)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _close_speech_and_workflows(self):
        async with AsyncExitStack() as cleanup:
            if self.speech:
                cleanup.push_async_callback(self.speech.close)
            for brain in (
                self.brain,
                *self.family_brains.values(),
                getattr(self, "_room_brain", None),
            ):
                if brain and getattr(brain, "features", None):
                    cleanup.push_async_callback(brain.features.workflows.close)


async def main():
    service = Server(load_options())
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(service.close)
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(signum, stopping.set)
            except (NotImplementedError, RuntimeError):
                continue
            cleanup.callback(loop.remove_signal_handler, signum)

        await service.setup()
        for app, port in ((service.app(), 8099), (SetupPanel(service).app(), 8098)):
            runner = web.AppRunner(app, shutdown_timeout=5)
            cleanup.push_async_callback(runner.cleanup)
            await runner.setup()
            await web.TCPSite(runner, host="0.0.0.0", port=port).start()
        service.discovery_task = asyncio.create_task(
            service.retry_companion_discovery()
        )
        LOG.info("JARVIS API listening on 8099 and HA ingress on 8098")
        await stopping.wait()
        LOG.info("Shutdown requested, releasing modem and audio")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    asyncio.run(main())
