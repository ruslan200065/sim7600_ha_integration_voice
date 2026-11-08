"""Tests for the SIM7600 coordinator."""

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.sim7600 import coordinator as coordinator_module
from custom_components.sim7600.const import (
    CONF_BALANCE_USSD,
    CONF_GNSS_INTERVAL,
    CONF_POLLING_INTERVAL,
    DOMAIN,
    EVENT_INCOMING_CALL,
    EVENT_SMS_RECEIVED,
    GPS_STALE_AFTER,
    NO_REGISTRATION_ISSUE_AFTER,
    SMS_CLEANUP_INTERVAL,
)
from custom_components.sim7600.coordinator import SIM7600DataUpdateCoordinator
from custom_components.sim7600.modem import ModemTimeoutError
from custom_components.sim7600.sms_codec import CusdReply
from custom_components.sim7600.types import GpsData, SmsData


@pytest.fixture
def mock_entry() -> MagicMock:
    """Config entry mock."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = {CONF_POLLING_INTERVAL: 60, CONF_GNSS_INTERVAL: 300}
    entry.options = {}
    return entry


@pytest.fixture
def gps_data() -> GpsData:
    """Sample GPS fix."""
    return GpsData(
        latitude=52.524,
        longitude=13.409,
        altitude=50.0,
        speed=1.5,
        date="250321",
        time="023504.0",
    )


@pytest.fixture
def mock_modem(gps_data: GpsData) -> AsyncMock:
    """Modem mock with typical answers."""
    modem = AsyncMock()
    modem.port = "/dev/ttyUSB2"
    modem.baudrate = 115200
    modem.has_pending_sms = False
    modem.get_signal_quality.return_value = 20
    modem.get_operator.return_value = "Test Operator"
    modem.get_network_info.return_value = {"mode": "LTE", "system_mode": "Online"}
    modem.get_imei.return_value = "123456789012345"
    modem.get_firmware.return_value = "V1.0"
    modem.get_manufacturer.return_value = "SimTech"
    modem.get_model.return_value = "SIM7600E-H"
    modem.get_registration_status.return_value = 1
    modem.get_gprs_registration_status.return_value = 1
    modem.get_sim_status.return_value = "READY"
    modem.get_unread_sms.return_value = []
    modem.set_gps.return_value = True
    modem.get_gps_info.return_value = gps_data
    modem.delete_sms.return_value = True
    modem.delete_read_sms.return_value = True
    return modem


def _coordinator(
    hass: HomeAssistant, modem: AsyncMock, entry: MagicMock
) -> SIM7600DataUpdateCoordinator:
    return SIM7600DataUpdateCoordinator(hass, modem, entry)


async def test_coordinator_update_success(
    hass: HomeAssistant, mock_modem: AsyncMock, mock_entry: MagicMock
) -> None:
    """A successful cycle returns all data."""
    data = await _coordinator(hass, mock_modem, mock_entry)._async_update_data()
    assert data["rssi"] == 20
    assert data["signal_dbm"] == -73
    assert data["operator"] == "Test Operator"
    assert data["network_mode"] == "LTE"
    assert data["imei"] == "123456789012345"
    assert data["model"] == "SIM7600E-H"
    assert data["sim_status"] == "READY"


async def test_config_entry_passed(
    hass: HomeAssistant, mock_modem: AsyncMock, mock_entry: MagicMock
) -> None:
    """The config entry is passed to DataUpdateCoordinator."""
    assert _coordinator(hass, mock_modem, mock_entry).config_entry is mock_entry


async def test_coordinator_update_failed(
    hass: HomeAssistant, mock_modem: AsyncMock, mock_entry: MagicMock
) -> None:
    """Exceptions become UpdateFailed."""
    mock_modem.get_signal_quality.side_effect = Exception("Connection error")
    with pytest.raises(UpdateFailed):
        await _coordinator(hass, mock_modem, mock_entry)._async_update_data()


async def test_modem_timeouts_raise_update_failed(
    hass: HomeAssistant, mock_modem: AsyncMock, mock_entry: MagicMock
) -> None:
    """ModemTimeoutError (modem silent) is UpdateFailed, not empty data."""
    mock_modem.get_operator.side_effect = ModemTimeoutError("silent")
    with pytest.raises(UpdateFailed):
        await _coordinator(hass, mock_modem, mock_entry)._async_update_data()


async def test_state_persistence_gps_on_none_response(
    hass: HomeAssistant, mock_modem: AsyncMock, mock_entry: MagicMock, gps_data: GpsData
) -> None:
    """A missing fix keeps the last fix while it is fresh."""
    coordinator = _coordinator(hass, mock_modem, mock_entry)
    await coordinator._async_update_data()
    mock_modem.get_gps_info.return_value = None
    coordinator.last_gnss_update = 0.0
    data = await coordinator._async_update_data()
    assert data["gps"] is not None
    assert data["gps"].latitude == gps_data.latitude
    assert data["gps_last_fix"] is not None


async def test_gps_expires_after_ten_minutes(
    hass: HomeAssistant, mock_modem: AsyncMock, mock_entry: MagicMock
) -> None:
    """Without a fix for 10 minutes the location becomes None."""
    coordinator = _coordinator(hass, mock_modem, mock_entry)
    await coordinator._async_update_data()
    mock_modem.get_gps_info.return_value = None
    coordinator._last_fix_time = time.time() - GPS_STALE_AFTER - 1
    coordinator.last_gnss_update = 0.0
    data = await coordinator._async_update_data()
    assert data["gps"] is None
    assert data["gps_last_fix"] is not None


async def test_gps_logged_at_debug(
    hass: HomeAssistant,
    mock_modem: AsyncMock,
    mock_entry: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Coordinates are never logged at INFO or above."""
    with caplog.at_level(logging.INFO, logger="custom_components.sim7600"):
        await _coordinator(hass, mock_modem, mock_entry)._async_update_data()
    assert "52.5" not in caplog.text


