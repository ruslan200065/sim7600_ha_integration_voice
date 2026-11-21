"""Snapshots and on-demand MJPEG from the add-on's shared camera capture."""
import asyncio

from aiohttp import ClientError, ClientTimeout, web

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


    async def handle_async_mjpeg_stream(self, request):
        client = self.coordinator.client
        response = None
        try:
            async with client.session.get(
                client.url + "/q1/camera/live",
                headers=client.headers,
                timeout=ClientTimeout(total=None, sock_connect=5, sock_read=15),
                allow_redirects=False,
            ) as upstream:
                if upstream.status != 200 or upstream.content_type != "multipart/x-mixed-replace":
                    raise web.HTTPServiceUnavailable(text="JARVIS camera stream unavailable")
                response = web.StreamResponse(headers={
                    "Content-Type": upstream.headers["Content-Type"],
                    "Cache-Control": "no-store",
                    "X-Accel-Buffering": "no",
                })
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    if request.transport is None or request.transport.is_closing():
                        break
                    await asyncio.wait_for(response.write(chunk), 3)
        except (ClientError, TimeoutError, OSError) as exc:
            if response is None or not response.prepared:
                raise web.HTTPServiceUnavailable(text="JARVIS camera stream unavailable") from exc
        return response
