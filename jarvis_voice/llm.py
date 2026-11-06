"""Chat Completions profiles and their independent model catalogs."""
import re
from urllib.parse import urlsplit

import aiohttp

DEEPSEEK_URL = "https://api.deepseek.com"
EFFORTS = {"default", "off", "minimal", "low", "medium", "high", "xhigh", "max"}
PROFILE_ID = re.compile(r"[a-zA-Z0-9_-]{1,64}")


def base_url(value):
    if not isinstance(value, str) or not value or len(value) > 500 or re.search(r"[\s\\]", value):
        raise ValueError("Укажи базовый URL API.")
    url = urlsplit(value)
    if (url.scheme not in {"https", "http"} or not url.hostname or url.username is not None
        or url.query or url.fragment or url.port == 0):
        raise ValueError("Используй URL http(s) без логина, параметров и фрагмента.")
    return value.rstrip("/")


def active_profile(options):
    selected = options.get("llm_active", "deepseek")
    if selected == "deepseek":
        return {"id": "deepseek", "name": "DeepSeek", "base_url": DEEPSEEK_URL,
                "api_key": options.get("deepseek_api_key", ""),
                "model": options.get("deepseek_model") or "deepseek-flash",
                "effort": options.get("deepseek_effort", "off")}
    return next((p for p in options.get("llm_profiles", []) if p["id"] == selected), None)


def merge_profiles(incoming, saved):
    if not isinstance(incoming, list):
        raise ValueError("Ожидался список профилей LLM.")
    previous = {item["id"]: item for item in saved}
    profiles = []
    seen = {"deepseek"}
    for item in incoming:
        if not isinstance(item, dict):
            raise ValueError("Неверный профиль LLM.")
        identifier, name, model = (item.get(field) for field in ("id", "name", "model"))
        effort = item.get("effort", "default")
        if (not isinstance(identifier, str) or not PROFILE_ID.fullmatch(identifier) or identifier in seen
            or not isinstance(name, str) or not name.strip() or len(name) > 80
            or not isinstance(model, str) or len(model) > 256 or not isinstance(effort, str) or effort not in EFFORTS):
            raise ValueError("Проверь имя, модель и режим рассуждения профиля LLM.")
        url = base_url(item.get("base_url"))
        old = previous.get(identifier, {})
        if "api_key" not in item and old.get("api_key") and old.get("base_url") != url:
            raise ValueError("При смене адреса введи новый ключ или явно удали старый.")
        key = item.get("api_key", old.get("api_key", ""))
        if not isinstance(key, str) or len(key) > 1000 or any(ord(char) < 33 for char in key):
            raise ValueError("Неверный ключ API.")
        profiles.append({"id": identifier, "name": name.strip(), "base_url": url,
                         "api_key": key, "model": model.strip(), "effort": effort})
        seen.add(identifier)
    return profiles


def auth_headers(key):
    return {"Authorization": "Bearer " + key} if key else {}


async def list_models(session, url, key):
    async with session.get(base_url(url) + "/models", headers=auth_headers(key),
                           allow_redirects=False, timeout=aiohttp.ClientTimeout(total=10)) as response:
        if response.status != 200:
            raise ValueError(f"Сервер моделей вернул HTTP {response.status}.")
        result = await response.json()
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise ValueError("Сервер вернул список моделей в другом формате.")
    return sorted({row["id"] for row in result["data"] if isinstance(row, dict)
                   and isinstance(row.get("id"), str) and 0 < len(row["id"]) <= 256})


def completion_options(profile):
    effort = profile["effort"]
    if profile["id"] == "deepseek":
        if effort in {"off", "default"}:
            return {"thinking": {"type": "disabled"}, "max_tokens": 350, "temperature": 0.4}
        mapped = {"minimal": "low", "medium": "high", "xhigh": "high"}.get(effort, effort)
        return {"thinking": {"type": "enabled"}, "reasoning_effort": mapped,
                "max_tokens": 8192 if mapped == "max" else 4096}
    if effort == "default":
        return {}
    return {"reasoning_effort": {"off": "none"}.get(effort, effort)}
