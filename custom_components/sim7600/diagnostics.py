"""Diagnostics for the SIM7600 integration."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_BALANCE_USSD, CONF_DEFAULT_RECIPIENT
from .coordinator import SIM7600DataUpdateCoordinator

TO_REDACT = {
    "imei",
    "latitude",
    "longitude",
    "altitude",
    "sender",
    "number",
    "message",
    "balance",
    "unique_id",
    "serial_number",
    CONF_DEFAULT_RECIPIENT,
    CONF_BALANCE_USSD,
}


def _plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics with IMEI, numbers, texts and coordinates redacted."""
    coordinator: SIM7600DataUpdateCoordinator = entry.runtime_data
    return {
        "entry": async_redact_data(
            {
                "title": entry.title,
                "unique_id": entry.unique_id,
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            TO_REDACT,
        ),
        "data": async_redact_data(_plain(coordinator.data or {}), TO_REDACT),
        "last_call": async_redact_data(_plain(coordinator.last_call or {}), TO_REDACT),
        "modem": {
            "connected": coordinator.modem.connected,
            "pending_multipart_sms": coordinator.modem.has_pending_sms,
            "last_update_success": coordinator.last_update_success,
        },
    }
