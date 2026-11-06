"""Q1 call state, GPS coordinates and transcribed phrases as HA sensors."""
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from .entity import JarvisEntity

SENSORS = {
    "call_state":("Call state",None),
    "signal":("Cellular signal (CSQ)",None),
    "gps_latitude":("GPS latitude",None),
    "gps_longitude":("GPS longitude",None),
    "last_transcript":("Last microphone transcript",None),
}

async def async_setup_entry(hass,entry,async_add_entities):
    c=hass.data["jarvis_q1"][entry.entry_id]
    async_add_entities([Q1Sensor(c,key,*info) for key,info in SENSORS.items()])

class Q1Sensor(JarvisEntity,SensorEntity):
    def __init__(self,coordinator,key,name,device_class):
        super().__init__(coordinator,key,name)
        self.key=key
        if device_class:
            self._attr_device_class=device_class

    @property
    def native_value(self):
        data=self.coordinator.data or {}
        key=self.key
        if key=="call_state":
            return data.get("call_state","unknown")
        if key=="signal":
            csq=str(data.get("signal") or "")
            try:
                return int(csq.split(",")[0])
            except ValueError:
                return None
        if key=="gps_latitude":
            return (data.get("gps") or {}).get("latitude")
        if key=="gps_longitude":
            return (data.get("gps") or {}).get("longitude")
        if key=="last_transcript":
            return self.coordinator.last_transcript or None
        return None
