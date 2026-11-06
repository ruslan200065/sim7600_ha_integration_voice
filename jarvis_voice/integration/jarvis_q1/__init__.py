"""Custom companion automatically copied by JARVIS Voice Home Assistant App."""
from __future__ import annotations

import logging
import voluptuous as vol
from homeassistant.const import Platform
from homeassistant.core import SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from .client import JarvisClient
from .coordinator import JarvisCoordinator
from .const import DOMAIN, CONF_HOST, CONF_PORT, CONF_TOKEN

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR,Platform.BINARY_SENSOR,Platform.BUTTON,Platform.CAMERA,Platform.MEDIA_PLAYER]

async def async_setup_entry(hass, entry):
    client = JarvisClient(async_get_clientsession(hass), entry.data[CONF_HOST],
                          entry.data[CONF_PORT], entry.data[CONF_TOKEN])
    coordinator = JarvisCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_probe()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not hass.services.has_service(DOMAIN, "speak"):
        _register_services(hass)
    return True

async def async_unload_entry(hass, entry):
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id,None)
        if not hass.data[DOMAIN]:
            for name in ("speak","listen","transcribe","call","hangup","set_led","gps_start","probe"):
                hass.services.async_remove(DOMAIN,name)
    return ok

def _get_coordinator(hass, entry_id):
    entries = hass.data.get(DOMAIN,{})
    if entry_id:
        return entries[entry_id]
    if len(entries)!=1:
        raise ValueError("Specify entry_id if multiple JARVIS gateways are installed")
    return next(iter(entries.values()))

def _register_services(hass):
    common={vol.Optional("entry_id"):cv.string}

    async def handle_speak(call):
        await _get_coordinator(hass,call.data.get("entry_id")).client.post(
            "/q1/speak",{"text":call.data["text"]},timeout=75)

    async def handle_listen(call):
        return await _get_coordinator(hass,call.data.get("entry_id")).listen(
            call.data.get("seconds",12))

    async def handle_transcribe(call):
        return await _get_coordinator(hass,call.data.get("entry_id")).transcribe(
            call.data.get("seconds",12))

    async def handle_call(call):
        await _get_coordinator(hass,call.data.get("entry_id")).client.post(
            "/dial",{"prompt":call.data.get("prompt","")},timeout=25)

    async def handle_hangup(call):
        await _get_coordinator(hass,call.data.get("entry_id")).client.post("/hangup")

    async def handle_led(call):
        await _get_coordinator(hass,call.data.get("entry_id")).client.post(
            "/q1/led",{"color":call.data["color"]})

    async def handle_gps(call):
        await _get_coordinator(hass,call.data.get("entry_id")).client.post(
            "/q1/gps/start",{"assisted":call.data.get("assisted",False)})

    async def handle_probe(call):
        c=_get_coordinator(hass,call.data.get("entry_id"))
        await c.async_probe()
        return c.probe

    hass.services.async_register(DOMAIN,"speak",handle_speak,schema=vol.Schema({
        **common,vol.Required("text"):vol.All(str,vol.Length(min=1,max=2000))
    }))
    hass.services.async_register(DOMAIN,"listen",handle_listen,schema=vol.Schema({
        **common,vol.Optional("seconds",default=12):vol.All(vol.Coerce(int),vol.Range(min=3,max=30))
    }),supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN,"transcribe",handle_transcribe,schema=vol.Schema({
        **common,vol.Optional("seconds",default=12):vol.All(vol.Coerce(int),vol.Range(min=3,max=30))
    }),supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN,"call",handle_call,schema=vol.Schema({
        **common,vol.Optional("prompt",default=""):vol.All(str,vol.Length(max=1200))
    }))
    hass.services.async_register(DOMAIN,"hangup",handle_hangup,schema=vol.Schema(common))
    hass.services.async_register(DOMAIN,"set_led",handle_led,schema=vol.Schema({
        **common,vol.Required("color"):vol.In(["red","green","blue","white","pink","yellow","cyan","off"])
    }))
    hass.services.async_register(DOMAIN,"gps_start",handle_gps,schema=vol.Schema({
        **common,vol.Optional("assisted",default=False):cv.boolean
    }))
    hass.services.async_register(DOMAIN,"probe",handle_probe,schema=vol.Schema(common),
                                 supports_response=SupportsResponse.ONLY)
