"""Custom companion automatically copied by JARVIS Voice Home Assistant App."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .client import JarvisClient, JarvisError
from .const import CONF_ENTRY_ID, DOMAIN
from .coordinator import JarvisCoordinator
from .helpers import EntryLookupError, resolve_entry_id

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.MEDIA_PLAYER,
    Platform.SWITCH,
]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
_RETIRED_ENTITIES = {
    ("sensor", "jarvis_q1_gps_latitude"),
    ("sensor", "jarvis_q1_gps_longitude"),
    ("binary_sensor", "jarvis_q1_gps_fix"),
    ("button", "jarvis_q1_gps_start"),
}


type JarvisConfigEntry = ConfigEntry[JarvisCoordinator]


async def async_setup(hass: HomeAssistant, config):
    """Register domain services once, independent of config entries."""
    _register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: JarvisConfigEntry) -> bool:
    client = JarvisClient.from_entry(hass, entry.data)
    coordinator = JarvisCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_probe()
    await coordinator.microphone.async_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _remove_retired_entities(hass, entry)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: JarvisConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


def _remove_retired_entities(hass: HomeAssistant, entry: JarvisConfigEntry) -> None:
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (entity.domain, entity.unique_id) in _RETIRED_ENTITIES:
            registry.async_remove(entity.entity_id)


def _get_coordinator(hass: HomeAssistant, entry_id: str | None) -> JarvisCoordinator:
    loaded = {
        entry.entry_id: entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    }
    try:
        return loaded[resolve_entry_id(loaded, entry_id)].runtime_data
    except EntryLookupError as exc:
        raise ServiceValidationError(str(exc)) from exc


def _register_services(hass):
    common = {vol.Optional(CONF_ENTRY_ID): cv.string}

    def coord(call) -> JarvisCoordinator:
        return _get_coordinator(hass, call.data.get(CONF_ENTRY_ID))

    async def handle_speak(call):
        await coord(call).client.post(
            "/q1/speak", {"text": call.data["text"]}, timeout=75
        )

    async def handle_listen(call):
        return await coord(call).listen(call.data.get("seconds", 12))

    async def handle_transcribe(call):
        return await coord(call).transcribe(call.data.get("seconds", 12))

    async def handle_call(call):
        await coord(call).client.post(
            "/dial",
            {
                "prompt": call.data.get("prompt", ""),
                "camera_entity": call.data.get("camera_entity"),
                "member": call.data.get("member"),
            },
            timeout=25,
        )

    async def handle_dtmf(call):
        await coord(call).client.post(
            "/dtmf", {"digits": call.data["digits"]}, timeout=70
        )

    async def handle_hangup(call):
        await coord(call).client.post("/hangup")

    async def handle_led(call):
        await coord(call).client.post("/q1/led", {"color": call.data["color"]})

    async def handle_probe(call):
        c = coord(call)
        try:
            return await c.async_probe(raise_errors=True)
        except JarvisError as exc:
            raise HomeAssistantError(f"JARVIS hardware probe failed: {exc}") from exc

    seconds = vol.All(vol.Coerce(int), vol.Range(min=3, max=30))
    hass.services.async_register(
        DOMAIN,
        "speak",
        handle_speak,
        schema=vol.Schema(
            {**common, vol.Required("text"): vol.All(str, vol.Length(min=1, max=2000))}
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "listen",
        handle_listen,
        schema=vol.Schema({**common, vol.Optional("seconds", default=12): seconds}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "transcribe",
        handle_transcribe,
        schema=vol.Schema({**common, vol.Optional("seconds", default=12): seconds}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "call",
        handle_call,
        schema=vol.Schema(
            {
                **common,
                vol.Optional("prompt", default=""): vol.All(str, vol.Length(max=1200)),
                vol.Optional("camera_entity"): cv.entity_id,
                vol.Optional("member"): vol.All(str, vol.Length(min=1, max=60)),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN, "hangup", handle_hangup, schema=vol.Schema(common)
    )
    hass.services.async_register(
        DOMAIN,
        "send_dtmf",
        handle_dtmf,
        schema=vol.Schema(
            {
                **common,
                vol.Required("digits"): vol.All(str, vol.Match(r"^[0-9*#A-D]{1,16}$")),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "set_led",
        handle_led,
        schema=vol.Schema(
            {
                **common,
                vol.Required("color"): vol.In(
                    ["red", "green", "blue", "white", "pink", "yellow", "cyan", "off"]
                ),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "probe",
        handle_probe,
        schema=vol.Schema(common),
        supports_response=SupportsResponse.ONLY,
    )
