"""Ingress diagnostics, phone commands, and voice preview."""

import io
import logging
import math
import struct
import wave

from aiohttp import ClientError, web
from audio import rms
from llm import (
    DEEPSEEK_URL,
    active_profile,
    base_url,
    list_models,
)
from q1_audio import capture_pcm, play_pcm
from setup_options import validate_changes as validate_changes

LOG = logging.getLogger(__name__)


class PanelTools:
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
                old = next(
                    (
                        p
                        for p in options.get("llm_profiles", [])
                        if p["id"] == identifier
                    ),
                    {},
                )
                saved_key = old.get("api_key", "") if old.get("base_url") == url else ""
            key = data.get("api_key", saved_key)
            if (
                not isinstance(key, str)
                or len(key) > 1000
                or any(ord(char) < 33 for char in key)
            ):
                raise ValueError("Неверный ключ API.")
            models = await list_models(self.service.http, url, key)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        except (TimeoutError, ClientError) as exc:
            raise web.HTTPBadGateway(text="Сервер моделей недоступен.") from exc
        return web.json_response({"models": models})

    async def phone_state(self, _request):
        service = self.service
        state = service.modem.call_state
        recipients = [
            {"key": member["key"], "name": member["name"]}
            for member in getattr(service, "family", {}).values()
        ]
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
            if not isinstance(data, dict) or not isinstance(
                data.get("member", ""), str
            ):
                raise web.HTTPBadRequest(text="Выберите получателя.")
            return (
                await self.service.dial_number(member=data.get("member"))
                if data.get("member")
                else await self.service.dial_number()
            )
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
            if check in {"microphone", "microphone_sample"}:
                if service.mic_lock.locked():
                    raise RuntimeError("Микрофон сейчас занят.")
                device = service.o.get("q1_mic_device")
                if not device:
                    raise RuntimeError("Выберите и примените устройство микрофона.")
                async with service.mic_lock:
                    pcm = await capture_pcm(
                        device, seconds=5 if check == "microphone_sample" else 1
                    )
                if not pcm:
                    raise RuntimeError("Устройство не передало PCM.")
                if check == "microphone_sample":
                    buffer = io.BytesIO()
                    with wave.open(buffer, "wb") as recording:
                        recording.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                        recording.writeframes(pcm)
                    return web.Response(
                        body=buffer.getvalue(),
                        content_type="audio/wav",
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
                if not profile or (
                    profile["id"] == "deepseek" and not profile["api_key"]
                ):
                    raise web.HTTPConflict(text="Настрой активный профиль LLM.")
                models = await list_models(
                    service.http, profile["base_url"], profile["api_key"]
                )
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
