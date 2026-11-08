"""Buttons for a microphone voice turn and GPS fix."""
from homeassistant.components.button import ButtonEntity

from .const import DOMAIN
from .entity import JarvisEntity


async def async_setup_entry(hass, entry, async_add_entities):
    c = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([Q1Button(c, "listen"), Q1Button(c, "gps_start"), Q1Button(c, "hangup")])


class Q1Button(JarvisEntity, ButtonEntity):
    def __init__(self, coordinator, key):
        super().__init__(coordinator, key)
        self.key = key

    @property
    def available(self):
        if not super().available:
            return False
        data = self.coordinator.data or {}
        if self.key == "listen":
            capabilities = data.get("capabilities") or {}
            return bool(capabilities.get("microphone_configured", False)
                        and capabilities.get("speech_configured", True))
        if self.key == "hangup":
            return data.get("call_state") in {"ringing", "answering", "dialing", "connected"}
        return data.get("call_state") != "offline"

    async def async_press(self):
        if self.key == "listen":
            await self.coordinator.listen()
        elif self.key == "gps_start":
            await self.coordinator.client.post("/q1/gps/start", {"assisted": False})
        else:
            await self.coordinator.client.post("/hangup", {})
        await self.coordinator.async_request_refresh()
