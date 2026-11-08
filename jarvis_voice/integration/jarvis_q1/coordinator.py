"""One coordinator shared by all JARVIS Q1 entities."""
from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import JarvisAuthError, JarvisError
from .const import (
    EVENT_CALL_ENDED,
    EVENT_CALL_INCOMING,
    EVENT_TRANSCRIPT,
    MICROPHONE_INTERVAL_SECONDS,
    STATUS_INTERVAL_SECONDS,
)
from .helpers import call_transition

_LOGGER = logging.getLogger(__name__)


class JarvisCoordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass, entry, client):
        super().__init__(
            hass, _LOGGER, name="JARVIS Q1",
            update_interval=timedelta(seconds=STATUS_INTERVAL_SECONDS),
        )
        self.entry = entry
        self.client = client
        self.last_transcript = ""
        self.last_answer = ""
        self.probe = {}
        self.microphone = None
        self._last_call_state = None
        self._last_number = ""

    async def async_probe(self, *, raise_errors=False):
        try:
            result = await self.client.get("/q1/probe", timeout=20)
        except JarvisAuthError as exc:
            if raise_errors:
                raise
            _LOGGER.warning("Hardware probe rejected: %s", exc)
            return self.probe
        except JarvisError as exc:
            if raise_errors:
                raise
            _LOGGER.warning("Hardware probe not yet available: %s", exc)
            return self.probe
        if not isinstance(result, dict):
            if raise_errors:
                raise HomeAssistantError("JARVIS /q1/probe returned an unexpected payload")
            _LOGGER.warning("Hardware probe returned unexpected payload")
            return self.probe
        self.probe = result
        return self.probe

    async def _async_update_data(self):
        try:
            data = await self.client.get("/status")
        except JarvisAuthError as exc:
            raise ConfigEntryAuthFailed(str(exc)) from exc
        except JarvisError as exc:
            raise UpdateFailed(str(exc)) from exc
        if not isinstance(data, dict):
            raise UpdateFailed("JARVIS /status returned an unexpected payload")
        if data.get("call_state") != "offline":
            try:
                gps = await self.client.get("/q1/gps", timeout=9)
            except JarvisAuthError as exc:
                raise ConfigEntryAuthFailed(str(exc)) from exc
            except JarvisError:
                gps = {}
            data["gps"] = gps if isinstance(gps, dict) else {}
        else:
            data["gps"] = {}
        self._fire_call_events(data)
        return data

    def _fire_call_events(self, data):
        current = data.get("call_state")
        number = str(data.get("active_number") or "")
        if number:
            self._last_number = number
        transition = call_transition(self._last_call_state, current)
        if transition == "incoming":
            self.hass.bus.async_fire(EVENT_CALL_INCOMING, {
                "entry_id": self.entry.entry_id, "number": number or None,
            })
        elif transition == "ended":
            self.hass.bus.async_fire(EVENT_CALL_ENDED, {
                "entry_id": self.entry.entry_id,
                "number": number or self._last_number or None,
                "previous_state": self._last_call_state,
            })
        if current in ("idle", "offline"):
            self._last_number = ""
        self._last_call_state = current

    def _notify(self):
        # Do not overwrite last_update_success/data when /status is unavailable.
        if self.data is not None and self.last_update_success:
            self.async_set_updated_data(self.data)
        else:
            self.async_update_listeners()

    async def listen(self, seconds=12):
        result = await self.client.post("/q1/listen", {"seconds": seconds}, timeout=seconds + 40)
        if not isinstance(result, dict):
            raise HomeAssistantError("JARVIS /q1/listen returned an unexpected payload")
        self.last_transcript = str(result.get("recognized") or "")
        self.last_answer = str(result.get("response") or "")
        self._notify()
        self.hass.bus.async_fire(EVENT_TRANSCRIPT, {
            "entry_id": self.entry.entry_id, "text": self.last_transcript,
            "response": self.last_answer})
        return result

    async def transcribe(self, seconds=12):
        result = await self.client.post("/q1/transcribe", {"seconds": seconds}, timeout=seconds + 40)
        if not isinstance(result, dict):
            raise HomeAssistantError("JARVIS /q1/transcribe returned an unexpected payload")
        self.last_transcript = str(result.get("text") or "")
        self._notify()
        self.hass.bus.async_fire(EVENT_TRANSCRIPT, {
            "entry_id": self.entry.entry_id, "text": self.last_transcript, "response": ""})
        return result


class MicrophoneCoordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass, client):
        super().__init__(
            hass, _LOGGER, name="JARVIS microphone",
            update_interval=timedelta(seconds=MICROPHONE_INTERVAL_SECONDS),
        )
        self.client = client

    async def _async_update_data(self):
        try:
            data = await self.client.get("/q1/microphone", timeout=5)
        except JarvisAuthError as exc:
            # The status coordinator owns reauth; just mark entities unavailable here.
            raise UpdateFailed(str(exc)) from exc
        except JarvisError as exc:
            raise UpdateFailed(str(exc)) from exc
        if not isinstance(data, dict):
            raise UpdateFailed("JARVIS /q1/microphone returned an unexpected payload")
        return data

    async def set_enabled(self, enabled):
        data = await self.client.post("/q1/microphone", {"enabled": enabled})
        if not isinstance(data, dict):
            raise HomeAssistantError("JARVIS /q1/microphone returned an unexpected payload")
        self.async_set_updated_data(data)
