"""Tests for the SIM7600 config and options flow."""

from unittest.mock import MagicMock, patch

import pytest
from homeassistant import config_entries, data_entry_flow
from homeassistant.components import usb
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sim7600.config_flow import (
    MANUAL_PATH_OPTION,
    CannotConnect,
    _read_until_final,
)
from custom_components.sim7600.const import (
    CONF_BALANCE_INTERVAL,
    CONF_BALANCE_USSD,
    CONF_BAUD_RATE,
    CONF_DEBUG_MODE,
    CONF_DEFAULT_RECIPIENT,
    CONF_GNSS_INTERVAL,
    CONF_MANUAL_PATH,
    CONF_POLLING_INTERVAL,
    CONF_SERIAL_PORT,
    DEFAULT_GNSS_INTERVAL,
    DEFAULT_POLLING_INTERVAL,
    DOMAIN,
)

IMEI = "861234567890123"
PROBE = "custom_components.sim7600.config_flow.probe_modem"
USER_INPUT = {
    CONF_SERIAL_PORT: "/dev/ttyUSB2",
    CONF_BAUD_RATE: 115200,
    CONF_POLLING_INTERVAL: 30,
    CONF_GNSS_INTERVAL: 120,
    CONF_DEBUG_MODE: False,
}


@pytest.fixture(autouse=True)
def mock_serial_list():
    """Mock the serial port list."""
    with patch("serial.tools.list_ports.comports") as mock:
        port = MagicMock()
        port.device = "/dev/ttyUSB2"
        port.description = "SIM7600 AT Port"
        port.serial_number = "USBSERIAL1"
        mock.return_value = [port]
        yield mock


@pytest.fixture(autouse=True)
def mock_setup_entry():
    """Do not set up the integration when an entry is created."""
    with patch("custom_components.sim7600.async_setup_entry", return_value=True):
        yield


@pytest.fixture
def mock_probe():
    """Probe answers OK with an IMEI."""
    with patch(PROBE, return_value=IMEI) as mock:
        yield mock


def _discovery() -> usb.UsbServiceInfo:
    return usb.UsbServiceInfo(
        device="/dev/ttyUSB2",
        vid="1E0E",
        pid="9001",
        serial_number="123456",
        manufacturer="SIMTech",
        description="SIM7600",
    )


async def test_flow_init(hass: HomeAssistant) -> None:
    """The user step shows a form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_no_devices_abort(hass: HomeAssistant, mock_serial_list) -> None:
    """No serial ports aborts with no_devices."""
    mock_serial_list.return_value = []
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "no_devices"


async def test_user_flow_success_imei_unique_id(
    hass: HomeAssistant, mock_probe
) -> None:
    """The IMEI from AT+CGSN becomes the unique ID."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SERIAL_PORT] == "/dev/ttyUSB2"
    assert result["data"][CONF_POLLING_INTERVAL] == 30
    assert result["result"].unique_id == IMEI
    mock_probe.assert_called_once_with("/dev/ttyUSB2", 115200)


async def test_user_flow_falls_back_to_usb_serial(hass: HomeAssistant) -> None:
    """Without IMEI the USB serial number is used."""
    with patch(PROBE, return_value=None):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["result"].unique_id == "USBSERIAL1"


