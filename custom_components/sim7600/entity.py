"""Base entity for the SIM7600 integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import SIM7600DataUpdateCoordinator


def device_info_for(
    coordinator: SIM7600DataUpdateCoordinator, entry: ConfigEntry
) -> DeviceInfo:
    """Build device info from the values read from the modem."""
    manufacturer = coordinator.manufacturer
    model = coordinator.model
    firmware = coordinator.firmware
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="SIM7600 Modem",
        manufacturer=manufacturer if isinstance(manufacturer, str) else MANUFACTURER,
        model=model if isinstance(model, str) else "SIM7600 Series",
        sw_version=firmware if isinstance(firmware, str) else None,
    )


class SIM7600Entity(CoordinatorEntity[SIM7600DataUpdateCoordinator]):
    """Common base for SIM7600 entities."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: SIM7600DataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._attr_device_info = device_info_for(coordinator, entry)
