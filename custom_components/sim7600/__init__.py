"""The SIM7600 integration."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_CODE,
    ATTR_DEVICE_ID,
    ATTR_ENTRY_ID,
    ATTR_MESSAGE,
    ATTR_NUMBER,
    CONF_BAUD_RATE,
    CONF_DEBUG_MODE,
    CONF_SERIAL_PORT,
    DOMAIN,
    LOGGER,
    SERVICE_SEND_SMS,
    SERVICE_SEND_USSD,
)
from .coordinator import SIM7600DataUpdateCoordinator, entry_option
from .modem import InvalidInputError, ModemError, SIM7600Modem
from .sms_codec import PHONE_NUMBER_PATTERN, USSD_CODE_PATTERN

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.DEVICE_TRACKER, Platform.NOTIFY]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_TARGET_SCHEMA = {
    vol.Optional(ATTR_ENTRY_ID): cv.string,
    vol.Optional(ATTR_DEVICE_ID): cv.string,
}
SEND_SMS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_NUMBER): vol.All(
            cv.string, vol.Strip, vol.Match(PHONE_NUMBER_PATTERN)
        ),
        vol.Required(ATTR_MESSAGE): cv.string,
        **_TARGET_SCHEMA,
    }
)
SEND_USSD_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CODE): vol.All(
            cv.string, vol.Strip, vol.Match(USSD_CODE_PATTERN)
        ),
        **_TARGET_SCHEMA,
    }
)

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
    "SIM7600SpeedSensor": "speed",
    "SIM7600AltitudeSensor": "altitude",
    "SIM7600DateSensor": "gnss_timestamp",
}
# Merged into gnss_timestamp.
LEGACY_REMOVED_SUFFIXES = ("SIM7600TimeSensor",)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration services once."""

    async def handle_send_sms(call: ServiceCall) -> None:
        coordinator = _resolve_coordinator(hass, call)
        try:
            await coordinator.modem.send_sms(
                call.data[ATTR_NUMBER], call.data[ATTR_MESSAGE]
            )
        except InvalidInputError as err:
            raise ServiceValidationError(
                str(err),
                translation_domain=DOMAIN,
                translation_key="invalid_input",
                translation_placeholders={"error": str(err)},
            ) from err
        except ModemError as err:
            raise HomeAssistantError(
                f"Sending SMS failed: {err}",
                translation_domain=DOMAIN,
                translation_key="send_sms_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def handle_send_ussd(call: ServiceCall) -> ServiceResponse:
        coordinator = _resolve_coordinator(hass, call)
        try:
            reply = await coordinator.modem.send_ussd(call.data[ATTR_CODE])
        except InvalidInputError as err:
            raise ServiceValidationError(
                str(err),
                translation_domain=DOMAIN,
                translation_key="invalid_input",
                translation_placeholders={"error": str(err)},
            ) from err
        except ModemError as err:
            raise HomeAssistantError(
                f"USSD request failed: {err}",
                translation_domain=DOMAIN,
                translation_key="send_ussd_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        return {"status": reply.status, "message": reply.message, "dcs": reply.dcs}

    hass.services.async_register(
        DOMAIN, SERVICE_SEND_SMS, handle_send_sms, schema=SEND_SMS_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_USSD,
        handle_send_ussd,
        schema=SEND_USSD_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    return True


@callback
def _resolve_coordinator(
    hass: HomeAssistant, call: ServiceCall
) -> SIM7600DataUpdateCoordinator:
    """Pick the loaded modem addressed by entry_id/device_id."""
    loaded = {
        entry.entry_id: entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    }
    entry_id: str | None = call.data.get(ATTR_ENTRY_ID)
    device_id: str | None = call.data.get(ATTR_DEVICE_ID)

    if device_id:
        device = dr.async_get(hass).async_get(device_id)
        matching = [
            eid for eid in (device.config_entries if device else ()) if eid in loaded
        ]
        if entry_id:
            matching = [eid for eid in matching if eid == entry_id]
        if not matching:
            raise ServiceValidationError(
                f"No loaded SIM7600 modem for device {device_id}",
                translation_domain=DOMAIN,
                translation_key="modem_not_found",
            )
        entry_id = matching[0]
    elif entry_id:
        if entry_id not in loaded:
            raise ServiceValidationError(
                f"No loaded SIM7600 modem with entry_id {entry_id}",
                translation_domain=DOMAIN,
                translation_key="modem_not_found",
            )
    elif len(loaded) == 1:
        entry_id = next(iter(loaded))
    elif not loaded:
        raise ServiceValidationError(
            "No SIM7600 modem is loaded",
            translation_domain=DOMAIN,
            translation_key="no_modem",
        )
    else:
        raise ServiceValidationError(
            "Several SIM7600 modems are loaded; set entry_id or device_id",
            translation_domain=DOMAIN,
            translation_key="multiple_modems",
        )
    coordinator: SIM7600DataUpdateCoordinator = loaded[entry_id].runtime_data
    return coordinator


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
            LOGGER.debug("Removing merged legacy entity %s", reg_entry.entity_id)
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
    except Exception:
        # Close the port so nothing reopens it before the next retry.
        await modem.close()
        raise

    entry.runtime_data = coordinator
    _async_migrate_unique_ids(hass, entry)
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
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
