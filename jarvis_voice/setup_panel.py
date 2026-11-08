"""Administrator-only Home Assistant ingress for settings and hardware checks."""

import asyncio
from datetime import datetime, timezone
import hashlib
import hmac
import io
import json
import logging
import math
import os
import re
import secrets
import struct
import wave
from pathlib import Path

from aiohttp import ClientError, ClientTimeout, web
from audio import rms
from modem import NUMBER
from workflows import validate_steps
from brain import SAFE_SERVICES
from family import family_profiles, lock_entities
from llm import DEEPSEEK_URL, EFFORTS, active_profile, base_url, list_models, merge_profiles
from q1_audio import capture_pcm, play_pcm
from q1_hardware import VIDEO_LINK, VIDEO_PATH, list_serial, list_video, probe_audio
from q1_protocol import gps_status

LOG = logging.getLogger(__name__)
ASSETS = Path(__file__).with_name("web")
SECRET_KEYS = {"yandex_api_key", "deepseek_api_key", "gate_pin"}
TEXT_FIELDS = {
    "at_port": 256,
    "pcm_port": 256,
    "q1_video_device": 256,
    "q1_mic_device": 128,
    "q1_speaker_device": 128,
    "allowed_number": 17,
    "owner_name": 60,
    "deepseek_model": 256,
    "deepseek_effort": 16,
    "llm_active": 64,
    "yandex_voice": 80,
    "yandex_role": 80,
    "system_prompt": 6000,
    "camera_entity": 256,
    "home_tts_entity": 256,
    "telegram_photo_target": 256,
    "reminder_calendar": 256,
    "reminder_person": 256,
    "country_code": 3,
    "room_agent": 16,
    "assist_agent_id": 256,
}
INTEGER_FIELDS = {
    "serial_baud": (1200, 921600),
    "max_calls_per_hour": (1, 20),
    "call_playback_volume": (10, 100),
    "llm_daily_token_limit": (0, 10_000_000),
}
BOOLEAN_FIELDS = {"auto_answer", "observe_all_entities", "control_all_entities", "speech_cache_enabled", "call_echo_filter",
                  "confirm_risky_actions", "ha_events", "call_summary", "stream_replies"}
OPTION_FIELDS = (
    set(TEXT_FIELDS)
    | set(INTEGER_FIELDS)
    | BOOLEAN_FIELDS
    | {
        "allowed_entities",
        "owner_locks",
        "observed_entities",
        "room_entities",
        "allow_domains",
        "family_members",
        "llm_profiles",
    }
)
AUDIO_FIELDS = ("audio_input", "audio_output")
SERIAL_PATH = re.compile(
    r"/dev/(?:tty[A-Za-z0-9]+|modemAT|serial/by-id/[A-Za-z0-9_.+:-]+)"
)
ENTITY_ID = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")


