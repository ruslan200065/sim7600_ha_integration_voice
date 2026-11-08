"""Config flow for SIM7600 4G & GPS Gateway integration."""

from __future__ import annotations

import re
import time
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
    CONF_BALANCE_INTERVAL,
    CONF_BALANCE_USSD,
    CONF_BAUD_RATE,
    CONF_DEBUG_MODE,
    CONF_DEFAULT_RECIPIENT,
    CONF_GNSS_INTERVAL,
    CONF_MANUAL_PATH,
    CONF_POLLING_INTERVAL,
    CONF_SERIAL_PORT,
    DEFAULT_BALANCE_INTERVAL,
    DEFAULT_BAUD,
    DEFAULT_GNSS_INTERVAL,
    DEFAULT_POLLING_INTERVAL,
    DOMAIN,
    LOGGER,
)
from .sms_codec import PHONE_NUMBER_RE, USSD_CODE_RE

BAUD_RATES = [9600, 19200, 38400, 57600, 115200]
MANUAL_PATH_OPTION = "__manual__"
PROBE_TIMEOUT = 2.0
_IMEI_RE = re.compile(r"^\d{15}$")


class CannotConnect(HomeAssistantError):
    """The port cannot be opened or does not answer AT with OK."""


def _read_until_final(ser: serial.Serial, timeout: float) -> list[str]:
    deadline = time.monotonic() + timeout
    lines: list[str] = []
    while time.monotonic() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        line = raw.decode(errors="replace").strip()
        if not line or line.upper().startswith("AT"):
            continue
        lines.append(line)
        if line == "OK" or line.startswith(("ERROR", "+CME ERROR:", "+CMS ERROR:")):
            break
    return lines


def probe_modem(port: str, baudrate: int) -> str | None:
    """Check the port answers AT with OK; return the IMEI if available.

    Runs in the executor. Raises CannotConnect.
    """
    try:
        with serial.Serial(port, baudrate, timeout=0.5, write_timeout=2) as ser:
            ser.reset_input_buffer()
            ser.write(b"ATE0\r")
            _read_until_final(ser, PROBE_TIMEOUT)
            ser.reset_input_buffer()
            ser.write(b"AT\r")
            if "OK" not in _read_until_final(ser, PROBE_TIMEOUT):
                raise CannotConnect(f"{port} did not answer AT with OK")
            ser.write(b"AT+CGSN\r")
            for line in _read_until_final(ser, PROBE_TIMEOUT):
                if _IMEI_RE.match(line):
                    return line
    except (serial.SerialException, OSError) as err:
        raise CannotConnect(str(err)) from err
    return None


def _interval_schema(
    polling_interval: int = DEFAULT_POLLING_INTERVAL,
    gnss_interval: int = DEFAULT_GNSS_INTERVAL,
    debug_mode: bool = False,
) -> dict[Any, Any]:
    return {
        vol.Optional(CONF_POLLING_INTERVAL, default=polling_interval): vol.All(
            vol.Coerce(int), vol.Range(min=15)
        ),
        vol.Optional(CONF_GNSS_INTERVAL, default=gnss_interval): vol.All(
            vol.Coerce(int), vol.Range(min=15)
        ),
        vol.Optional(CONF_DEBUG_MODE, default=debug_mode): bool,
    }


