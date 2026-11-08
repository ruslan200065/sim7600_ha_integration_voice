"""Tests for SIM7600 sensor entities."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import UnitOfSpeed

from custom_components.sim7600.sensor import (
    SENSORS_BY_KEY,
    SIM7600Sensor,
    parse_gnss_timestamp,
    truncate_state,
)
from custom_components.sim7600.types import GpsData, SmsData


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
def mock_coordinator(gps_data: GpsData) -> MagicMock:
    """Coordinator mock with full data."""
    coordinator = MagicMock()
    coordinator.manufacturer = "SIMCOM INCORPORATED"
    coordinator.model = "SIMCOM_SIM7600E-H"
    coordinator.firmware = "LE20B04SIM7600M22"
    coordinator.balance_ussd = None
    coordinator.data = {
        "signal_dbm": -73,
        "operator": "Test Operator",
        "network_mode": "LTE",
        "system_mode": "Online",
        "sim_status": "READY",
        "reg_status": 1,
        "gprs_reg_status": 5,
        "gps": gps_data,
    }
    return coordinator


@pytest.fixture
def mock_entry() -> MagicMock:
    """Config entry mock."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    return entry


def _sensor(coordinator: MagicMock, entry: MagicMock, key: str) -> SIM7600Sensor:
    return SIM7600Sensor(coordinator, entry, SENSORS_BY_KEY[key])


def test_sensors_native_value(
    mock_coordinator: MagicMock, mock_entry: MagicMock
) -> None:
    """Network sensors return the coordinator values."""
    assert _sensor(mock_coordinator, mock_entry, "signal_strength").native_value == -73
    assert (
        _sensor(mock_coordinator, mock_entry, "operator").native_value
        == "Test Operator"
    )
    assert _sensor(mock_coordinator, mock_entry, "network_mode").native_value == "LTE"
    assert _sensor(mock_coordinator, mock_entry, "system_mode").native_value == "Online"


def test_sensors_unique_id_and_naming(
    mock_coordinator: MagicMock, mock_entry: MagicMock
) -> None:
    """Unique IDs use stable keys; names come from translations."""
    sensor = _sensor(mock_coordinator, mock_entry, "signal_strength")
    assert sensor.unique_id == "test_entry_signal_strength"
    assert sensor.has_entity_name is True
    assert sensor.translation_key == "signal_strength"


def test_device_info_uses_modem_values(
    mock_coordinator: MagicMock, mock_entry: MagicMock
) -> None:
    """Device info contains the real model and firmware."""
    info = _sensor(mock_coordinator, mock_entry, "imei").device_info
    assert info is not None
    assert info["model"] == "SIMCOM_SIM7600E-H"
    assert info["sw_version"] == "LE20B04SIM7600M22"
    assert info["manufacturer"] == "SIMCOM INCORPORATED"


def test_sim_status_sensor(mock_coordinator: MagicMock, mock_entry: MagicMock) -> None:
    """SIM status sensor shows READY or None."""
    sensor = _sensor(mock_coordinator, mock_entry, "sim_status")
    assert sensor.native_value == "READY"
    mock_coordinator.data = {"sim_status": None}
    assert sensor.native_value is None


def test_speed_sensor_is_knots(
    mock_coordinator: MagicMock, mock_entry: MagicMock, gps_data: GpsData
) -> None:
    """+CGPSINFO speed is in knots; HA handles unit conversion."""
    sensor = _sensor(mock_coordinator, mock_entry, "speed")
    assert sensor.native_value == gps_data.speed
    assert sensor.native_unit_of_measurement == UnitOfSpeed.KNOTS
    assert sensor.device_class == SensorDeviceClass.SPEED


def test_altitude_sensor_from_gps(
    mock_coordinator: MagicMock, mock_entry: MagicMock, gps_data: GpsData
) -> None:
    """Altitude sensor reads the GPS fix."""
    assert _sensor(mock_coordinator, mock_entry, "altitude").native_value == 50.0


def test_gnss_timestamp_combines_date_and_time(
    mock_coordinator: MagicMock, mock_entry: MagicMock
) -> None:
    """GNSS date and time are one UTC timestamp sensor."""
    sensor = _sensor(mock_coordinator, mock_entry, "gnss_timestamp")
    assert sensor.device_class == SensorDeviceClass.TIMESTAMP
    assert sensor.native_value == datetime(2021, 3, 25, 2, 35, 4, tzinfo=UTC)


def test_parse_gnss_timestamp_fraction_and_garbage() -> None:
    """Fractional seconds are kept; garbage returns None."""
    assert parse_gnss_timestamp("071125", "123045.5") == datetime(
        2025, 11, 7, 12, 30, 45, 500000, tzinfo=UTC
    )
    assert parse_gnss_timestamp("", "") is None
    assert parse_gnss_timestamp("991399", "999999") is None


def test_gps_sensors_none_without_fix(
    mock_coordinator: MagicMock, mock_entry: MagicMock
) -> None:
    """GPS sensors return None when there is no (fresh) fix."""
    mock_coordinator.data = {"gps": None}
    for key in ("speed", "altitude", "gnss_timestamp"):
        assert _sensor(mock_coordinator, mock_entry, key).native_value is None


def test_reg_status_enum(mock_coordinator: MagicMock, mock_entry: MagicMock) -> None:
    """Registration status is an enum sensor with readable options."""
    reg = _sensor(mock_coordinator, mock_entry, "reg_status")
    gprs = _sensor(mock_coordinator, mock_entry, "gprs_reg_status")
    assert reg.device_class == SensorDeviceClass.ENUM
    assert reg.native_value == "home"
    assert gprs.native_value == "roaming"
    assert reg.native_value in reg.options
    mock_coordinator.data = {"reg_status": 42}
    assert reg.native_value == "unknown"


def test_truncate_state() -> None:
    """Strings longer than 255 characters are cut to 255."""
    assert truncate_state("a" * 255) == "a" * 255
    cut = truncate_state("a" * 300)
    assert cut is not None
    assert len(cut) == 255
    assert truncate_state(None) is None


def test_last_sms_truncated_with_full_text_attribute(
    mock_coordinator: MagicMock, mock_entry: MagicMock
) -> None:
    """A long SMS is truncated in the state, full text is an attribute."""
    text = "Привет " * 60
    mock_coordinator.data = {
        "last_sms": SmsData(
            index=1, sender="+111", timestamp="2025-11-07T12:30:45+03:00", message=text
        )
    }
    sensor = _sensor(mock_coordinator, mock_entry, "last_sms")
    assert len(sensor.native_value) == 255
    attrs = sensor.extra_state_attributes
    assert attrs is not None
    assert attrs["full_text"] == text
    assert attrs["sender"] == "+111"


def test_balance_sensor(mock_coordinator: MagicMock, mock_entry: MagicMock) -> None:
    """Balance sensor exists only with a USSD code and shows the reply."""
    description = SENSORS_BY_KEY["balance"]
    assert not description.exists_fn(mock_coordinator)
    mock_coordinator.balance_ussd = "*100#"
    assert description.exists_fn(mock_coordinator)
    mock_coordinator.data = {"balance": "Баланс: 100 р.", "balance_updated": None}
    assert (
        _sensor(mock_coordinator, mock_entry, "balance").native_value
        == "Баланс: 100 р."
    )
