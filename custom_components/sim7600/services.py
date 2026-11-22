"""Home Assistant SMS and USSD services."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
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

from .const import (
    ATTR_CODE,
    ATTR_DEVICE_ID,
    ATTR_ENTRY_ID,
    ATTR_MESSAGE,
    ATTR_NUMBER,
    DOMAIN,
    SERVICE_SEND_SMS,
    SERVICE_SEND_USSD,
)
from .coordinator import SIM7600DataUpdateCoordinator
from .modem import InvalidInputError, ModemError, SIM7600Modem
from .sms_codec import PHONE_NUMBER_PATTERN, USSD_CODE_PATTERN

_TARGET_SCHEMA: dict[vol.Marker, object] = {
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


async def send_sms(modem: SIM7600Modem, number: str, message: str) -> None:
    """Send an SMS and translate errors for both Home Assistant entry points."""
    try:
        await modem.send_sms(number, message)
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


def register_services(hass: HomeAssistant) -> None:
    """Register the integration services once."""

    async def handle_send_sms(call: ServiceCall) -> None:
        coordinator = _resolve_coordinator(hass, call)
        await send_sms(
            coordinator.modem, call.data[ATTR_NUMBER], call.data[ATTR_MESSAGE]
        )

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
        if len(matching) != 1:
            raise _multiple_modems()
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
        raise _multiple_modems()
    coordinator: SIM7600DataUpdateCoordinator = loaded[entry_id].runtime_data
    return coordinator


def _multiple_modems() -> ServiceValidationError:
    return ServiceValidationError(
        "Several SIM7600 modems are loaded; set entry_id or device_id",
        translation_domain=DOMAIN,
        translation_key="multiple_modems",
    )
