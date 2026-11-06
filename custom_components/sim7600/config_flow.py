"""Config flow for SIM7600 4G & GPS Gateway integration."""

from __future__ import annotations

from typing import Any

import serial
import serial.tools.list_ports
import voluptuous as vol
from homeassistant.components import usb
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError

from .const import (
    CONF_BAUD_RATE,
    CONF_DEBUG_MODE,
    CONF_GNSS_INTERVAL,
    CONF_POLLING_INTERVAL,
    CONF_SERIAL_PORT,
    DEFAULT_BAUD,
    DEFAULT_GNSS_INTERVAL,
    DEFAULT_POLLING_INTERVAL,
    DOMAIN,
    LOGGER,
)


def _options_schema(
    polling_interval: int = DEFAULT_POLLING_INTERVAL,
    gnss_interval: int = DEFAULT_GNSS_INTERVAL,
    debug_mode: bool = False,
) -> vol.Schema:
    """Schema für wiederverwendbare Options-Felder."""
    return vol.Schema(
        {
            vol.Optional(CONF_POLLING_INTERVAL, default=polling_interval): vol.All(
                vol.Coerce(int), vol.Range(min=15)
            ),
            vol.Optional(CONF_GNSS_INTERVAL, default=gnss_interval): vol.All(
                vol.Coerce(int), vol.Range(min=15)
            ),
            vol.Optional(CONF_DEBUG_MODE, default=debug_mode): bool,
        }
    )


class Sim7600ConfigFlow(ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Handle a config flow for SIM7600 4G & GPS Gateway."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._discovery_info: usb.UsbServiceInfo | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> Sim7600OptionsFlow:
        """Return the options flow handler."""
        return Sim7600OptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await self._validate_input(user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=user_input[CONF_SERIAL_PORT], data=user_input
                )

        ports = await self.hass.async_add_executor_job(serial.tools.list_ports.comports)
        list_of_ports = {
            port.device: f"{port.device} ({port.description})" for port in ports
        }

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SERIAL_PORT): vol.In(list_of_ports),
                    vol.Required(CONF_BAUD_RATE, default=DEFAULT_BAUD): vol.In(
                        [9600, 19200, 38400, 57600, 115200]
                    ),
                }
            ).extend(_options_schema().schema),
            errors=errors,
        )

    async def async_step_usb(
        self, discovery_info: usb.UsbServiceInfo
    ) -> ConfigFlowResult:
        """Handle USB discovery."""
        LOGGER.debug("USB discovery: %s", discovery_info)

        await self.async_set_unique_id(discovery_info.device)
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {"port": discovery_info.device}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm discovery — zeigt vollständiges Konfigurationsformular."""
        if self._discovery_info is None:
            return self.async_abort(reason="unknown")

        if user_input is not None:
            return self.async_create_entry(
                title=self._discovery_info.device,
                data={
                    CONF_SERIAL_PORT: self._discovery_info.device,
                    CONF_BAUD_RATE: DEFAULT_BAUD,
                    **user_input,
                },
            )

        return self.async_show_form(
            step_id="discovery_confirm",
            data_schema=_options_schema(),
            description_placeholders={"port": self._discovery_info.device},
        )

    async def _validate_input(self, data: dict[str, Any]) -> None:
        """Validate the user input allows us to connect."""
        port = data[CONF_SERIAL_PORT]
        baud = data[CONF_BAUD_RATE]

        def _check_port() -> None:
            try:
                ser = serial.Serial(port, baud, timeout=1)
                ser.close()
            except Exception as err:
                raise CannotConnect from err

        await self.hass.async_add_executor_job(_check_port)


class Sim7600OptionsFlow(OptionsFlow):
    """Handle options for SIM7600 integration.

    Erlaubt die nachträgliche Änderung von polling_interval, gnss_interval
    und debug_mode ohne Neu-Konfiguration der seriellen Verbindung.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the options form."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # Aktuelle Werte aus options (falls geändert) oder data (Ersteinrichtung)
        current_polling = self.config_entry.options.get(
            CONF_POLLING_INTERVAL,
            self.config_entry.data.get(CONF_POLLING_INTERVAL, DEFAULT_POLLING_INTERVAL),
        )
        current_gnss = self.config_entry.options.get(
            CONF_GNSS_INTERVAL,
            self.config_entry.data.get(CONF_GNSS_INTERVAL, DEFAULT_GNSS_INTERVAL),
        )
        current_debug = self.config_entry.options.get(
            CONF_DEBUG_MODE,
            self.config_entry.data.get(CONF_DEBUG_MODE, False),
        )

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(
                polling_interval=current_polling,
                gnss_interval=current_gnss,
                debug_mode=current_debug,
            ),
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""
