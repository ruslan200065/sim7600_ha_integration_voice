"""Snapshots and on-demand MJPEG from the add-on's shared camera capture."""

import asyncio

from aiohttp import ClientError, ClientTimeout, web
from aiohttp.helpers import parse_mimetype
from homeassistant.components.camera import Camera
from homeassistant.exceptions import HomeAssistantError

from .entity import JarvisEntity

_MJPEG_TIMEOUT = ClientTimeout(total=None, sock_connect=5, sock_read=15)
_CLIENT_WRITE_TIMEOUT = 3
_MJPEG_CHUNK_SIZE = 64 * 1024


async def async_setup_entry(hass, entry, async_add_entities):
    c = entry.runtime_data
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
            ((self.coordinator.data or {}).get("capabilities") or {}).get(
                "camera_configured"
            )
        )

    async def async_camera_image(self, width=None, height=None):
        try:
            return await self.coordinator.client.get(
                "/q1/snapshot", timeout=17, raw=True
            )
        except HomeAssistantError:
            return None

    async def handle_async_mjpeg_stream(self, request):
        client = self.coordinator.client
        response = None
        try:
            async with client.session.get(
                client.url + "/q1/camera/live",
                headers=client.headers,
                timeout=_MJPEG_TIMEOUT,
                allow_redirects=False,
            ) as upstream:
                content_type = upstream.headers.get("Content-Type", "")
                mime = parse_mimetype(content_type)
                if (
                    upstream.status != 200
                    or (mime.type, mime.subtype) != ("multipart", "x-mixed-replace")
                    or not mime.parameters.get("boundary")
                ):
                    raise web.HTTPServiceUnavailable(
                        text="JARVIS camera stream unavailable"
                    )
                response = web.StreamResponse(
                    headers={
                        "Content-Type": content_type,
                        "Cache-Control": "no-store",
                        "X-Accel-Buffering": "no",
                    }
                )
                await asyncio.wait_for(response.prepare(request), _CLIENT_WRITE_TIMEOUT)
                async for chunk in upstream.content.iter_chunked(_MJPEG_CHUNK_SIZE):
                    transport = request.transport
                    if transport is None or transport.is_closing():
                        break
                    await asyncio.wait_for(response.write(chunk), _CLIENT_WRITE_TIMEOUT)
        except (ClientError, TimeoutError, OSError) as exc:
            if response is None or not response.prepared:
                raise web.HTTPServiceUnavailable(
                    text="JARVIS camera stream unavailable"
                ) from exc
        return response
