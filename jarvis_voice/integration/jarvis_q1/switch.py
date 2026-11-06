"""Enable local microphone monitoring in Home Assistant."""
from homeassistant.components.switch import SwitchEntity
from .entity import JarvisEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data["jarvis_q1"][entry.entry_id].microphone
    async_add_entities([MicrophoneSwitch(coordinator)])


class MicrophoneSwitch(JarvisEntity, SwitchEntity):
    _attr_icon = "mdi:microphone"

    def __init__(self, coordinator):
        super().__init__(coordinator, "microphone_monitor", "Microphone monitor")

    @property
    def available(self):
        return super().available and bool((self.coordinator.data or {}).get("configured"))

    @property
    def is_on(self):
        return (self.coordinator.data or {}).get("enabled")

    async def async_turn_on(self, **kwargs):
        await self.coordinator.set_enabled(True)

    async def async_turn_off(self, **kwargs):
        await self.coordinator.set_enabled(False)
