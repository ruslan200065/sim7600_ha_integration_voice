"""Shared device metadata for companion entities."""
from __future__ import annotations

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


class JarvisEntity(CoordinatorEntity):
    """Base entity: name comes from translations via ``translation_key``.

    The unique_id format (``jarvis_q1_<key>``) is unchanged so existing entity
    registry entries and their entity_ids are preserved.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator, key, name=None):
        super().__init__(coordinator)
        self._attr_unique_id = f"jarvis_q1_{key}"
        self._attr_translation_key = key
        self._attr_device_info = {
            "identifiers": {(DOMAIN, "yandex_signal_q1")},
            "manufacturer": "Yandex / SIMCom",
            "name": "JARVIS Signal Q1",
            "model": "Signal Q1 Voice Gateway",
        }
