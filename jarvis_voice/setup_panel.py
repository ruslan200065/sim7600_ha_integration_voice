"""Administrator-only Home Assistant ingress for settings and hardware checks."""

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import struct
from pathlib import Path

from aiohttp import ClientError, ClientTimeout, web
from audio import rms
from modem import NUMBER
from q1_audio import capture_pcm, play_pcm
from q1_hardware import VIDEO_LINK, VIDEO_PATH, list_serial, list_video, probe_audio
from q1_protocol import gps_status

LOG = logging.getLogger(__name__)
ASSETS = Path(__file__).with_name("web")
SECRET_KEYS = {"yandex_api_key", "deepseek_api_key"}
TEXT_FIELDS = {
    "at_port": 256,
    "pcm_port": 256,
    "q1_video_device": 256,
    "q1_mic_device": 128,
    "q1_speaker_device": 128,
    "allowed_number": 17,
    "deepseek_model": 128,
    "yandex_voice": 80,
    "yandex_role": 80,
    "camera_entity": 256,
}
INTEGER_FIELDS = {"serial_baud": (1200, 921600), "max_calls_per_hour": (1, 20)}
OPTION_FIELDS = (
    set(TEXT_FIELDS)
    | set(INTEGER_FIELDS)
    | {
        "auto_answer",
        "allowed_entities",
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
            if not isinstance(value, str) or len(value) > TEXT_FIELDS.get(key, 512):
                raise ValueError(f"Неверное значение поля {key}.")
            value = value.strip()
            if any(ord(char) < 32 for char in value):
                raise ValueError(f"Недопустимые символы в поле {key}.")
        elif key in INTEGER_FIELDS:
            lo, hi = INTEGER_FIELDS[key]
            if type(value) is not int or not lo <= value <= hi:
                raise ValueError(f"{key}: допустимо от {lo} до {hi}.")
        elif key == "auto_answer" and not isinstance(value, bool):
            raise ValueError("Неверное значение автоответа.")
        elif key == "allowed_entities":
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
        options[key] = value

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
    if options.get("auto_answer") and not number:
        raise ValueError("Укажите разрешённый номер для автоответа.")
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
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self' blob:; connect-src 'self'; frame-ancestors 'self'; "
            "base-uri 'none'"
        )
        return response

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
            self.supervisor("GET", "info"), probe_audio()
        )
        options = info["options"]
        active = self.service.o
        return web.json_response(
            {
                "version": info.get("version"),
                "boot": self.boot_id,
                "options": {
                    key: options[key] for key in OPTION_FIELDS if key in options
                },
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
            service = self.service
            if (
                service.modem.call_state not in {"idle", "offline"}
                or service.mic_lock.locked()
                or service.speaker_lock.locked()
                or service.media.playing
            ):
                raise web.HTTPConflict(
                    text="Дождитесь окончания звонка или работы со звуком."
                )
            if not self.restart_task or self.restart_task.done():
                self.restart_task = asyncio.create_task(self.restart_addon())
        return web.json_response({"restarting": True}, status=202)

    async def restart_addon(self):
        await asyncio.sleep(0.5)
        try:
            await self.supervisor("POST", "restart", timeout=45)
        except web.HTTPException as exc:
            LOG.warning("Could not restart add-on from ingress: %s", exc.status)

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
            if check == "gps":
                result = await gps_status(service.modem)
                status = result["modem_gps_status"]
                fix = "координаты получены" if result["fix"] else "координат пока нет"
                return web.json_response({"message": f"{status} · {fix}"})
            if check == "microphone":
                if service.mic_lock.locked():
                    raise RuntimeError("Микрофон сейчас занят.")
                device = service.o.get("q1_mic_device")
                if not device:
                    raise RuntimeError("Выберите и примените устройство микрофона.")
                async with service.mic_lock:
                    pcm = await capture_pcm(device, seconds=1)
                if not pcm:
                    raise RuntimeError("Устройство не передало PCM.")
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
                key = service.o.get("deepseek_api_key")
                if not key:
                    raise RuntimeError("Добавьте и примените ключ DeepSeek.")
                async with service.http.get(
                    "https://api.deepseek.com/models",
                    headers={"Authorization": "Bearer " + key},
                    timeout=ClientTimeout(total=8),
                    allow_redirects=False,
                ) as response:
                    response.raise_for_status()
                    models = (await response.json())["data"]
                model = service.o.get("deepseek_model", "deepseek-flash")
                if not any(item["id"] == model for item in models):
                    raise RuntimeError(
                        f"Модель {model} отсутствует в списке доступных."
                    )
                return web.json_response(
                    {"message": f"Ключ принят, модель {model} доступна."}
                )
            raise web.HTTPNotFound()
        except (ClientError, TimeoutError) as exc:
            raise web.HTTPBadGateway(
                text="Проверка не получила успешный ответ."
            ) from exc
        except (RuntimeError, ValueError, OSError) as exc:
            raise web.HTTPConflict(text=str(exc)[:250]) from exc

    def app(self):
        app = web.Application(middlewares=[self.authorize], client_max_size=32 * 1024)
        app.router.add_get("/", self.home)
        app.router.add_static("/assets", ASSETS)
        app.router.add_get("/state", self.state)
        app.router.add_put("/settings", self.save)
        app.router.add_post("/restart", self.restart)
        app.router.add_post("/checks/{check}", self.check)
        return app

    async def home(self, _request):
        return web.FileResponse(ASSETS / "index.html")
