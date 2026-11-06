"""Tests für den SIM7600 Konfigurations-Flow."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries, data_entry_flow
from homeassistant.components import usb

from custom_components.sim7600.const import (
    CONF_BAUD_RATE,
    CONF_DEBUG_MODE,
    CONF_GNSS_INTERVAL,
    CONF_POLLING_INTERVAL,
    CONF_SERIAL_PORT,
    DEFAULT_GNSS_INTERVAL,
    DEFAULT_POLLING_INTERVAL,
    DOMAIN,
)


@pytest.fixture(autouse=True)
def mock_serial_list():
    """Mock der seriellen Port-Liste."""
    with patch("serial.tools.list_ports.comports") as mock:
        port = MagicMock()
        port.device = "/dev/ttyUSB2"
        port.description = "SIM7600 AT Port"
        mock.return_value = [port]
        yield mock


@pytest.fixture
def mock_serial():
    """Mock für serial.Serial."""
    with patch("serial.Serial") as mock:
        yield mock


@pytest.fixture(autouse=True)
def mock_async_serial():
    """Keep entry setup from registering a mock file descriptor with asyncio."""
    reader = AsyncMock()
    reader.readline.return_value = b"OK\r\n"
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    with patch(
        "serial_asyncio_fast.open_serial_connection", return_value=(reader, writer)
    ):
        yield


# --- Bestehende Tests ---


async def test_flow_init(hass) -> None:
    """Config-Dialog startet korrekt."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_user_flow_success(hass, mock_serial) -> None:
    """Erfolgreicher Config-Flow erstellt Config-Entry mit allen Feldern."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_SERIAL_PORT: "/dev/ttyUSB2",
            CONF_BAUD_RATE: 115200,
            CONF_POLLING_INTERVAL: 30,
            CONF_GNSS_INTERVAL: 120,
            CONF_DEBUG_MODE: False,
        },
    )

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "/dev/ttyUSB2"
    assert result["data"][CONF_SERIAL_PORT] == "/dev/ttyUSB2"
    assert result["data"][CONF_POLLING_INTERVAL] == 30
    assert result["data"][CONF_GNSS_INTERVAL] == 120


async def test_user_flow_cannot_connect(hass, mock_serial) -> None:
    """Verbindungsfehler zeigt Fehlermeldung."""
    mock_serial.side_effect = Exception("Connection failed")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_SERIAL_PORT: "/dev/ttyUSB2",
            CONF_BAUD_RATE: 115200,
        },
    )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_usb_discovery(hass) -> None:
    """USB-Discovery-Flow zeigt Bestätigungsformular."""
    discovery_info = usb.UsbServiceInfo(
        device="/dev/ttyUSB2",
        vid="1E0E",
        pid="9001",
        serial_number="123456",
        manufacturer="SIMTech",
        description="SIM7600",
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USB}, data=discovery_info
    )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"


async def test_usb_discovery_creates_entry_with_defaults(hass) -> None:
    """USB-Discovery erstellt Config-Entry mit Standard-Intervallen."""
    discovery_info = usb.UsbServiceInfo(
        device="/dev/ttyUSB2",
        vid="1E0E",
        pid="9001",
        serial_number="123456",
        manufacturer="SIMTech",
        description="SIM7600",
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USB}, data=discovery_info
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_POLLING_INTERVAL: DEFAULT_POLLING_INTERVAL,
            CONF_GNSS_INTERVAL: DEFAULT_GNSS_INTERVAL,
            CONF_DEBUG_MODE: False,
        },
    )

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SERIAL_PORT] == "/dev/ttyUSB2"
    assert result["data"][CONF_POLLING_INTERVAL] == DEFAULT_POLLING_INTERVAL
    assert result["data"][CONF_GNSS_INTERVAL] == DEFAULT_GNSS_INTERVAL


# --- Neue Tests: Options-Flow ---


async def test_options_flow_changes_polling_interval(hass, mock_serial) -> None:
    """Options-Flow erlaubt Änderung des Polling-Intervals."""
    # Config-Entry via Flow erstellen
    flow_result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    flow_result = await hass.config_entries.flow.async_configure(
        flow_result["flow_id"],
        {
            CONF_SERIAL_PORT: "/dev/ttyUSB2",
            CONF_BAUD_RATE: 115200,
            CONF_POLLING_INTERVAL: 60,
            CONF_GNSS_INTERVAL: 300,
            CONF_DEBUG_MODE: False,
        },
    )
    assert flow_result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    entry = flow_result["result"]

    # Options-Flow starten
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == data_entry_flow.FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_POLLING_INTERVAL: 30,
            CONF_GNSS_INTERVAL: 120,
            CONF_DEBUG_MODE: True,
        },
    )

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_POLLING_INTERVAL] == 30
    assert result["data"][CONF_GNSS_INTERVAL] == 120
    assert result["data"][CONF_DEBUG_MODE] is True
