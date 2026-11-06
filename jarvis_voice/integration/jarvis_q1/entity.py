"""Shared device metadata for companion entities."""
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN

class JarvisEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, key, name):
        super().__init__(coordinator)
        self._attr_unique_id = f"jarvis_q1_{key}"
        self._attr_name = name
        self._attr_device_info = {
            "identifiers": {(DOMAIN,"yandex_signal_q1")},
            "manufacturer": "Yandex / SIMCom",
            "name": "JARVIS Signal Q1",
            "model": "Signal Q1 Voice Gateway",
        }