async def test_state_persistence_signal_on_none_response(
    hass: HomeAssistant, mock_modem: AsyncMock, mock_entry: MagicMock
) -> None:
    """A missing signal value keeps the last dBm value."""
    coordinator = _coordinator(hass, mock_modem, mock_entry)
    await coordinator._async_update_data()
    mock_modem.get_signal_quality.return_value = None
    data = await coordinator._async_update_data()
    assert data["signal_dbm"] == -73


async def test_no_update_failed_on_partial_error(
    hass: HomeAssistant, mock_modem: AsyncMock, mock_entry: MagicMock
) -> None:
    """A single command returning None is not an error."""
    mock_modem.get_operator.return_value = None
    data = await _coordinator(hass, mock_modem, mock_entry)._async_update_data()
    assert data is not None


# --- SMS ---


async def test_event_fired_for_each_sms(
    hass: HomeAssistant, mock_modem: AsyncMock, mock_entry: MagicMock
) -> None:
    """Every received SMS fires its own event; last one is kept in data."""
    events = async_capture_events(hass, EVENT_SMS_RECEIVED)
    mock_modem.get_unread_sms.return_value = [
        SmsData(index=1, sender="+111", timestamp="t1", message="Первое"),
        SmsData(
            index=4,
            sender="+222",
            timestamp="t2",
            message="Second long",
            indices=(4, 5),
            parts=2,
        ),
    ]
    data = await _coordinator(hass, mock_modem, mock_entry)._async_update_data()
    await hass.async_block_till_done()

    assert [e.data["text"] for e in events] == ["Первое", "Second long"]
    assert events[0].data["sender"] == "+111"
    assert events[0].data["timestamp"] == "t1"
    assert events[0].data["entry_id"] == "test_entry"
    assert events[1].data["parts"] == 2
    assert data["last_sms"].message == "Second long"


async def test_sms_deleted_after_reading(
    hass: HomeAssistant, mock_modem: AsyncMock, mock_entry: MagicMock
) -> None:
    """All storage slots of a message are deleted."""
    mock_modem.get_unread_sms.return_value = [
        SmsData(index=3, sender="+1", timestamp="t", message="x", indices=(3, 8))
    ]
    await _coordinator(hass, mock_modem, mock_entry)._async_update_data()
    assert [c.args for c in mock_modem.delete_sms.await_args_list] == [(3,), (8,)]


