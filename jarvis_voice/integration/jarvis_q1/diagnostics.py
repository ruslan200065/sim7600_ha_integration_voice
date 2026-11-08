"""Diagnostics for JARVIS Q1 with token, host and phone numbers redacted."""
from __future__ import annotations

from typing import Any

from .const import CONF_HOST, CONF_TOKEN, DOMAIN
from .helpers import redact

REDACT_KEYS = {
    CONF_TOKEN, CONF_HOST, "api_token", "authorization", "active_number", "number",
    "phone_number", "caller_number", "allowed_number", "latitude", "longitude",
    "imei", "imsi", "iccid", "hostname",
}


async def async_get_config_entry_diagnostics(hass, entry) -> dict[str, Any]:
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    secrets = [entry.data.get(CONF_TOKEN), entry.data.get(CONF_HOST)]
    payload: dict[str, Any] = {
        "entry": {"title": entry.title, "source": entry.source, "version": entry.version,
                  "data": dict(entry.data), "options": dict(entry.options)},
    }
    if coordinator is not None:
        payload["status"] = coordinator.data
        payload["last_update_success"] = coordinator.last_update_success
        payload["probe"] = coordinator.probe
        payload["microphone"] = getattr(coordinator.microphone, "data", None)
        payload["last_transcript_length"] = len(coordinator.last_transcript or "")
    return redact(payload, REDACT_KEYS, secrets)
