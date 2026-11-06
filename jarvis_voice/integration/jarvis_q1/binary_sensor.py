"""Q1 microphone configuration and GPS lock indications."""
from homeassistant.components.binary_sensor import BinarySensorEntity
from .entity import JarvisEntity

async def async_setup_entry(hass,entry,async_add_entities):
    c=hass.data["jarvis_q1"][entry.entry_id]
    async_add_entities([Q1Flag(c,"gps_fix","GPS fix"),Q1Flag(c,"q1_mic","Microphone configured")])

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
