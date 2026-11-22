"""Validation and field definitions for the add-on configuration form."""

import os
import re

from brain import SAFE_SERVICES
from family import family_profiles, lock_entities
from llm import EFFORTS, active_profile, base_url, merge_profiles
from modem import NUMBER
from q1_hardware import VIDEO_LINK, VIDEO_PATH
from resilience import validate_speech_fallback

SECRET_KEYS = {
    "yandex_api_key",
    "deepseek_api_key",
    "gate_pin",
    "speech_fallback_api_key",
}
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
    "speech_fallback_base_url": 512,
    "speech_fallback_stt_model": 256,
    "speech_fallback_tts_model": 256,
    "speech_fallback_voice": 256,
}
INTEGER_FIELDS = {
    "serial_baud": (1200, 921600),
    "max_calls_per_hour": (1, 20),
    "call_playback_volume": (10, 100),
    "llm_daily_token_limit": (0, 10_000_000),
}
BOOLEAN_FIELDS = {
    "auto_answer",
    "observe_all_entities",
    "control_all_entities",
    "speech_cache_enabled",
    "call_echo_filter",
    "confirm_risky_actions",
    "ha_events",
    "call_summary",
    "stream_replies",
    "speech_fallback_enabled",
}
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
        "llm_fallback_ids",
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
            if not isinstance(value, str) or (
                key != "gate_pin" and len(value) > TEXT_FIELDS.get(key, 512)
            ):
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
        elif key == "llm_fallback_ids":
            if (
                not isinstance(value, list)
                or len(value) > 4
                or any(not isinstance(item, str) for item in value)
            ):
                raise ValueError("Выберите до четырёх резервных профилей.")
            value = list(dict.fromkeys(value))
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
        raise ValueError(
            "Код замка должен состоять из цифр; оставьте пустым для открытия без кода."
        )
    profile_ids = {"deepseek", *[row["id"] for row in options.get("llm_profiles", [])]}
    if any(item not in profile_ids for item in options.get("llm_fallback_ids", [])):
        raise ValueError("Резервный профиль LLM не найден.")
    if options.get("speech_fallback_base_url"):
        options["speech_fallback_base_url"] = base_url(
            options["speech_fallback_base_url"]
        )
    if (
        current.get("speech_fallback_api_key")
        and "speech_fallback_api_key" not in changes
        and options.get("speech_fallback_base_url")
        != current.get("speech_fallback_base_url")
    ):
        raise ValueError(
            "При смене сервера речи введите новый ключ или явно удалите старый."
        )
    if options.get("speech_fallback_enabled"):
        validate_speech_fallback(
            {
                key: options.get("speech_fallback_" + key, "")
                for key in ("base_url", "api_key", "stt_model", "tts_model", "voice")
            }
        )
    calendar = options.get("reminder_calendar", "")
    if calendar and (
        not ENTITY_ID.fullmatch(calendar) or not calendar.startswith("calendar.")
    ):
        raise ValueError("Укажите календарь Home Assistant вида calendar.name.")
    person = options.get("reminder_person", "")
    if person and (not ENTITY_ID.fullmatch(person) or not person.startswith("person.")):
        raise ValueError("Укажите свою сущность Home Assistant вида person.name.")
    calendars = [member["calendar"] for member in family.values() if member["calendar"]]
    if calendar:
        calendars.append(calendar)
    if len(calendars) != len(set(calendars)):
        raise ValueError(
            "Для каждого участника выберите отдельный календарь напоминаний."
        )
    if options.get("auto_answer") and not (number or family):
        raise ValueError("Укажите разрешённый номер для автоответа.")
    for key, domain in (
        ("home_tts_entity", "tts"),
        ("telegram_photo_target", "notify"),
    ):
        entity = changes.get(key, "").strip()
        if entity and (
            not ENTITY_ID.fullmatch(entity) or not entity.startswith(domain + ".")
        ):
            raise ValueError(f"{key}: укажите сущность {domain}.*.")
    camera = changes.get("camera_entity", "").strip()
    if camera and not (camera.startswith("camera.") and ENTITY_ID.fullmatch(camera)):
        raise ValueError("Укажите идентификатор камеры вида camera.front_door.")
    return options
