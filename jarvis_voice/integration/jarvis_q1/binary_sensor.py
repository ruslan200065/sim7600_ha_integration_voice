"""Q1 microphone configuration indicator."""
from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

from .const import DOMAIN
from .entity import JarvisEntity


async def async_setup_entry(hass, entry, async_add_entities):
    c = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MicrophoneConfigured(c), MicrophoneSound(c.microphone)])


class MicrophoneConfigured(JarvisEntity, BinarySensorEntity):
    def __init__(self, coordinator):
        super().__init__(coordinator, "q1_mic")

    @property
    def is_on(self):
        d = self.coordinator.data or {}
        return bool((d.get("capabilities") or {}).get("microphone_configured"))


class MicrophoneSound(JarvisEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.SOUND

    def __init__(self, coordinator):
        super().__init__(coordinator, "microphone_sound")

    @property
    def is_on(self):
        return (self.coordinator.data or {}).get("sound")