def validate_changes(changes, current):
    """Accept only fields exposed in the panel and preserve untouched options."""
    if not isinstance(changes, dict) or changes.keys() - OPTION_FIELDS - SECRET_KEYS:
        raise ValueError("Переданы неизвестные настройки.")
    options = current.copy()
    for key, value in changes.items():
        if key in TEXT_FIELDS or key in SECRET_KEYS:
            if not isinstance(value, str) or (key != "gate_pin" and len(value) > TEXT_FIELDS.get(key, 512)):
                raise ValueError(f"Неверное значение поля {key}.")
            value = value.strip()
            allowed_whitespace = "\n\r\t" if key == "system_prompt" else ""
            if any(ord(char) < 32 and char not in allowed_whitespace for char in value):
                raise ValueError(f"Недопустимые символы в поле {key}.")
        elif key in INTEGER_FIELDS:
            lo, hi = INTEGER_FIELDS[key]
            if type(value) is not int or not lo <= value <= hi:
                raise ValueError(f"{key}: допустимо от {lo} до {hi}.")
        elif key in BOOLEAN_FIELDS and not isinstance(value, bool):
            raise ValueError(f"Неверное значение поля {key}.")
        elif key == "owner_locks":
            value = lock_entities(value)
        elif key == "llm_profiles":
            value = merge_profiles(value, current.get("llm_profiles", []))
        elif key == "family_members":
            family_profiles(value)
        elif key in {"allowed_entities", "observed_entities", "room_entities"}:
            if (
                not isinstance(value, list)
                or len(value) > 100
                or any(
                    not isinstance(item, str) or not ENTITY_ID.fullmatch(item)
                    for item in value
                )
            ):
                raise ValueError("Укажите до 100 идентификаторов вида light.room.")
            value = list(dict.fromkeys(value))
        elif key == "allow_domains":
            if not isinstance(value, list) or any(
                not isinstance(item, str) or item not in SAFE_SERVICES for item in value
            ):
                raise ValueError("Передан неизвестный домен управления.")
            value = list(dict.fromkeys(value))
        options[key] = value

    if options.get("deepseek_effort", "off") not in EFFORTS:
        raise ValueError("Неверный режим рассуждения DeepSeek.")
    profile = active_profile(options)
    if not profile or (profile["id"] != "deepseek" and not profile["model"]):
        raise ValueError("Выбери модель в активном профиле LLM.")

    for key in ("at_port", "pcm_port"):
        if key in changes and not (key == "pcm_port" and not options[key]):
            if not SERIAL_PATH.fullmatch(options[key]):
                raise ValueError(f"{key}: выберите последовательный порт в /dev.")
    if (
        options.get("pcm_port")
        and options.get("at_port")
        and (
            os.path.realpath(options["at_port"])
            == os.path.realpath(options["pcm_port"])
        )
    ):
        raise ValueError("Для команд модема и звука звонка нужны разные порты.")
    device = changes.get("q1_video_device", "").strip()
    if device and not (VIDEO_PATH.fullmatch(device) or VIDEO_LINK.fullmatch(device)):
        raise ValueError("Выберите камеру /dev/videoN или путь /dev/v4l/by-id.")
    number = options.get("allowed_number") or ""
    if number and not NUMBER.fullmatch(number):
        raise ValueError(
            "Введите номер в международном формате, например +79991234567."
        )
    family = family_profiles(options.get("family_members", []))
    if number and number.lstrip("+") in family:
        raise ValueError("Номер владельца уже добавлен отдельно.")
    country = options.get("country_code", "7") or ""
    if not re.fullmatch(r"[0-9]{0,3}", str(country)):
        raise ValueError("Код страны: до 3 цифр без плюса, например 7.")
    if (options.get("room_agent") or "jarvis") not in ("jarvis", "assist"):
        raise ValueError("Агент для голоса из комнаты: jarvis или assist.")
    agent = options.get("assist_agent_id", "") or ""
    if agent and not re.fullmatch(r"[A-Za-z0-9_.\-]{1,256}", agent):
        raise ValueError("ID агента Assist: например conversation.home_assistant.")
    pin = options.get("gate_pin", "")
    if pin and not re.fullmatch(r"[0-9]+", pin):
        raise ValueError("Код замка должен состоять из цифр; оставьте пустым для открытия без кода.")
    calendar = options.get("reminder_calendar", "")
    if calendar and (not ENTITY_ID.fullmatch(calendar) or not calendar.startswith("calendar.")):
        raise ValueError("Укажите календарь Home Assistant вида calendar.name.")
    person = options.get("reminder_person", "")
    if person and (not ENTITY_ID.fullmatch(person) or not person.startswith("person.")):
        raise ValueError("Укажите свою сущность Home Assistant вида person.name.")
    calendars = [member["calendar"] for member in family.values() if member["calendar"]]
    if calendar:
        calendars.append(calendar)
    if len(calendars) != len(set(calendars)):
        raise ValueError("Для каждого участника выберите отдельный календарь напоминаний.")
    if options.get("auto_answer") and not (number or family):
        raise ValueError("Укажите разрешённый номер для автоответа.")
    for key, domain in (("home_tts_entity", "tts"), ("telegram_photo_target", "notify")):
        entity = changes.get(key, "").strip()
        if entity and (not ENTITY_ID.fullmatch(entity) or not entity.startswith(domain + ".")):
            raise ValueError(f"{key}: укажите сущность {domain}.*.")
    camera = changes.get("camera_entity", "").strip()
    if camera and not (camera.startswith("camera.") and ENTITY_ID.fullmatch(camera)):
        raise ValueError("Укажите идентификатор камеры вида camera.front_door.")
    return options


def pulse_devices(report, key):
    rows = []
    for line in report.get(key, "").splitlines():
        fields = line.split("\t")
        if (
            len(fields) > 1
            and fields[0].isdigit()
            and not fields[1].endswith(".monitor")
        ):
            rows.append(fields[1])
    return rows


