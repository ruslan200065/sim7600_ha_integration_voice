"""Custom companion automatically copied by JARVIS Voice Home Assistant App."""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.const import Platform
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import JarvisClient, JarvisError
from .const import CONF_ENTRY_ID, CONF_HOST, CONF_PORT, CONF_TOKEN, DOMAIN
from .coordinator import JarvisCoordinator, MicrophoneCoordinator
from .helpers import EntryLookupError, resolve_entry_id

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON,
             Platform.CAMERA, Platform.MEDIA_PLAYER, Platform.SWITCH]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass, config):
    """Register domain services once, independent of config entries."""
    hass.data.setdefault(DOMAIN, {})
    _register_services(hass)
    return True


async def async_setup_entry(hass, entry):
    client = JarvisClient(async_get_clientsession(hass), entry.data[CONF_HOST],
                          entry.data[CONF_PORT], entry.data[CONF_TOKEN])
    coordinator = JarvisCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_probe()
    coordinator.microphone = MicrophoneCoordinator(hass, client)
    await coordinator.microphone.async_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry):
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return ok


def _get_coordinator(hass, entry_id) -> JarvisCoordinator:
    loaded = hass.data.get(DOMAIN, {})
    try:
        return loaded[resolve_entry_id(loaded, entry_id)]
    except EntryLookupError as exc:
        raise ServiceValidationError(str(exc)) from exc


def _register_services(hass):
    common = {vol.Optional(CONF_ENTRY_ID): cv.string}

    def coord(call) -> JarvisCoordinator:
        return _get_coordinator(hass, call.data.get(CONF_ENTRY_ID))

    async def handle_speak(call):
        await coord(call).client.post("/q1/speak", {"text": call.data["text"]}, timeout=75)

    async def handle_listen(call):
        return await coord(call).listen(call.data.get("seconds", 12))

    async def handle_transcribe(call):
        return await coord(call).transcribe(call.data.get("seconds", 12))

    async def handle_call(call):
        await coord(call).client.post(
            "/dial", {"prompt": call.data.get("prompt", ""), "camera_entity": call.data.get("camera_entity"),
                      "member": call.data.get("member")}, timeout=25)

    async def handle_dtmf(call):
        await coord(call).client.post("/dtmf", {"digits": call.data["digits"]}, timeout=70)

    async def handle_hangup(call):
        await coord(call).client.post("/hangup")

    async def handle_led(call):
        await coord(call).client.post("/q1/led", {"color": call.data["color"]})

    async def handle_gps(call):
        await coord(call).client.post("/q1/gps/start", {"assisted": call.data.get("assisted", False)})

    async def handle_probe(call):
        c = coord(call)
        try:
            return await c.async_probe(raise_errors=True)
        except JarvisError as exc:
            raise HomeAssistantError(f"JARVIS hardware probe failed: {exc}") from exc

    seconds = vol.All(vol.Coerce(int), vol.Range(min=3, max=30))
    hass.services.async_register(DOMAIN, "speak", handle_speak, schema=vol.Schema({
        **common, vol.Required("text"): vol.All(str, vol.Length(min=1, max=2000))}))
    hass.services.async_register(DOMAIN, "listen", handle_listen, schema=vol.Schema({
        **common, vol.Optional("seconds", default=12): seconds}),
        supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "transcribe", handle_transcribe, schema=vol.Schema({
        **common, vol.Optional("seconds", default=12): seconds}),
        supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "call", handle_call, schema=vol.Schema({
        **common, vol.Optional("prompt", default=""): vol.All(str, vol.Length(max=1200)),
        vol.Optional("camera_entity"): cv.entity_id,
        vol.Optional("member"): vol.All(str, vol.Length(min=1, max=60))}))
    hass.services.async_register(DOMAIN, "hangup", handle_hangup, schema=vol.Schema(common))
    hass.services.async_register(DOMAIN, "send_dtmf", handle_dtmf, schema=vol.Schema({
        **common, vol.Required("digits"): vol.All(str, vol.Match(r"^[0-9*#A-D]{1,16}$"))}))
    hass.services.async_register(DOMAIN, "set_led", handle_led, schema=vol.Schema({
        **common, vol.Required("color"): vol.In(
            ["red", "green", "blue", "white", "pink", "yellow", "cyan", "off"])}))
    hass.services.async_register(DOMAIN, "gps_start", handle_gps, schema=vol.Schema({
        **common, vol.Optional("assisted", default=False): cv.boolean}))
    hass.services.async_register(DOMAIN, "probe", handle_probe, schema=vol.Schema(common),
                                 supports_response=SupportsResponse.ONLY)
