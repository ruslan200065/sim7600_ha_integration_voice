"""A snapshot camera entity. Uses the real Q1 UVC camera via App proxy."""
from homeassistant.components.camera import Camera
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .entity import JarvisEntity


async def async_setup_entry(hass, entry, async_add_entities):
    c = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([Q1Camera(c)])


class Q1Camera(JarvisEntity, Camera):
    def __init__(self, coordinator):
        JarvisEntity.__init__(self, coordinator, "camera")
        Camera.__init__(self)

    @property
    def is_on(self):
        return True

    @property
    def available(self):
        return super().available and bool(
            ((self.coordinator.data or {}).get("capabilities") or {}).get("camera_configured"))

    async def async_camera_image(self, width=None, height=None):
        try:
            return await self.coordinator.client.get("/q1/snapshot", timeout=17, raw=True)
        except HomeAssistantError:
            return None