async def test_periodic_read_sms_cleanup(
    hass: HomeAssistant, mock_modem: AsyncMock, mock_entry: MagicMock
) -> None:
    """AT+CMGD=1,1 runs once per cleanup interval, not every cycle."""
    coordinator = _coordinator(hass, mock_modem, mock_entry)
    await coordinator._async_update_data()
    mock_modem.delete_read_sms.assert_not_called()
    coordinator._last_sms_cleanup = time.time() - SMS_CLEANUP_INTERVAL - 1
    await coordinator._async_update_data()
    mock_modem.delete_read_sms.assert_awaited_once()


async def test_cleanup_skipped_while_multipart_pending(
    hass: HomeAssistant, mock_modem: AsyncMock, mock_entry: MagicMock
) -> None:
    """Read parts of an incomplete multipart SMS are not deleted."""
    mock_modem.has_pending_sms = True
    coordinator = _coordinator(hass, mock_modem, mock_entry)
    coordinator._last_sms_cleanup = 0
    await coordinator._async_update_data()
    mock_modem.delete_read_sms.assert_not_called()


# --- URCs ---


async def test_cmti_triggers_refresh(
    hass: HomeAssistant, mock_modem: AsyncMock, mock_entry: MagicMock
) -> None:
    """+CMTI requests an immediate refresh."""
    coordinator = _coordinator(hass, mock_modem, mock_entry)
    assert mock_modem.urc_callback == coordinator.handle_urc
    with patch.object(coordinator, "async_request_refresh", AsyncMock()) as refresh:
        coordinator.handle_urc('+CMTI: "SM",3')
        await hass.async_block_till_done()
    refresh.assert_awaited_once()


async def test_incoming_call_event_once_per_call(
    hass: HomeAssistant, mock_modem: AsyncMock, mock_entry: MagicMock
) -> None:
    """RING + repeated +CLIP fire one event with the number."""
    events = async_capture_events(hass, EVENT_INCOMING_CALL)
    coordinator = _coordinator(hass, mock_modem, mock_entry)
    for _ in range(3):
        coordinator.handle_urc("RING")
        coordinator.handle_urc('+CLIP: "+79991234567",145,,,,0')
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data == {"number": "+79991234567", "entry_id": "test_entry"}

    coordinator.handle_urc("NO CARRIER")
    coordinator.handle_urc('+CLIP: "+79990000000",145,,,,0')
    await hass.async_block_till_done()
    assert len(events) == 2


async def test_ring_without_clip_fires_event(
    hass: HomeAssistant, mock_modem: AsyncMock, mock_entry: MagicMock
) -> None:
    """A RING without caller ID fires an event with number None."""
    events = async_capture_events(hass, EVENT_INCOMING_CALL)
    with patch.object(coordinator_module, "RING_CLIP_WAIT", 0.01):
        coordinator = _coordinator(hass, mock_modem, mock_entry)
        coordinator.handle_urc("RING")
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data["number"] is None


# --- Repairs ---


async def test_sim_pin_issue(
    hass: HomeAssistant, mock_modem: AsyncMock, mock_entry: MagicMock
) -> None:
    """SIM PIN creates a repair issue, READY removes it."""
    registry = ir.async_get(hass)
    coordinator = _coordinator(hass, mock_modem, mock_entry)
    mock_modem.get_sim_status.return_value = "SIM PIN"
    await coordinator._async_update_data()
    assert registry.async_get_issue(DOMAIN, "sim_pin_required_test_entry")
    mock_modem.get_sim_status.return_value = "READY"
    await coordinator._async_update_data()
    assert registry.async_get_issue(DOMAIN, "sim_pin_required_test_entry") is None