class Sim7600ConfigFlow(ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Handle a config flow for SIM7600 4G & GPS Gateway."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._discovered_port: str | None = None
        self._usb_serial: str | None = None
        self._ports: dict[str, str] = {}
        self._port_serials: dict[str, str | None] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> Sim7600OptionsFlow:
        """Return the options flow handler."""
        return Sim7600OptionsFlow()

    async def _async_probe(self, port: str, baud: int) -> str | None:
        return await self.hass.async_add_executor_job(probe_modem, port, baud)

    async def _async_create(
        self, port: str, data: dict[str, Any], imei: str | None, usb_serial: str | None
    ) -> ConfigFlowResult:
        unique_id = imei or usb_serial or port
        await self.async_set_unique_id(unique_id, raise_on_progress=False)
        self._abort_if_unique_id_configured(updates={CONF_SERIAL_PORT: port})
        data = {**data, CONF_SERIAL_PORT: port}
        data.pop(CONF_MANUAL_PATH, None)
        return self.async_create_entry(title=f"SIM7600 ({port})", data=data)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select a serial port (or choose manual entry)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            port = user_input[CONF_SERIAL_PORT]
            if port == MANUAL_PATH_OPTION:
                return await self.async_step_manual()
            try:
                imei = await self._async_probe(port, user_input[CONF_BAUD_RATE])
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return await self._async_create(
                    port, user_input, imei, self._port_serials.get(port)
                )

        ports = await self.hass.async_add_executor_job(serial.tools.list_ports.comports)
        if not ports:
            return self.async_abort(reason="no_devices")
        self._ports = {}
        self._port_serials = {}
        for port in ports:
            self._ports[port.device] = f"{port.device} ({port.description})"
            self._port_serials[port.device] = getattr(port, "serial_number", None)
        options = {**self._ports, MANUAL_PATH_OPTION: "Enter path manually"}

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SERIAL_PORT): vol.In(options),
                    vol.Required(CONF_BAUD_RATE, default=DEFAULT_BAUD): vol.In(
                        BAUD_RATES
                    ),
                    **_interval_schema(),
                }
            ),
            errors=errors,
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Enter a serial port path manually."""
        errors: dict[str, str] = {}
        if user_input is not None and CONF_MANUAL_PATH in user_input:
            port = user_input[CONF_MANUAL_PATH].strip()
            try:
                imei = await self._async_probe(port, user_input[CONF_BAUD_RATE])
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return await self._async_create(port, user_input, imei, None)

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MANUAL_PATH): str,
                    vol.Required(CONF_BAUD_RATE, default=DEFAULT_BAUD): vol.In(
                        BAUD_RATES
                    ),
                    **_interval_schema(),
                }
            ),
            errors=errors,
        )

    async def async_step_usb(
        self, discovery_info: usb.UsbServiceInfo
    ) -> ConfigFlowResult:
        """Handle USB discovery.

        The SIM7600 exposes several serial interfaces; only the AT port
        answers "AT" with OK, the others are aborted.
        """
        LOGGER.debug("USB discovery: %s", discovery_info)
        port = await self.hass.async_add_executor_job(
            usb.get_serial_by_id, discovery_info.device
        )
        try:
            imei = await self._async_probe(port, DEFAULT_BAUD)
        except CannotConnect:
            return self.async_abort(reason="not_at_port")

        self._discovered_port = port
        self._usb_serial = discovery_info.serial_number
        await self.async_set_unique_id(imei or discovery_info.serial_number or port)
        self._abort_if_unique_id_configured(updates={CONF_SERIAL_PORT: port})
        self.context["title_placeholders"] = {"port": port}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a discovered modem and set the polling intervals."""
        if self._discovered_port is None:
            return self.async_abort(reason="unknown")
        port = self._discovered_port
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                imei = await self._async_probe(port, DEFAULT_BAUD)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                return await self._async_create(
                    port,
                    {CONF_BAUD_RATE: DEFAULT_BAUD, **user_input},
                    imei,
                    self._usb_serial,
                )

        return self.async_show_form(
            step_id="discovery_confirm",
            data_schema=vol.Schema(_interval_schema()),
            description_placeholders={"port": port},
            errors=errors,
        )


class Sim7600OptionsFlow(OptionsFlow):
    """Options: intervals, debug, default SMS recipient, balance USSD."""

    def _current(self, key: str, default: Any) -> Any:
        entry = self.config_entry
        value = entry.options.get(key)
        if value is None:
            value = entry.data.get(key, default)
        return default if value is None else value

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the options form."""
        errors: dict[str, str] = {}
        if user_input is not None:
            recipient = str(user_input.get(CONF_DEFAULT_RECIPIENT) or "").strip()
            ussd = str(user_input.get(CONF_BALANCE_USSD) or "").strip()
            if recipient and not PHONE_NUMBER_RE.fullmatch(recipient):
                errors[CONF_DEFAULT_RECIPIENT] = "invalid_number"
            if ussd and not USSD_CODE_RE.fullmatch(ussd):
                errors[CONF_BALANCE_USSD] = "invalid_ussd"
            if not errors:
                return self.async_create_entry(
                    title="",
                    data={
                        **user_input,
                        CONF_DEFAULT_RECIPIENT: recipient,
                        CONF_BALANCE_USSD: ussd,
                    },
                )

        schema = vol.Schema(
            {
                **_interval_schema(
                    polling_interval=self._current(
                        CONF_POLLING_INTERVAL, DEFAULT_POLLING_INTERVAL
                    ),
                    gnss_interval=self._current(
                        CONF_GNSS_INTERVAL, DEFAULT_GNSS_INTERVAL
                    ),
                    debug_mode=self._current(CONF_DEBUG_MODE, False),
                ),
                vol.Optional(
                    CONF_DEFAULT_RECIPIENT,
                    description={
                        "suggested_value": self._current(CONF_DEFAULT_RECIPIENT, "")
                    },
                ): str,
                vol.Optional(
                    CONF_BALANCE_USSD,
                    description={
                        "suggested_value": self._current(CONF_BALANCE_USSD, "")
                    },
                ): str,
                vol.Optional(
                    CONF_BALANCE_INTERVAL,
                    default=self._current(
                        CONF_BALANCE_INTERVAL, DEFAULT_BALANCE_INTERVAL
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=168)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