async def test_user_flow_cannot_connect(hass: HomeAssistant) -> None:
    """A port that does not answer AT/OK shows cannot_connect."""
    with patch(PROBE, side_effect=CannotConnect("no OK")):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_already_configured(hass: HomeAssistant, mock_probe) -> None:
    """The same IMEI aborts with already_configured."""
    MockConfigEntry(domain=DOMAIN, unique_id=IMEI, data=USER_INPUT).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_manual_path(hass: HomeAssistant, mock_probe) -> None:
    """A manually entered path is probed and stored."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_SERIAL_PORT: MANUAL_PATH_OPTION}
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "manual"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_MANUAL_PATH: " /dev/serial/by-id/usb-SimTech-if02 ",
            CONF_BAUD_RATE: 115200,
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SERIAL_PORT] == "/dev/serial/by-id/usb-SimTech-if02"
    assert CONF_MANUAL_PATH not in result["data"]


async def test_usb_discovery_uses_serial_by_id(hass: HomeAssistant, mock_probe) -> None:
    """Discovery resolves the by-id path, probes it and shows the confirm form."""
    with patch(
        "homeassistant.components.usb.get_serial_by_id",
        return_value="/dev/serial/by-id/usb-SimTech-if02",
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USB}, data=_discovery()
        )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"
    mock_probe.assert_called_with("/dev/serial/by-id/usb-SimTech-if02", 115200)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_POLLING_INTERVAL: DEFAULT_POLLING_INTERVAL,
            CONF_GNSS_INTERVAL: DEFAULT_GNSS_INTERVAL,
            CONF_DEBUG_MODE: False,
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SERIAL_PORT] == "/dev/serial/by-id/usb-SimTech-if02"
    assert result["result"].unique_id == IMEI
    # Probed again in discovery_confirm.
    assert mock_probe.call_count == 2


async def test_usb_discovery_non_at_port_aborts(hass: HomeAssistant) -> None:
    """Interfaces that do not answer AT (NMEA, audio) are ignored."""
    with patch(PROBE, side_effect=CannotConnect("no OK")):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USB}, data=_discovery()
        )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "not_at_port"


async def test_usb_discovery_already_configured_updates_port(
    hass: HomeAssistant, mock_probe
) -> None:
    """A known modem on a new path aborts and updates the stored port."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=IMEI, data={**USER_INPUT, CONF_SERIAL_PORT: "/old"}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USB}, data=_discovery()
    )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_SERIAL_PORT] == "/dev/ttyUSB2"


async def test_discovery_confirm_probe_failure(hass: HomeAssistant) -> None:
    """If the port stops answering before confirmation an error is shown."""
    with patch(PROBE, side_effect=[IMEI, CannotConnect("gone")]):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USB}, data=_discovery()
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


# --- Options flow ---


async def _options_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=IMEI, data=USER_INPUT)
    entry.add_to_hass(hass)
    return entry


async def test_options_flow(hass: HomeAssistant) -> None:
    """Options store intervals, default recipient and balance USSD."""
    entry = await _options_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_POLLING_INTERVAL: 30,
            CONF_GNSS_INTERVAL: 120,
            CONF_DEBUG_MODE: True,
            CONF_DEFAULT_RECIPIENT: " +79991234567 ",
            CONF_BALANCE_USSD: "*100#",
            CONF_BALANCE_INTERVAL: 6,
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_POLLING_INTERVAL] == 30
    assert result["data"][CONF_DEBUG_MODE] is True
    assert result["data"][CONF_DEFAULT_RECIPIENT] == "+79991234567"
    assert result["data"][CONF_BALANCE_USSD] == "*100#"


async def test_options_flow_validates_number_and_ussd(hass: HomeAssistant) -> None:
    """Invalid recipient and USSD code show field errors."""
    entry = await _options_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_DEFAULT_RECIPIENT: '+7"AT+CFUN=0',
            CONF_BALANCE_USSD: "*100#;ATD",
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {
        CONF_DEFAULT_RECIPIENT: "invalid_number",
        CONF_BALANCE_USSD: "invalid_ussd",
    }


async def test_options_flow_empty_recipient_allowed(hass: HomeAssistant) -> None:
    """Leaving recipient and USSD empty disables both features."""
    entry = await _options_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DEFAULT_RECIPIENT] == ""
    assert result["data"][CONF_BALANCE_USSD] == ""


# --- Probe helper ---


def test_read_until_final_skips_echo() -> None:
    """The probe ignores echo lines and stops at OK."""
    ser = MagicMock()
    ser.readline.side_effect = [b"AT\r\n", b"\r\n", b"OK\r\n", b"never\r\n"]
    assert _read_until_final(ser, 1.0) == ["OK"]