class SetupPanel:
    def __init__(self, service):
        self.service = service
        self.token = os.environ.get("SUPERVISOR_TOKEN", "")
        self.revision_key = secrets.token_bytes(32)
        self.lock = asyncio.Lock()
        self.boot_id = secrets.token_hex(8)
        self.audio_pending = False
        self.restart_task = None

    @web.middleware
    async def authorize(self, request, handler):
        if request.remote != "172.30.32.2" or not request.headers.get(
            "X-Remote-User-Id"
        ):
            raise web.HTTPForbidden()
        if request.method not in ("GET", "HEAD") and (
            request.headers.get("X-Jarvis-Setup") != "1"
            or request.headers.get("Sec-Fetch-Site") == "cross-site"
        ):
            raise web.HTTPForbidden()
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            self.secure_headers(exc)
            raise
        return self.secure_headers(response)

    @staticmethod
    def secure_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self' blob:; media-src 'self' blob:; connect-src 'self'; "
            "frame-ancestors 'self'; base-uri 'self'"
        )
        return response

    def call_active(self):
        service = self.service
        return (
            service.modem.call_state not in {"idle", "offline"}
            or service.mic_lock.locked()
            or service.speaker_lock.locked()
            or service.media.playing
        )

    async def supervisor(self, method, path, data=None, timeout=10):
        if not self.token:
            raise web.HTTPServiceUnavailable(text="Supervisor недоступен.")
        try:
            async with self.service.http.request(
                method,
                "http://supervisor/addons/self/" + path,
                headers={"Authorization": "Bearer " + self.token},
                json=data,
                timeout=ClientTimeout(total=timeout),
                allow_redirects=False,
            ) as response:
                response.raise_for_status()
                result = await response.json()
                if result.get("result") != "ok":
                    raise web.HTTPBadGateway(text="Supervisor отклонил запрос.")
                return result.get("data") or {}
        except (ClientError, TimeoutError, ValueError) as exc:
            raise web.HTTPBadGateway(
                text="Не удалось выполнить запрос к Supervisor."
            ) from exc

    def revision(self, info):
        data = {key: info.get(key) for key in ("options", *AUDIO_FIELDS)}
        serialized = json.dumps(data, sort_keys=True).encode()
        return hmac.new(self.revision_key, serialized, hashlib.sha256).hexdigest()

    async def state(self, _request):
        info, audio = await asyncio.gather(
            self.supervisor("GET", "info"), probe_audio(), return_exceptions=True
        )
        if isinstance(info, BaseException):
            raise info
        if isinstance(audio, BaseException):
            LOG.warning("Audio probe failed: %s", audio)
            audio = {}
        options = info["options"]
        active = self.service.o
        return web.json_response(
            {
                "version": info.get("version"),
                "boot": self.boot_id,
                "options": {
                    key: options[key] for key in OPTION_FIELDS if key in options and key != "llm_profiles"
                },
                "llm_profiles": [
                    {**{field: p[field] for field in ("id", "name", "base_url", "model", "effort")},
                     "has_key": bool(p.get("api_key"))}
                    for p in options.get("llm_profiles", [])
                ],
                "keys": {key: bool(options.get(key)) for key in SECRET_KEYS},
                "audio": {key: info.get(key) for key in AUDIO_FIELDS},
                "devices": {
                    "serial": list_serial(),
                    "video": list_video(),
                    "inputs": pulse_devices(audio, "pulseaudio_sources"),
                    "outputs": pulse_devices(audio, "pulseaudio_sinks"),
                },
                "modem": self.service.modem.call_state,
                "revision": self.revision(info),
                "pending": self.audio_pending
                or any(
                    options.get(key) != active.get(key)
                    for key in OPTION_FIELDS | SECRET_KEYS
                    if not str(options.get(key)).startswith("!secret ")
                ),
            }
        )

    async def save(self, request):
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("Ожидался объект настроек.")
            audio = data.get("audio", {})
            if not isinstance(audio, dict) or audio.keys() - set(AUDIO_FIELDS):
                raise ValueError("Неизвестное аудиоустройство.")
            for value in audio.values():
                if value is not None and (
                    not isinstance(value, str)
                    or not re.fullmatch(r"[A-Za-z0-9_.:+-]{1,256}", value)
                ):
                    raise ValueError("Неверное имя устройства PulseAudio.")
            async with self.lock:
                info = await self.supervisor("GET", "info")
                if data.get("revision") != self.revision(info):
                    raise web.HTTPConflict(
                        text="Настройки изменились. Обновите страницу."
                    )
                options = validate_changes(data.get("options", {}), info["options"])
                changed = options != info["options"] or any(
                    info.get(key) != value for key, value in audio.items()
                )
                if changed:
                    await self.supervisor(
                        "POST", "options", {"options": options, **audio}
                    )
                    self.audio_pending |= any(
                        info.get(key) != value for key, value in audio.items()
                    )
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        return web.json_response({"changed": changed})

    async def restart(self, _request):
        async with self.lock:
            if self.call_active():
                raise web.HTTPConflict(
                    text="Дождитесь окончания звонка или работы со звуком."
                )
            if not self.restart_task or self.restart_task.done():
                self.restart_task = asyncio.create_task(self.restart_addon())
        return web.json_response({"restarting": True}, status=202)

    async def restart_addon(self):
        await asyncio.sleep(0.5)
        async with self.lock:
            if self.call_active():
                LOG.warning("Restart postponed: a call or audio session started")
                return
        try:
            await self.supervisor("POST", "restart", timeout=45)
        except web.HTTPException as exc:
            LOG.warning("Could not restart add-on from ingress: %s", exc.status)

    def personal_profiles(self):
        service = self.service
        return {
            service.owner_key or "local": service.o.get("owner_name") or "Владелец",
            **{member["key"]: member["name"] for member in service.family.values()},
        }

    async def personal(self, request):
        profiles = self.personal_profiles()
        profile = request.query.get("profile") or next(iter(profiles))
        if profile not in profiles:
            raise web.HTTPNotFound(text="Профиль не найден.")
        data = await asyncio.to_thread(self.service.personal.dashboard, profile)
        knowledge = getattr(self.service, "knowledge", None)
        if knowledge:
            metadata = {row["text"]: row for row in await asyncio.to_thread(knowledge.memories, profile)}
            for note in data["notes"]:
                note.update({**metadata.get(note["text"], {}), "owner_name": profiles[profile]})
        return web.json_response({**data, "profile": profile,
                                  "profiles": [{"id": key, "name": name} for key, name in profiles.items()]})

    async def operations(self, request):
        profiles = self.personal_profiles()
        profile = request.query.get("profile") or next(iter(profiles))
        if profile not in profiles:
            raise web.HTTPNotFound(text="Профиль не найден.")
        policies = getattr(self.service, "policies", None)
        timeline = getattr(self.service, "timeline", None)
        if policies is None or timeline is None:
            raise web.HTTPServiceUnavailable(text="Службы задач ещё не готовы.")
        member = next((item for item in self.service.family.values() if item["key"] == profile), None)
        brain = self.service.caller_brain(member) if member else self.service.brain
        manager = getattr(getattr(brain, "features", None), "workflows", None)
        try:
            if request.method == "PATCH":
                data = await request.json()
                if not isinstance(data, dict) or set(data) != {"policy"}:
                    raise ValueError("Ожидались настройки уведомлений.")
                policy = await asyncio.to_thread(policies.set, profile, data["policy"])
                return web.json_response({"policy": policy})
            if request.method == "POST":
                data = await request.json()
                if not isinstance(data, dict) or manager is None:
                    raise ValueError("Задачи этого профиля недоступны.")
                if data.get("action") == "cancel_task" and set(data) == {"action", "id"}:
                    if not isinstance(data["id"], str):
                        raise ValueError("Укажите задачу.")
                    return web.json_response(manager.cancel(data["id"]))
                if data.get("action") == "delete_scenario" and set(data) == {"action", "name"}:
                    return web.json_response({"deleted": await asyncio.to_thread(manager.delete, data["name"])})
                if data.get("action") == "save_scenario" and set(data) == {"action", "name", "steps"}:
                    steps = validate_steps(data["steps"])
                    for step in steps:
                        args = step.get("arguments", {})
                        entity = args.get("entity_id")
                        if entity and (not isinstance(entity, str) or not brain._visible(entity)):
                            raise ValueError("Устройство недоступно этому участнику.")
                        if step["tool"] in {"call_ha_service", "get_entity_state", "announce"} and not entity:
                            raise ValueError("Выберите устройство для каждого шага.")
                        if step["tool"] == "call_ha_service" and args.get("service") not in brain._control_actions(entity):
                            raise ValueError("Действие недоступно этому участнику.")
                        if step["tool"] == "announce" and not brain._control_actions(entity):
                            raise ValueError("Оповещение недоступно этому участнику.")
                        camera = args.get("camera")
                        if step["tool"] in {"see_camera", "send_camera_photo"} and camera and (
                                not isinstance(camera, str) or not brain.camera_allowed(camera)):
                            raise ValueError("Камера недоступна этому участнику.")
                        # A stored scenario is a plan, never a persistent confirmation.
                        args.pop("confirmed", None)
                    saved = await asyncio.to_thread(manager.save, data["name"], steps)
                    return web.json_response(saved)
                raise ValueError("Неизвестное действие с задачей.")
            offset = int(request.query.get("offset", 0))
            policy, trace, scenarios = await asyncio.gather(
                asyncio.to_thread(policies.get, profile),
                asyncio.to_thread(timeline.list, profile, limit=100, offset=offset),
                asyncio.to_thread(manager.list_scenarios) if manager else asyncio.sleep(0, result=[]),
            )
            return web.json_response({"profile": profile,
                "profiles": [{"id": key, "name": value} for key, value in profiles.items()],
                "policy": policy, "timeline": trace,
                "tasks": manager.list_tasks() if manager else [], "scenarios": scenarios})
        except (ValueError, TypeError, OverflowError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc

    async def personal_edit(self, request):
        profile = request.query.get("profile")
        kind, identifier = request.match_info["kind"], request.match_info["id"]
        if profile not in self.personal_profiles() or kind not in {"notes", "preferences", "reminders"}:
            raise web.HTTPNotFound(text="Запись не найдена.")
        store = self.service.personal
        knowledge = getattr(self.service, "knowledge", None)
        old_note = None
        if kind == "notes" and knowledge:
            current = await asyncio.to_thread(store.dashboard, profile)
            old_note = next((row["text"] for row in current["notes"] if str(row["id"]) == str(identifier)), None)
        if request.method == "DELETE":
            changed = await asyncio.to_thread(store.remove_record, profile, kind, identifier)
        else:
            try:
                data = await request.json()
            except (ValueError, UnicodeDecodeError) as exc:
                raise web.HTTPBadRequest(text="Неверные данные записи.") from exc
            if not isinstance(data, dict):
                raise web.HTTPBadRequest(text="Неверные данные записи.")
            text = data.get("text")
            if not isinstance(text, str) or not 0 < len(text.strip()) <= 500:
                raise web.HTTPBadRequest(text="Введи текст до 500 символов.")
            text = text.strip()
            if kind == "notes":
                changed = await asyncio.to_thread(store.edit_note, profile, identifier, text)
            elif kind == "preferences":
                entity = data.get("entity")
                if not isinstance(entity, str) or not ENTITY_ID.fullmatch(entity):
                    raise web.HTTPBadRequest(text="Выбери сущность Home Assistant.")
                text = " ".join(re.findall(r"[a-zа-я0-9]+", text.lower().replace("ё", "е")))[:160]
                if not text:
                    raise web.HTTPBadRequest(text="Введи фразу для запоминания.")
                changed = await asyncio.to_thread(store.edit_preference, profile, identifier, text, entity)
            else:
                due = data.get("due")
                if due is not None:
                    try:
                        due = datetime.fromisoformat(due)
                        if due.tzinfo is None or due <= datetime.now(timezone.utc):
                            raise ValueError
                        due = due.timestamp()
                    except (ValueError, TypeError, OverflowError, OSError) as exc:
                        raise web.HTTPBadRequest(text="Выбери будущую дату и время.") from exc
                value = data.get("value")
                if value is not None and (not isinstance(value, str) or not 0 < len(value.strip()) <= 80):
                    raise web.HTTPBadRequest(text="Введи значение условия.")
                if value is not None:
                    existing = await asyncio.to_thread(store.dashboard, profile)
                    condition = next((item["condition"] for item in existing["reminders"] if item["id"] == identifier), None)
                    if condition and condition.get("operator") in {"above", "below"}:
                        try:
                            if not -1e20 <= float(value) <= 1e20:
                                raise ValueError
                        except ValueError as exc:
                            raise web.HTTPBadRequest(text="Введи числовой порог.") from exc
                changed = await asyncio.to_thread(store.edit_reminder, profile, identifier, text, due,
                                                  value.strip() if value is not None else None)
        if not changed:
            raise web.HTTPConflict(text="Запись уже изменилась, выполнена или совпадает с другой.")
        if kind == "notes" and knowledge:
            if request.method == "DELETE" and old_note:
                await asyncio.to_thread(knowledge.forget, profile, old_note)
            elif request.method != "DELETE":
                await asyncio.to_thread(knowledge.rename_memory, profile, old_note, text)
        return web.json_response({"updated": True})

    async def knowledge(self, request):
        store = self.service.knowledge
        if request.method == "GET":
            return web.json_response({"entities": await asyncio.to_thread(store.entities)})
        entity = request.match_info["entity"]
        if not ENTITY_ID.fullmatch(entity):
            raise web.HTTPBadRequest(text="Выберите сущность Home Assistant.")
        if request.method == "DELETE":
            await asyncio.to_thread(store.remove_entity, entity)
            return web.json_response({"updated": True})
        try:
            data = await request.json()
            if not isinstance(data, dict) or data.keys() - {"aliases", "room", "purpose"}:
                raise ValueError("Неверные поля карты дома.")
            # Settings map is administrator-maintained and cannot create a phantom device.
            await self.service.brain._ha("GET", "/api/states/" + entity)
            value = await asyncio.to_thread(store.set_entity, entity, data.get("aliases", []),
                                            data.get("room", ""), data.get("purpose", ""))
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        except (ClientError, TimeoutError, RuntimeError) as exc:
            raise web.HTTPBadGateway(text="Не удалось проверить устройство в Home Assistant.") from exc
        return web.json_response({"entity": entity, **value})

    async def models(self, request):
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("Выбери профиль LLM.")
            info = await self.supervisor("GET", "info")
            options = info["options"]
            identifier = data.get("id")
            if identifier == "deepseek":
                url = DEEPSEEK_URL
                saved_key = options.get("deepseek_api_key", "")
            else:
                url = base_url(data.get("base_url"))
                old = next((p for p in options.get("llm_profiles", []) if p["id"] == identifier), {})
                saved_key = old.get("api_key", "") if old.get("base_url") == url else ""
            key = data.get("api_key", saved_key)
            if not isinstance(key, str) or len(key) > 1000 or any(ord(char) < 33 for char in key):
                raise ValueError("Неверный ключ API.")
            models = await list_models(self.service.http, url, key)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        except (TimeoutError, ClientError) as exc:
            raise web.HTTPBadGateway(text="Сервер моделей недоступен.") from exc
        return web.json_response({"models": models})

    async def entities(self, _request):
        brain = self.service.brain
        try:
            rows = await brain._ha("GET", "/api/states")
            ids = {row["entity_id"] for row in rows if ENTITY_ID.fullmatch(row["entity_id"])}
            metadata = await brain._registry_metadata(ids)
        except (RuntimeError, ValueError, TimeoutError, ClientError) as exc:
            raise web.HTTPBadGateway(text="Каталог Home Assistant недоступен.") from exc
        return web.json_response({
            "entities": [
                {"id": entity, "name": row.get("attributes", {}).get("friendly_name") or entity,
                 "area": metadata.get(entity, {}).get("area")}
                for row in rows if (entity := row["entity_id"]) in ids
            ],
            "control_domains": sorted(SAFE_SERVICES),
        })

    async def phone_state(self, _request):
        service = self.service
        state = service.modem.call_state
        recipients = [{"key": member["key"], "name": member["name"]}
                      for member in getattr(service, "family", {}).values()]
        owner = bool(service.o.get("allowed_number"))
        return web.json_response(
            {
                "state": state,
                "auto_answer": bool(service.o.get("auto_answer")),
                "can_dial": state == "idle" and bool(owner or recipients),
                "owner_available": owner,
                "recipients": recipients,
                "can_answer": state == "ringing" and service.caller_allowed(),
                "can_hangup": state in {"ringing", "answering", "dialing", "connected"},
            }
        )

    async def phone_action(self, request):
        action = request.match_info["action"]
        if action == "dial":
            try:
                data = await request.json() if request.can_read_body else {}
            except ValueError as exc:
                raise web.HTTPBadRequest(text="Неверный получатель.") from exc
            if not isinstance(data, dict) or not isinstance(data.get("member", ""), str):
                raise web.HTTPBadRequest(text="Выберите получателя.")
            return await self.service.dial_number(member=data.get("member")) if data.get("member") else await self.service.dial_number()
        if action == "answer":
            return await self.service.answer(request)
        if action == "hangup":
            return await self.service.hangup(request)
        if action == "volte":
            modem = self.service.modem
            if modem.call_state != "idle":
                raise web.HTTPConflict(text="Дождитесь окончания звонка.")
            try:
                lines = await modem.command(
                    "AT+VOLTESETTING=1",
                    timeout=4,
                    expected_call=(modem.call_id, "idle"),
                )
            except (RuntimeError, OSError) as exc:
                raise web.HTTPConflict(text=str(exc)) from exc
            if lines[-1] != "OK":
                raise web.HTTPConflict(
                    text="Модем отклонил включение VoLTE: " + lines[-1]
                )
            return web.json_response(
                {
                    "message": "Модем принял команду. Проверьте сеть во время следующего звонка."
                }
            )
        raise web.HTTPNotFound()

    async def history(self, request):
        kind = request.query.get("type")
        if kind not in (None, "call", "sms"):
            raise web.HTTPBadRequest(text="Неизвестный тип записи.")
        try:
            offset = int(request.query.get("offset", 0))
            if not 0 <= offset <= 100_000:
                raise ValueError
        except ValueError as exc:
            raise web.HTTPBadRequest(text="Неверная страница журнала.") from exc
        result = await self.service.history.page(kind, offset)
        return web.json_response({**result, "sms": self.service.sms_status})

    async def audit(self, request):
        try:
            limit = int(request.query.get("limit", 100))
        except ValueError as exc:
            raise web.HTTPBadRequest(text="Неверный размер журнала.") from exc
        store = self.service.personal
        timezone_ = self.service.brain.timezone
        today = datetime.now(timezone_).date().isoformat()
        log, usage, used = await asyncio.gather(
            asyncio.to_thread(store.audit_log, max(1, min(limit, 500))),
            asyncio.to_thread(store.usage),
            asyncio.to_thread(store.tokens_today, today))
        names = self.personal_profiles()
        names.setdefault("room", "Комната")
        for row in (*log, *usage):
            row["profile_name"] = names.get(row["profile"], row["profile"])
        return web.json_response({
            "log": log, "usage": usage, "today": today, "tokens_today": used,
            "daily_limit": int(self.service.o.get("llm_daily_token_limit") or 0)})

    async def refresh_sms(self, _request):
        if self.service.modem.call_state != "idle":
            raise web.HTTPConflict(text="SMS обновятся, когда модем освободится.")
        self.service.sms_wake.set()
        return web.json_response({"queued": True}, status=202)

    async def check(self, request):
        service = self.service
        check = request.match_info["check"]
        try:
            if check == "camera":
                device = None
                if request.can_read_body:
                    data = await request.json()
                    if not isinstance(data, dict) or not isinstance(
                        data.get("device"), str
                    ):
                        raise ValueError("Выберите видеоустройство.")
                    device = data["device"].strip()
                    if not device:
                        raise ValueError("Выберите видеоустройство.")
                try:
                    jpeg = await service.capture_camera(device)
                except RuntimeError as exc:
                    if "resource busy" in str(exc).lower():
                        raise RuntimeError(
                            "Камера занята другим процессом. Выберите другой видеовход "
                            "или проверьте настройки motionEye и go2rtc."
                        ) from exc
                    raise
                return web.Response(body=jpeg, content_type="image/jpeg")
            if check == "modem":
                modem = service.modem
                if modem.call_state == "offline":
                    raise RuntimeError(
                        "Модем недоступен. Проверьте AT-порт и скорость."
                    )
                message = (
                    f"{modem.model} · {modem.firmware} · CSQ {modem.signal or '—'}"
                )
                return web.json_response({"message": message})
            if check == "network":
                if service.modem.call_state == "offline":
                    raise RuntimeError("Модем недоступен. Проверьте AT-порт.")
                status = await service.modem.phone_diagnostics()
                contexts = await service.modem.data_diagnostics()
                message = " · ".join(f"{key} {value}" for key, value in status.items())
                apns = ", ".join(item["apn"] for item in contexts if item["apn"])
                if apns:
                    message += " · APN " + apns
                return web.json_response(
                    {
                        "message": message,
                        "network": status,
                        "data_contexts": contexts,
                    }
                )
            if check == "gps":
                result = await gps_status(service.modem)
                status = result["modem_gps_status"]
                fix = "координаты получены" if result["fix"] else "координат пока нет"
                return web.json_response({"message": f"{status} · {fix}"})
            if check in {"microphone", "microphone_sample"}:
                if service.mic_lock.locked():
                    raise RuntimeError("Микрофон сейчас занят.")
                device = service.o.get("q1_mic_device")
                if not device:
                    raise RuntimeError("Выберите и примените устройство микрофона.")
                async with service.mic_lock:
                    pcm = await capture_pcm(device, seconds=5 if check == "microphone_sample" else 1)
                if not pcm:
                    raise RuntimeError("Устройство не передало PCM.")
                if check == "microphone_sample":
                    buffer = io.BytesIO()
                    with wave.open(buffer, "wb") as recording:
                        recording.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                        recording.writeframes(pcm)
                    return web.Response(
                        body=buffer.getvalue(), content_type="audio/wav",
                        headers={"Cache-Control": "no-store"},
                    )
                return web.json_response(
                    {"message": f"Получено {len(pcm)} байт PCM · уровень {rms(pcm)}"}
                )
            if check == "speaker":
                device = service.o.get("q1_speaker_device")
                if not device:
                    raise RuntimeError("Выберите и примените устройство динамика.")
                if service.speaker_lock.locked() or service.media.playing:
                    raise RuntimeError("Динамик сейчас занят.")
                samples = [
                    int(1400 * math.sin(index * math.tau * 520 / 16000))
                    for index in range(4000)
                ]
                async with service.speaker_lock:
                    await play_pcm(device, struct.pack("<4000h", *samples))
                return web.json_response(
                    {"message": "Короткий тестовый сигнал отправлен."}
                )
            if check == "speech":
                if not service.speech:
                    raise RuntimeError("Добавьте и примените ключ SpeechKit.")
                from grpc import RpcError

                try:
                    size = 0
                    async for chunk in service.speech.synthesize("Проверка связи."):
                        size += len(chunk)
                except RpcError as exc:
                    raise RuntimeError(f"SpeechKit вернул {exc.code().name}.") from exc
                if not size:
                    raise RuntimeError("SpeechKit не вернул звук.")
                return web.json_response(
                    {"message": f"SpeechKit вернул {size} байт звука."}
                )
            if check == "deepseek":
                profile = active_profile(service.o)
                if not profile or (profile["id"] == "deepseek" and not profile["api_key"]):
                    raise web.HTTPConflict(text="Настрой активный профиль LLM.")
                models = await list_models(service.http, profile["base_url"], profile["api_key"])
                message = f"{profile['name']} · {len(models)} моделей."
                if profile["model"] not in models:
                    message += " Выбранной модели нет в списке сервера."
                return web.json_response({"message": message})

            raise web.HTTPNotFound()
        except (ClientError, TimeoutError) as exc:
            raise web.HTTPBadGateway(
                text="Проверка не получила успешный ответ."
            ) from exc
        except (RuntimeError, ValueError, OSError) as exc:
            raise web.HTTPConflict(text=str(exc)[:250]) from exc

    async def sandbox(self, request):
        """Persona sandbox: a tool-free reply from the active LLM, optionally spoken as WAV."""
        try:
            data = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise web.HTTPBadRequest(text="Body must be JSON") from exc
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="Body must be a JSON object")
        text = data.get("text")
        prompt = data.get("prompt")
        if not isinstance(text, str) or not text.strip() or len(text) > 1000:
            raise web.HTTPBadRequest(text="Введите фразу до 1000 символов.")
        if prompt is not None and (not isinstance(prompt, str) or len(prompt) > 6000):
            raise web.HTTPBadRequest(text="Манера общения: до 6000 символов.")
        service = self.service
        try:
            if request.match_info.get("mode") == "speak":
                if not service.speech:
                    raise RuntimeError("Добавьте и примените ключ SpeechKit.")
                from grpc import RpcError

                pcm = bytearray()
                try:
                    async for chunk in service.speech.synthesize(text.strip()[:500]):
                        pcm.extend(chunk)
                except RpcError as exc:
                    raise RuntimeError(f"SpeechKit вернул {exc.code().name}.") from exc
                if not pcm:
                    raise RuntimeError("SpeechKit не вернул звук.")
                buffer = io.BytesIO()
                with wave.open(buffer, "wb") as recording:
                    recording.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                    recording.writeframes(bytes(pcm))
                return web.Response(body=buffer.getvalue(), content_type="audio/wav")
            reply = await service.brain.sandbox(text.strip(), prompt)
            return web.json_response({"reply": reply})
        except (ClientError, TimeoutError) as exc:
            raise web.HTTPBadGateway(text="Модель не ответила.") from exc
        except (RuntimeError, ValueError) as exc:
            raise web.HTTPConflict(text=str(exc)[:250]) from exc

    def app(self):
        app = web.Application(middlewares=[self.authorize], client_max_size=32 * 1024)
        app.router.add_post("/sandbox", self.sandbox)
        app.router.add_post("/sandbox/{mode:speak}", self.sandbox)
        app.router.add_get("/", self.home)
        app.router.add_static("/assets", ASSETS)
        app.router.add_get("/state", self.state)
        app.router.add_get("/entities", self.entities)
        app.router.add_get("/knowledge", self.knowledge)
        app.router.add_patch("/knowledge/{entity}", self.knowledge)
        app.router.add_delete("/knowledge/{entity}", self.knowledge)
        app.router.add_post("/llm/models", self.models)
        app.router.add_get("/operations", self.operations)
        app.router.add_patch("/operations", self.operations)
        app.router.add_post("/operations", self.operations)
        app.router.add_get("/personal", self.personal)
        app.router.add_patch("/personal/{kind}/{id}", self.personal_edit)
        app.router.add_delete("/personal/{kind}/{id}", self.personal_edit)
        app.router.add_put("/settings", self.save)
        app.router.add_post("/restart", self.restart)
        app.router.add_get("/phone", self.phone_state)
        app.router.add_post("/phone/{action}", self.phone_action)
        app.router.add_get("/history", self.history)
        app.router.add_get("/audit", self.audit)
        app.router.add_post("/history/refresh", self.refresh_sms)
        app.router.add_post("/checks/{check}", self.check)
        return app

    async def home(self, _request):
        return web.FileResponse(ASSETS / "index.html")
