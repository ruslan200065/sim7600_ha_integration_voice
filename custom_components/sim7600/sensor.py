"""Support for SIM7600 sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfLength,
    UnitOfSpeed,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import MAX_STATE_LENGTH, REG_STATUS, REG_STATUS_OPTIONS
from .coordinator import SIM7600DataUpdateCoordinator
from .entity import SIM7600Entity
from .types import GpsData, SmsData


def truncate_state(value: str | None) -> str | None:
    """Limit a string state to the 255 characters HA allows."""
    if value is None or len(value) <= MAX_STATE_LENGTH:
        return value
    return value[: MAX_STATE_LENGTH - 1] + "…"


def parse_gnss_timestamp(date_str: str, time_str: str) -> datetime | None:
    """Combine +CGPSINFO date (ddmmyy) and time (hhmmss.s) into a UTC datetime."""
    try:
        day, month, year = int(date_str[0:2]), int(date_str[2:4]), int(date_str[4:6])
        hour, minute = int(time_str[0:2]), int(time_str[2:4])
        seconds = float(time_str[4:])
        whole = int(seconds)
        micro = int(round((seconds - whole) * 1_000_000))
        return datetime(
            2000 + year,
            month,
            day,
            hour,
            minute,
            whole,
            min(micro, 999_999),
            tzinfo=UTC,
        )
    except (ValueError, IndexError):
        return None


def _gps(data: dict[str, Any]) -> GpsData | None:
    return data.get("gps")


def _reg_status(value: int | None) -> str | None:
    if value is None:
        return None
    return REG_STATUS.get(value, "unknown")


def _sms_attributes(data: dict[str, Any]) -> dict[str, Any]:
    sms: SmsData | None = data.get("last_sms")
    if sms is None:
        return {}
    return {
        "sender": sms.sender,
        "timestamp": sms.timestamp,
        "full_text": sms.message,
        "parts": sms.parts,
        "complete": sms.complete,
    }


def _balance_attributes(data: dict[str, Any]) -> dict[str, Any]:
    balance = data.get("balance")
    if balance is None:
        return {}
    updated = data.get("balance_updated")
    return {
        "full_text": balance,
        "last_updated": updated.isoformat() if updated is not None else None,
    }


@dataclass(frozen=True, kw_only=True)
class SIM7600SensorEntityDescription(SensorEntityDescription):
    """Describe a SIM7600 sensor."""

    value_fn: Callable[[dict[str, Any]], Any]
    attributes_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    exists_fn: Callable[[SIM7600DataUpdateCoordinator], bool] = lambda _: True


SENSORS: tuple[SIM7600SensorEntityDescription, ...] = (
    SIM7600SensorEntityDescription(
        key="signal_strength",
        translation_key="signal_strength",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get("signal_dbm"),
    ),
    SIM7600SensorEntityDescription(
        key="operator",
        translation_key="operator",
        value_fn=lambda data: truncate_state(data.get("operator")),
    ),
    SIM7600SensorEntityDescription(
        key="network_mode",
        translation_key="network_mode",
        value_fn=lambda data: truncate_state(data.get("network_mode")),
    ),
    SIM7600SensorEntityDescription(
        key="system_mode",
        translation_key="system_mode",
        value_fn=lambda data: truncate_state(data.get("system_mode")),
    ),
    SIM7600SensorEntityDescription(
        key="reg_status",
        translation_key="reg_status",
        device_class=SensorDeviceClass.ENUM,
        options=REG_STATUS_OPTIONS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _reg_status(data.get("reg_status")),
    ),
    SIM7600SensorEntityDescription(
        key="gprs_reg_status",
        translation_key="gprs_reg_status",
        device_class=SensorDeviceClass.ENUM,
        options=REG_STATUS_OPTIONS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _reg_status(data.get("gprs_reg_status")),
    ),
    SIM7600SensorEntityDescription(
        key="imei",
        translation_key="imei",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("imei"),
    ),
    SIM7600SensorEntityDescription(
        key="firmware_version",
        translation_key="firmware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: truncate_state(data.get("firmware")),
    ),
    SIM7600SensorEntityDescription(
        key="sim_status",
        translation_key="sim_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: truncate_state(data.get("sim_status")),
    ),
    SIM7600SensorEntityDescription(
        key="last_sms",
        translation_key="last_sms",
        value_fn=lambda data: truncate_state(
            sms.message if (sms := data.get("last_sms")) is not None else None
        ),
        attributes_fn=_sms_attributes,
    ),
    SIM7600SensorEntityDescription(
        key="speed",
        translation_key="speed",
        device_class=SensorDeviceClass.SPEED,
        # +CGPSINFO reports speed over ground in knots; HA converts the unit.
        native_unit_of_measurement=UnitOfSpeed.KNOTS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: gps.speed if (gps := _gps(data)) is not None else None,
    ),
    SIM7600SensorEntityDescription(
        key="altitude",
        translation_key="altitude",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.METERS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: gps.altitude if (gps := _gps(data)) is not None else None,
    ),
    SIM7600SensorEntityDescription(
        key="gnss_timestamp",
        translation_key="gnss_timestamp",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: (
            parse_gnss_timestamp(gps.date, gps.time)
            if (gps := _gps(data)) is not None
            else None
        ),
    ),
    SIM7600SensorEntityDescription(
        key="gps_last_fix",
        translation_key="gps_last_fix",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("gps_last_fix"),
    ),
    SIM7600SensorEntityDescription(
        key="balance",
        translation_key="balance",
        value_fn=lambda data: truncate_state(data.get("balance")),
        attributes_fn=_balance_attributes,
        exists_fn=lambda coordinator: bool(coordinator.balance_ussd),
    ),
)
SENSORS_BY_KEY = {description.key: description for description in SENSORS}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SIM7600 sensors based on a config entry."""
    coordinator: SIM7600DataUpdateCoordinator = entry.runtime_data
    async_add_entities(
        SIM7600Sensor(coordinator, entry, description)
        for description in SENSORS
        if description.exists_fn(coordinator)
    )


class SIM7600Sensor(SIM7600Entity, SensorEntity):
    """A SIM7600 sensor."""

    entity_description: SIM7600SensorEntityDescription

    def __init__(
        self,
        coordinator: SIM7600DataUpdateCoordinator,
        entry: ConfigEntry,
        description: SIM7600SensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        if self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(self.coordinator.data)
