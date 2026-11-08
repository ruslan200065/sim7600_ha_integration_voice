"""Support for SIM7600 device tracker."""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import SIM7600DataUpdateCoordinator
from .entity import SIM7600Entity
from .types import GpsData


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SIM7600 device tracker based on a config entry."""
    coordinator: SIM7600DataUpdateCoordinator = entry.runtime_data
    async_add_entities([SIM7600DeviceTracker(coordinator, entry)])


class SIM7600DeviceTracker(SIM7600Entity, TrackerEntity):
    """GPS position of the SIM7600 modem.

    The coordinator drops the fix after 10 minutes without a new one, so the
    location becomes unknown instead of showing a stale position.
    """

    _attr_name = None  # use the device name
    _attr_translation_key = "gps"

    def __init__(
        self, coordinator: SIM7600DataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the tracker."""
        super().__init__(coordinator, entry)
        # Unchanged since 0.1 to keep the existing entity.
        self._attr_unique_id = f"{entry.entry_id}_GPS"

    @property
    def _gps(self) -> GpsData | None:
        return self.coordinator.data.get("gps")

    @property
    def latitude(self) -> float | None:
        """Return latitude value of the device."""
        gps = self._gps
        return gps.latitude if gps is not None else None

    @property
    def longitude(self) -> float | None:
        """Return longitude value of the device."""
        gps = self._gps
        return gps.longitude if gps is not None else None

    @property
    def altitude(self) -> float | None:
        """Return altitude value of the device."""
        gps = self._gps
        return gps.altitude if gps is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        gps = self._gps
        if gps is None:
            return {}
        last_fix = self.coordinator.data.get("gps_last_fix")
        return {
            "date": gps.date,
            "time": gps.time,
            "speed": gps.speed,
            "speed_unit": "kn",
            "altitude": gps.altitude,
            "last_fix": last_fix.isoformat() if last_fix is not None else None,
        }
