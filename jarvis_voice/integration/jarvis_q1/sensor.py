"""Q1 call state, signal, GPS coordinates and transcribed phrases as HA sensors."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory, PERCENTAGE, SIGNAL_STRENGTH_DECIBELS_MILLIWATT
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .entity import JarvisEntity
from .helpers import MAX_STATE_LENGTH, csq_to_dbm, parse_csq, truncate_state


async def async_setup_entry(hass, entry, async_add_entities):
    c = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        CallStateSensor(c),
        SignalCsqSensor(c),
        SignalDbmSensor(c),
        GpsSensor(c, "gps_latitude", "latitude"),
        GpsSensor(c, "gps_longitude", "longitude"),
        TranscriptSensor(c),
        MicrophoneLevel(c.microphone),
    ])


class CallStateSensor(JarvisEntity, SensorEntity):
    _attr_icon = "mdi:phone"

    def __init__(self, coordinator):
        super().__init__(coordinator, "call_state")

    @property
    def native_value(self):
        return truncate_state((self.coordinator.data or {}).get("call_state") or "unknown")


class SignalCsqSensor(JarvisEntity, SensorEntity):
    _attr_icon = "mdi:signal"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator):
        super().__init__(coordinator, "signal")

    @property
    def native_value(self):
        return parse_csq((self.coordinator.data or {}).get("signal"))


class SignalDbmSensor(JarvisEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator):
        super().__init__(coordinator, "signal_dbm")

    @property
    def native_value(self):
        return csq_to_dbm(parse_csq((self.coordinator.data or {}).get("signal")))


class GpsSensor(JarvisEntity, SensorEntity):
    _attr_icon = "mdi:crosshairs-gps"

    def __init__(self, coordinator, key, field):
        super().__init__(coordinator, key)
        self._field = field

    @property
    def native_value(self):
        return ((self.coordinator.data or {}).get("gps") or {}).get(self._field)


class TranscriptSensor(JarvisEntity, RestoreEntity, SensorEntity):
    """State is capped at 255 chars; full text and answer live in attributes."""

    _attr_icon = "mdi:text-recognition"
    _unrecorded_attributes = frozenset({"full_text", "response"})

    def __init__(self, coordinator):
        super().__init__(coordinator, "last_transcript")

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        if self.coordinator.last_transcript:
            return
        last = await self.async_get_last_state()
        if last is None or last.state in ("unknown", "unavailable", ""):
            return
        attrs = last.attributes or {}
        self.coordinator.last_transcript = str(attrs.get("full_text") or last.state)
        self.coordinator.last_answer = str(attrs.get("response") or "")

    @property
    def native_value(self):
        return truncate_state(self.coordinator.last_transcript, MAX_STATE_LENGTH)

    @property
    def extra_state_attributes(self):
        text = self.coordinator.last_transcript or ""
        return {
            "full_text": text,
            "response": self.coordinator.last_answer or "",
            "truncated": len(text) > MAX_STATE_LENGTH,
        }


class MicrophoneLevel(JarvisEntity, SensorEntity):
    """Live mic level: no long-term statistics and disabled by default."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = None
    _attr_icon = "mdi:microphone"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator):
        super().__init__(coordinator, "microphone_level")

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("level")
