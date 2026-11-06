"""Q1 microphone configuration and GPS lock indications."""
from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from .entity import JarvisEntity

async def async_setup_entry(hass,entry,async_add_entities):
    c=hass.data["jarvis_q1"][entry.entry_id]
    async_add_entities([Q1Flag(c,"gps_fix","GPS fix"),Q1Flag(c,"q1_mic","Microphone configured"), MicrophoneSound(c.microphone)])

class Q1Flag(JarvisEntity,BinarySensorEntity):
    def __init__(self, coordinator, key, name):
        super().__init__(coordinator,key,name)
        self.key=key

    @property
    def is_on(self):
        d=self.coordinator.data or {}
        if self.key=="gps_fix":
            return bool((d.get("gps") or {}).get("fix"))
        return bool((d.get("capabilities") or {}).get("microphone_configured"))

class MicrophoneSound(JarvisEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.SOUND

    def __init__(self, coordinator):
        super().__init__(coordinator, "microphone_sound", "Microphone sound")

    @property
    def is_on(self):
        return (self.coordinator.data or {}).get("sound")
