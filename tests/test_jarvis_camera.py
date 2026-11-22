"""Verify the companion forwards live bytes and keeps the API token upstream."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request
from aiohttp.web import HTTPServiceUnavailable
from homeassistant.components.camera import CameraEntityFeature
from homeassistant.components.camera.const import DATA_COMPONENT
from homeassistant.components.camera.media_source import CameraMediaSource
from homeassistant.core import HomeAssistant

from jarvis_voice.integration.jarvis_q1.camera import Q1Camera

CONTENT_TYPE = "multipart/x-mixed-replace; boundary=jarvis"
FRAME = (
    b"--jarvis\r\nContent-Type: image/jpeg\r\nContent-Length: 4\r\n\r\n"
    b"\xff\xd8\xff\xd9\r\n"
)


async def test_ha_camera_proxies_authenticated_mjpeg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def iter_chunks(_size: int):
        yield FRAME
        yield FRAME

    upstream = MagicMock()
    upstream.status = 200
    upstream.content_type = "multipart/x-mixed-replace"
    upstream.headers = {"Content-Type": CONTENT_TYPE}
    upstream.content.iter_chunked = iter_chunks
    get = MagicMock()
    get.return_value.__aenter__.return_value = upstream
    client = SimpleNamespace(
        session=SimpleNamespace(get=get),
        url="http://addon:8099",
        headers={"Authorization": "Bearer test-token"},
    )
    camera = SimpleNamespace(coordinator=SimpleNamespace(client=client))
    response = SimpleNamespace(prepare=AsyncMock(), write=AsyncMock())
    make_response = MagicMock(return_value=response)
    monkeypatch.setattr(
        "jarvis_voice.integration.jarvis_q1.camera.web.StreamResponse", make_response
    )
    request = make_mocked_request("GET", "/api/camera_proxy_stream")
    request.transport.is_closing.return_value = False

    assert await Q1Camera.handle_async_mjpeg_stream(camera, request) is response
    assert get.call_args.args == ("http://addon:8099/q1/camera/live",)
    assert get.call_args.kwargs["headers"] == client.headers
    assert make_response.call_args.kwargs["headers"] == {
        "Content-Type": CONTENT_TYPE,
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
    }
    assert [call.args[0] for call in response.write.await_args_list] == [FRAME, FRAME]


async def test_ha_camera_rejects_multipart_stream_without_boundary() -> None:
    upstream = SimpleNamespace(
        status=200, headers={"Content-Type": "multipart/x-mixed-replace"}
    )
    get = MagicMock()
    get.return_value.__aenter__.return_value = upstream
    camera = SimpleNamespace(
        coordinator=SimpleNamespace(
            client=SimpleNamespace(
                session=SimpleNamespace(get=get), url="http://addon:8099", headers={}
            )
        )
    )
    with pytest.raises(HTTPServiceUnavailable):
        await Q1Camera.handle_async_mjpeg_stream(
            camera, make_mocked_request("GET", "/api/camera_proxy_stream")
        )


async def test_ha_media_resolves_to_direct_camera_stream(hass: HomeAssistant) -> None:
    entity_id = "camera.jarvis_signal_q1_camera"
    camera = Q1Camera(SimpleNamespace())
    camera.entity_id = entity_id
    assert CameraEntityFeature.STREAM not in camera.supported_features
    assert not camera.camera_capabilities.frontend_stream_types
    hass.data[DATA_COMPONENT] = SimpleNamespace(get_entity=lambda _: camera)
    result = await CameraMediaSource(hass).async_resolve_media(
        SimpleNamespace(identifier=entity_id)
    )
    assert result.url == f"/api/camera_proxy_stream/{entity_id}"
    assert result.mime_type == "image/jpeg"
