"""Administrator-only Home Assistant ingress for settings and hardware checks."""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from pathlib import Path

from aiohttp import ClientError, ClientTimeout, web
from install_integration import installed_companion_status
from intelligence_panel import register_intelligence
from panel_operations import PanelOperations
from panel_tools import PanelTools
from q1_hardware import list_serial, list_video, probe_audio
from setup_options import AUDIO_FIELDS, OPTION_FIELDS, SECRET_KEYS
from setup_options import validate_changes as validate_changes

LOG = logging.getLogger(__name__)
ASSETS = Path(__file__).with_name("web")


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


class SetupPanel(PanelOperations, PanelTools):
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
                if not isinstance(result, dict) or result.get("result") != "ok":
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
                    key: options[key]
                    for key in OPTION_FIELDS
                    if key in options and key != "llm_profiles"
                },
                "llm_profiles": [
                    {
                        **{
                            field: p[field]
                            for field in ("id", "name", "base_url", "model", "effort")
                        },
                        "has_key": bool(p.get("api_key")),
                    }
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

    async def companion(self, _request):
        info = await self.supervisor("GET", "info")
        status = await asyncio.to_thread(installed_companion_status)
        return web.json_response(
            {
                **status,
                "auto_install": info.get("options", {}).get(
                    "auto_install_integration", True
                ),
            }
        )

    async def companion_credentials(self, _request):
        try:
            host = await self.service.companion_address()
        except (
            ClientError,
            TimeoutError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            RuntimeError,
        ) as exc:
            raise web.HTTPBadGateway(
                text="Supervisor не вернул адрес приложения."
            ) from exc
        return web.json_response(
            {"host": host, "port": 8099, "token": self.service.api_token}
        )

    async def companion_discover(self, _request):
        if not await self.service.discover_companion():
            raise web.HTTPBadGateway(text="Supervisor не принял запрос обнаружения.")
        return web.json_response({"published": True})

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

    async def shutdown(self, _app):
        if self.restart_task and not self.restart_task.done():
            self.restart_task.cancel()
            await asyncio.gather(self.restart_task, return_exceptions=True)

    def app(self):
        app = web.Application(middlewares=[self.authorize], client_max_size=32 * 1024)
        app.on_shutdown.append(self.shutdown)
        register_intelligence(app, self)
        if hasattr(self.service, "camera_panel"):
            self.service.camera_panel.register(app)
        app.router.add_post("/sandbox", self.sandbox)
        app.router.add_post("/sandbox/{mode:speak}", self.sandbox)
        app.router.add_get("/", self.home)
        app.router.add_static("/assets", ASSETS)
        app.router.add_get("/state", self.state)
        app.router.add_get("/companion", self.companion)
        app.router.add_post("/companion/credentials", self.companion_credentials)
        app.router.add_post("/companion/discover", self.companion_discover)
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
        app.router.add_delete("/history/{kind}", self.clear_history)
        app.router.add_get("/audit", self.audit)
        app.router.add_post("/history/refresh", self.refresh_sms)
        app.router.add_post("/checks/{check}", self.check)
        return app

    async def home(self, _request):
        return web.FileResponse(ASSETS / "index.html")
