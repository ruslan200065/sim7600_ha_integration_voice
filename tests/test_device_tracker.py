"""Tests für den SIM7600 Device Tracker."""

from unittest.mock import MagicMock

import pytest

from custom_components.sim7600.device_tracker import SIM7600DeviceTracker
from custom_components.sim7600.types import GpsData


@pytest.fixture
def gps_data() -> GpsData:
    """Beispiel-GPS-Daten."""
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
    """Coordinator-Mock mit GPS-Daten."""
    coordinator = MagicMock()
    coordinator.manufacturer = "SIMCOM INCORPORATED"
    coordinator.model = "SIMCOM_SIM7600E-H"
    coordinator.firmware = "V1"
    coordinator.data = {"gps": gps_data, "gps_last_fix": None}
    return coordinator


@pytest.fixture
def mock_entry() -> MagicMock:
    """Config-Entry-Mock."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    return entry


def test_tracker_latitude_from_gps(
    mock_coordinator: MagicMock, mock_entry: MagicMock, gps_data: GpsData
) -> None:
    """Latitude wird korrekt aus GpsData gelesen."""
    tracker = SIM7600DeviceTracker(mock_coordinator, mock_entry)
    assert tracker.latitude == gps_data.latitude


def test_tracker_longitude_from_gps(
    mock_coordinator: MagicMock, mock_entry: MagicMock, gps_data: GpsData
) -> None:
    """Longitude wird korrekt aus GpsData gelesen."""
    tracker = SIM7600DeviceTracker(mock_coordinator, mock_entry)
    assert tracker.longitude == gps_data.longitude


def test_tracker_altitude_attribute(
    mock_coordinator: MagicMock, mock_entry: MagicMock, gps_data: GpsData
) -> None:
    """Altitude ist als Zusatzattribut vorhanden."""
    tracker = SIM7600DeviceTracker(mock_coordinator, mock_entry)
    attrs = tracker.extra_state_attributes
    assert attrs["altitude"] == gps_data.altitude


def test_tracker_speed_attribute(
    mock_coordinator: MagicMock, mock_entry: MagicMock, gps_data: GpsData
) -> None:
    """Speed ist als Zusatzattribut vorhanden."""
    tracker = SIM7600DeviceTracker(mock_coordinator, mock_entry)
    attrs = tracker.extra_state_attributes
    assert attrs["speed"] == gps_data.speed


def test_tracker_date_attribute(
    mock_coordinator: MagicMock, mock_entry: MagicMock, gps_data: GpsData
) -> None:
    """Date ist als Zusatzattribut vorhanden."""
    tracker = SIM7600DeviceTracker(mock_coordinator, mock_entry)
    attrs = tracker.extra_state_attributes
    assert attrs["date"] == gps_data.date


def test_tracker_time_attribute(
    mock_coordinator: MagicMock, mock_entry: MagicMock, gps_data: GpsData
) -> None:
    """Time ist als Zusatzattribut vorhanden."""
    tracker = SIM7600DeviceTracker(mock_coordinator, mock_entry)
    attrs = tracker.extra_state_attributes
    assert attrs["time"] == gps_data.time


def test_tracker_unique_id_unchanged(
    mock_coordinator: MagicMock, mock_entry: MagicMock
) -> None:
    """The tracker keeps its pre-0.5.0 unique_id."""
    tracker = SIM7600DeviceTracker(mock_coordinator, mock_entry)
    assert tracker.unique_id == "test_entry_GPS"
    assert tracker.has_entity_name is True


def test_tracker_persists_last_position_when_gps_none(
    mock_coordinator: MagicMock, mock_entry: MagicMock, gps_data: GpsData
) -> None:
    """GPS=None → latitude/longitude sind None."""
    mock_coordinator.data = {"gps": None}
    tracker = SIM7600DeviceTracker(mock_coordinator, mock_entry)
    assert tracker.latitude is None
    assert tracker.longitude is None
    assert tracker.extra_state_attributes == {}


def test_tracker_no_crash_when_gps_none(
    mock_coordinator: MagicMock, mock_entry: MagicMock
) -> None:
    """Kein AttributeError wenn GPS-Daten fehlen."""
    mock_coordinator.data = {"gps": None}
    tracker = SIM7600DeviceTracker(mock_coordinator, mock_entry)
    assert tracker.latitude is None
    assert tracker.longitude is None
    assert tracker.altitude is None
    assert tracker.extra_state_attributes == {}
