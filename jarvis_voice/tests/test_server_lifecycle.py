"""Regression tests for add-on cleanup and HTTP input validation."""

import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
with patch.dict(sys.modules, {"speech": SimpleNamespace(SpeechKit=object)}):
    from main import Server


async def test_shutdown_releases_modem_and_http_after_media_cleanup_fails(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPTIONS_PATH", str(tmp_path / "options.json"))
    server = Server({"api_token": "test"})
    server.audio.stop = AsyncMock()
    server.media.stop = AsyncMock(side_effect=RuntimeError("ALSA shutdown failed"))
    server.modem.stop = AsyncMock()
    server.http = SimpleNamespace(close=AsyncMock())
    server.speech = SimpleNamespace(close=AsyncMock())

    with pytest.raises(RuntimeError, match="ALSA shutdown failed"):
        await server.close()

    server.audio.stop.assert_awaited_once()
    server.modem.stop.assert_awaited_once()
    server.speech.close.assert_awaited_once()
    server.http.close.assert_awaited_once()


async def test_api_rejects_a_json_array_before_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("OPTIONS_PATH", str(tmp_path / "options.json"))
    server = Server({"api_token": "test"})
    async with TestClient(TestServer(server.app())) as client:
        response = await client.post(
            "/q1/led", json=[], headers={"Authorization": "Bearer test"}
        )
        assert response.status == 400
        assert await response.text() == "Body must be a JSON object"