async def test_no_registration_issue_after_timeout(
    hass: HomeAssistant, mock_modem: AsyncMock, mock_entry: MagicMock
) -> None:
    """Long missing registration creates an issue; registration clears it."""
    registry = ir.async_get(hass)
    issue_id = "no_network_registration_test_entry"
    mock_modem.get_registration_status.return_value = 2
    mock_modem.get_gprs_registration_status.return_value = 0
    coordinator = _coordinator(hass, mock_modem, mock_entry)
    await coordinator._async_update_data()
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    coordinator._unregistered_since = time.time() - NO_REGISTRATION_ISSUE_AFTER - 1
    await coordinator._async_update_data()
    assert registry.async_get_issue(DOMAIN, issue_id)
    mock_modem.get_registration_status.return_value = 1
    await coordinator._async_update_data()
    assert registry.async_get_issue(DOMAIN, issue_id) is None


# --- Balance ---


async def test_balance_disabled_by_default(
    hass: HomeAssistant, mock_modem: AsyncMock, mock_entry: MagicMock
) -> None:
    """No USSD is sent without the balance option."""
    data = await _coordinator(hass, mock_modem, mock_entry)._async_update_data()
    mock_modem.send_ussd.assert_not_called()
    assert data["balance"] is None


async def test_balance_updated_by_interval(
    hass: HomeAssistant, mock_modem: AsyncMock, mock_entry: MagicMock
) -> None:
    """The balance USSD runs once per interval."""
    mock_entry.options = {CONF_BALANCE_USSD: "*100#"}
    mock_modem.send_ussd.return_value = CusdReply(0, "Баланс: 10 р.", 72)
    coordinator = _coordinator(hass, mock_modem, mock_entry)
    data = await coordinator._async_update_data()
    await coordinator._async_update_data()
    mock_modem.send_ussd.assert_awaited_once_with("*100#")
    assert data["balance"] == "Баланс: 10 р."


# --- GPS timing / retry / failures ---


async def test_gps_not_fetched_before_interval(
    hass: HomeAssistant, mock_modem: AsyncMock, mock_entry: MagicMock
) -> None:
    """GPS is not polled before the interval elapsed."""
    coordinator = _coordinator(hass, mock_modem, mock_entry)
    coordinator.gps_enabled = True
    coordinator.last_gnss_update = time.time()
    await coordinator._async_update_data()
    mock_modem.get_gps_info.assert_not_called()


async def test_gps_fetched_after_interval(
    hass: HomeAssistant, mock_modem: AsyncMock, mock_entry: MagicMock
) -> None:
    """GPS is polled after the interval elapsed."""
    coordinator = _coordinator(hass, mock_modem, mock_entry)
    coordinator.gps_enabled = True
    coordinator.last_gnss_update = 0.0
    await coordinator._async_update_data()
    mock_modem.get_gps_info.assert_called_once()


async def test_gps_activation_retried_when_false(
    hass: HomeAssistant, mock_modem: AsyncMock, mock_entry: MagicMock
) -> None:
    """set_gps() returning False is retried in the next cycle."""
    mock_modem.set_gps.return_value = False
    coordinator = _coordinator(hass, mock_modem, mock_entry)
    await coordinator._async_update_data()
    assert not coordinator.gps_enabled
    await coordinator._async_update_data()
    assert mock_modem.set_gps.call_count == 2


async def test_consecutive_failures_log_warning_and_reset(
    hass: HomeAssistant,
    mock_modem: AsyncMock,
    mock_entry: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """More than 3 failures log a warning; success resets the counter."""
    mock_modem.get_signal_quality.side_effect = Exception("Connection error")
    coordinator = _coordinator(hass, mock_modem, mock_entry)
    with caplog.at_level(logging.WARNING, logger="custom_components.sim7600"):
        for _ in range(4):
            with pytest.raises(UpdateFailed):
                await coordinator._async_update_data()
    assert coordinator._consecutive_failures == 4
    assert "unreachable" in caplog.text
    mock_modem.get_signal_quality.side_effect = None
    await coordinator._async_update_data()
    assert coordinator._consecutive_failures == 0


async def test_static_data_cached_after_first_call(
    hass: HomeAssistant, mock_modem: AsyncMock, mock_entry: MagicMock
) -> None:
    """IMEI and firmware are read once."""
    coordinator = _coordinator(hass, mock_modem, mock_entry)
    await coordinator._async_update_data()
    await coordinator._async_update_data()
    mock_modem.get_imei.assert_called_once()
    mock_modem.get_firmware.assert_called_once()
