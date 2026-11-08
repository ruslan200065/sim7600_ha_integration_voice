"""Tests for setup, unload, services, notify and diagnostics."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceRegistry
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sim7600.const import (
    CONF_BAUD_RATE,
    CONF_DEFAULT_RECIPIENT,
    CONF_SERIAL_PORT,
    DOMAIN,
    SERVICE_SEND_SMS,
    SERVICE_SEND_USSD,
)
from custom_components.sim7600.diagnostics import async_get_config_entry_diagnostics
from custom_components.sim7600.modem import (
    InvalidInputError,
    ModemCommandError,
    ModemConnectionError,
)
from custom_components.sim7600.sms_codec import CusdReply
from custom_components.sim7600.types import GpsData, SmsData

IMEI = "861234567890123"


def _make_modem(port: str = "/dev/ttyUSB2") -> AsyncMock:
    modem = AsyncMock()
    modem.port = port
    modem.connected = True
    modem.has_pending_sms = False
    modem.get_signal_quality.return_value = 20
    modem.get_operator.return_value = "Operator"
    modem.get_network_info.return_value = {"mode": "LTE", "system_mode": "Online"}
    modem.get_imei.return_value = IMEI
    modem.get_firmware.return_value = "LE20B04SIM7600M22"
    modem.get_manufacturer.return_value = "SIMCOM INCORPORATED"
    modem.get_model.return_value = "SIMCOM_SIM7600E-H"
    modem.get_registration_status.return_value = 1
    modem.get_gprs_registration_status.return_value = 1
    modem.get_sim_status.return_value = "READY"
    modem.get_unread_sms.return_value = [
        SmsData(index=1, sender="+79991234567", timestamp="t", message="Секрет")
    ]
    modem.set_gps.return_value = True
    modem.get_gps_info.return_value = GpsData(
        latitude=55.75,
        longitude=37.61,
        altitude=150.0,
        speed=2.0,
        date="071125",
        time="123045.0",
        fix_time=None,
    )
    return modem


@pytest.fixture
def modems():
    """Patch the modem class; each entry gets its own mock."""
    created: list[AsyncMock] = []

    def _factory(port: str, *_args, **_kwargs) -> AsyncMock:
        modem = _make_modem(port)
        created.append(modem)
        return modem

    with patch("custom_components.sim7600.SIM7600Modem", side_effect=_factory):
        yield created


def _entry(
    hass: HomeAssistant, port: str = "/dev/ttyUSB2", **options
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{IMEI}{port}",
        data={CONF_SERIAL_PORT: port, CONF_BAUD_RATE: 115200},
        options=options,
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


# --- Setup / unload ---


async def test_setup_and_unload_closes_modem(hass: HomeAssistant, modems) -> None:
    """Unloading closes the port for good; services stay registered."""
    entry = _entry(hass)
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.services.has_service(DOMAIN, SERVICE_SEND_SMS)
    assert hass.services.has_service(DOMAIN, SERVICE_SEND_USSD)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    modems[0].close.assert_awaited_once()
    assert hass.services.has_service(DOMAIN, SERVICE_SEND_SMS)

    # A service call must not use (reopen) the unloaded modem.
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_SMS,
            {"number": "+79991234567", "message": "x"},
            blocking=True,
        )
    modems[0].send_sms.assert_not_called()


async def test_service_registered_once(hass: HomeAssistant, modems) -> None:
    """Two entries register the service once (async_setup), not per entry."""
    original = ServiceRegistry.async_register
    with patch.object(
        ServiceRegistry, "async_register", autospec=True, side_effect=original
    ) as register:
        first = _entry(hass, "/dev/ttyUSB2")
        second = _entry(hass, "/dev/ttyUSB5")
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()
        assert first.state is ConfigEntryState.LOADED
        assert second.state is ConfigEntryState.LOADED
        await hass.config_entries.async_reload(first.entry_id)
        await hass.async_block_till_done()
    sms_registrations = [
        c for c in register.call_args_list if c.args[1:3] == (DOMAIN, SERVICE_SEND_SMS)
    ]
    assert len(sms_registrations) == 1


async def test_first_refresh_failure_disconnects(hass: HomeAssistant) -> None:
    """A failing first refresh closes the port and the entry retries."""
    modem = _make_modem()
    modem.get_signal_quality.side_effect = ModemConnectionError("gone")
    entry = _entry(hass)
    with patch("custom_components.sim7600.SIM7600Modem", return_value=modem):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    modem.close.assert_awaited_once()


async def test_legacy_unique_ids_migrated(hass: HomeAssistant, modems) -> None:
    """Pre-0.5.0 class-name unique_ids keep their entity_id."""
    entry = _entry(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_SIM7600SignalSensor",
        config_entry=entry,
        suggested_object_id="sim7600_signal_strength",
    )
    time_sensor = registry.async_get_or_create(
        "sensor", DOMAIN, f"{entry.entry_id}_SIM7600TimeSensor", config_entry=entry
    )
    await _setup(hass, entry)
    migrated = registry.async_get(old.entity_id)
    assert migrated is not None
    assert migrated.unique_id == f"{entry.entry_id}_signal_strength"
    assert registry.async_get(time_sensor.entity_id) is None


# --- send_sms service ---


async def test_send_sms_single_entry(hass: HomeAssistant, modems) -> None:
    """With one modem no target is needed."""
    await _setup(hass, _entry(hass))
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_SMS,
        {"number": "+79991234567", "message": "Привет"},
        blocking=True,
    )
    modems[0].send_sms.assert_awaited_once_with("+79991234567", "Привет")


@pytest.mark.parametrize("number", ['+7999"\r\nAT+CFUN=0', "12", "+7 999 123"])
async def test_send_sms_schema_rejects_bad_number(
    hass: HomeAssistant, modems, number: str
) -> None:
    """The service schema rejects numbers that could inject AT commands."""
    await _setup(hass, _entry(hass))
    with pytest.raises(Exception, match="number"):
        await hass.services.async_call(
            DOMAIN, SERVICE_SEND_SMS, {"number": number, "message": "x"}, blocking=True
        )
    modems[0].send_sms.assert_not_called()


async def test_send_sms_pick_by_entry_and_device(hass: HomeAssistant, modems) -> None:
    """entry_id or device_id select the modem; ambiguity is an error."""
    first = _entry(hass, "/dev/ttyUSB2")
    second = _entry(hass, "/dev/ttyUSB5")
    await _setup(hass, first)  # sets up every entry of the domain
    assert second.state is ConfigEntryState.LOADED
    modems.sort(key=lambda modem: modem.port)
    data = {"number": "+79991234567", "message": "x"}

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, SERVICE_SEND_SMS, data, blocking=True)

    await hass.services.async_call(
        DOMAIN, SERVICE_SEND_SMS, {**data, "entry_id": second.entry_id}, blocking=True
    )
    assert modems[1].send_sms.await_count == 1
    assert modems[0].send_sms.await_count == 0

    device = dr.async_get(hass).async_get_device({(DOMAIN, first.entry_id)})
    assert device is not None
    await hass.services.async_call(
        DOMAIN, SERVICE_SEND_SMS, {**data, "device_id": device.id}, blocking=True
    )
    assert modems[0].send_sms.await_count == 1

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, SERVICE_SEND_SMS, {**data, "entry_id": "missing"}, blocking=True
        )


async def test_send_sms_failure_raises(hass: HomeAssistant, modems) -> None:
    """Modem errors become HomeAssistantError, input errors validation errors."""
    await _setup(hass, _entry(hass))
    data = {"number": "+79991234567", "message": "x"}
    modems[0].send_sms.side_effect = ModemCommandError("+CMS ERROR: 500")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, SERVICE_SEND_SMS, data, blocking=True)
    modems[0].send_sms.side_effect = InvalidInputError("too long")
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, SERVICE_SEND_SMS, data, blocking=True)


# --- send_ussd service ---


async def test_send_ussd_returns_response(hass: HomeAssistant, modems) -> None:
    """send_ussd returns the decoded network reply."""
    await _setup(hass, _entry(hass))
    modems[0].send_ussd.return_value = CusdReply(0, "Баланс: 100 р.", 72)
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_USSD,
        {"code": "*100#"},
        blocking=True,
        return_response=True,
    )
    assert response == {"status": 0, "message": "Баланс: 100 р.", "dcs": 72}


async def test_send_ussd_rejects_bad_code(hass: HomeAssistant, modems) -> None:
    """Codes with other characters fail validation."""
    await _setup(hass, _entry(hass))
    with pytest.raises(Exception, match="code"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_USSD,
            {"code": '*100#"'},
            blocking=True,
            return_response=True,
        )


# --- Notify ---


async def test_notify_sends_to_default_recipient(hass: HomeAssistant, modems) -> None:
    """notify.send_message sends an SMS to the configured recipient."""
    entry = _entry(hass, **{CONF_DEFAULT_RECIPIENT: "+79991234567"})
    await _setup(hass, entry)
    entity_id = er.async_get(hass).async_get_entity_id(
        "notify", DOMAIN, f"{entry.entry_id}_sms_notify"
    )
    assert entity_id is not None
    await hass.services.async_call(
        "notify",
        "send_message",
        {"entity_id": entity_id, "message": "Дверь открыта", "title": "Дом"},
        blocking=True,
    )
    modems[0].send_sms.assert_awaited_once_with("+79991234567", "Дом\nДверь открыта")


async def test_notify_unavailable_without_recipient(
    hass: HomeAssistant, modems
) -> None:
    """Without a default recipient the notify entity is unavailable."""
    entry = _entry(hass)
    await _setup(hass, entry)
    entity_id = er.async_get(hass).async_get_entity_id(
        "notify", DOMAIN, f"{entry.entry_id}_sms_notify"
    )
    assert entity_id is not None
    assert hass.states.get(entity_id).state == "unavailable"


# --- Diagnostics ---


async def test_diagnostics_redacts_private_data(hass: HomeAssistant, modems) -> None:
    """IMEI, phone numbers, SMS text and coordinates are redacted."""
    entry = _entry(hass, **{CONF_DEFAULT_RECIPIENT: "+79991234567"})
    await _setup(hass, entry)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    dumped = repr(diagnostics)
    for secret in (IMEI, "+79991234567", "Секрет", "55.75", "37.61"):
        assert secret not in dumped
    assert diagnostics["data"]["operator"] == "Operator"
    assert diagnostics["data"]["gps"]["latitude"] == "**REDACTED**"


# --- Device tracker ---


async def test_device_tracker_and_sensors_created(hass: HomeAssistant, modems) -> None:
    """Platforms create the tracker with the old unique_id and new sensors."""
    entry = _entry(hass)
    await _setup(hass, entry)
    registry = er.async_get(hass)
    assert registry.async_get_entity_id(
        "device_tracker", DOMAIN, f"{entry.entry_id}_GPS"
    )
    assert registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_gnss_timestamp"
    )
    assert not registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_balance"
    )
    device = dr.async_get(hass).async_get_device({(DOMAIN, entry.entry_id)})
    assert device is not None
    assert device.model == "SIMCOM_SIM7600E-H"
    assert device.sw_version == "LE20B04SIM7600M22"
