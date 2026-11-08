"""Notify entity for the SIM7600 integration (SMS to a default recipient)."""

from __future__ import annotations

from homeassistant.components.notify import NotifyEntity, NotifyEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SIM7600DataUpdateCoordinator
from .entity import SIM7600Entity
from .modem import InvalidInputError, ModemError


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the SMS notify entity."""
    coordinator: SIM7600DataUpdateCoordinator = entry.runtime_data
    async_add_entities([SIM7600SmsNotifyEntity(coordinator, entry)])


class SIM7600SmsNotifyEntity(SIM7600Entity, NotifyEntity):
    """Send SMS to the default recipient set in the options."""

    _attr_translation_key = "sms"
    _attr_supported_features = NotifyEntityFeature.TITLE

    def __init__(
        self, coordinator: SIM7600DataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_sms_notify"

    @property
    def available(self) -> bool:
        """Available while the modem works and a recipient is configured."""
        return super().available and bool(self.coordinator.default_recipient)

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        """Send the message as SMS; the title becomes the first line."""
        recipient = self.coordinator.default_recipient
        if not recipient:
            raise ServiceValidationError(
                "No default SMS recipient configured",
                translation_domain=DOMAIN,
                translation_key="no_default_recipient",
            )
        text = f"{title}\n{message}" if title else message
        try:
            await self.coordinator.modem.send_sms(recipient, text)
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
