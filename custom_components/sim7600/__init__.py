"""The SIM7600 integration."""

from __future__ import annotations

import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_BAUD_RATE,
    CONF_DEBUG_MODE,
    CONF_SERIAL_PORT,
    DOMAIN,
    LOGGER,
)
from .coordinator import SIM7600DataUpdateCoordinator, entry_option
from .modem import SIM7600Modem
from .services import register_services

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.NOTIFY]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# unique_id suffixes used before 0.5.0 (class names) -> new description keys.
LEGACY_UNIQUE_ID_SUFFIXES = {
    "SIM7600SignalSensor": "signal_strength",
    "SIM7600OperatorSensor": "operator",
    "SIM7600NetworkModeSensor": "network_mode",
    "SIM7600SystemModeSensor": "system_mode",
    "SIM7600IMEISensor": "imei",
    "SIM7600FirmwareSensor": "firmware_version",
    "SIM7600SIMStatusSensor": "sim_status",
    "SIM7600LastSMSSensor": "last_sms",
}
LEGACY_REMOVED_SUFFIXES = {
    "GPS",
    "speed",
    "altitude",
    "gnss_timestamp",
    "gps_last_fix",
    "SIM7600SpeedSensor",
    "SIM7600AltitudeSensor",
    "SIM7600DateSensor",
    "SIM7600TimeSensor",
}


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration services once."""
    register_services(hass)
    return True


@callback
def _async_migrate_unique_ids(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Move pre-0.5.0 unique_ids (class names) to stable keys."""
    registry = er.async_get(hass)
    prefix = f"{entry.entry_id}_"
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if not reg_entry.unique_id.startswith(prefix):
            continue
        suffix = reg_entry.unique_id[len(prefix) :]
        if suffix in LEGACY_REMOVED_SUFFIXES:
            LOGGER.debug("Removing retired entity %s", reg_entry.entity_id)
            registry.async_remove(reg_entry.entity_id)
            continue
        new_key = LEGACY_UNIQUE_ID_SUFFIXES.get(suffix)
        if new_key is None:
            continue
        new_unique_id = f"{prefix}{new_key}"
        if registry.async_get_entity_id(reg_entry.domain, DOMAIN, new_unique_id):
            continue
        LOGGER.debug("Migrating %s to unique_id %s", reg_entry.entity_id, new_unique_id)
        registry.async_update_entity(reg_entry.entity_id, new_unique_id=new_unique_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the integration from a config entry."""
    modem = SIM7600Modem(
        entry.data[CONF_SERIAL_PORT],
        entry.data[CONF_BAUD_RATE],
        debug=bool(entry_option(entry, CONF_DEBUG_MODE, False)),
    )
    coordinator = SIM7600DataUpdateCoordinator(hass, modem, entry)

    try:
        await coordinator.async_config_entry_first_refresh()
        entry.runtime_data = coordinator
        _async_migrate_unique_ids(hass, entry)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except (Exception, asyncio.CancelledError):
        # Stop the refresh source before releasing the port for the next retry.
        try:
            await coordinator.async_shutdown()
        finally:
            await modem.close()
        raise

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload after options changed."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and close the serial port for good."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: SIM7600DataUpdateCoordinator = entry.runtime_data
        await coordinator.modem.close()
    return unload_ok
