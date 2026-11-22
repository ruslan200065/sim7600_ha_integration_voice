"""Regression tests for add-on cleanup and HTTP input validation."""

import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
with patch.dict(sys.modules, {"speech": SimpleNamespace(SpeechKit=object)}):
    from main import Server
    from setup_panel import SetupPanel


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


async def test_discovery_uses_supervisor_hostname(tmp_path, monkeypatch):
    monkeypatch.setenv("OPTIONS_PATH", str(tmp_path / "options.json"))
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-test")
    server = Server({"api_token": "app-test"})
    server.http = MagicMock()
    info = server.http.get.return_value.__aenter__.return_value
    info.raise_for_status = MagicMock()
    info.json = AsyncMock(return_value={"data": {"hostname": "a123-jarvis-sim7600"}})
    discovery = server.http.post.return_value.__aenter__.return_value
    discovery.raise_for_status = MagicMock()
    discovery.json = AsyncMock(return_value={"result": "ok"})

    assert await server.discover_companion()
    assert server.http.post.call_args.kwargs["json"] == {
        "service": "jarvis_q1",
        "config": {
            "host": "a123-jarvis-sim7600",
            "port": 8099,
            "token": "app-test",
        },
    }


async def test_companion_credentials_require_supervisor_ingress(tmp_path, monkeypatch):
    monkeypatch.setenv("OPTIONS_PATH", str(tmp_path / "options.json"))
    server = Server({"api_token": "app-test"})
    server.companion_address = AsyncMock(return_value="internal-app")
    async with TestClient(TestServer(SetupPanel(server).app())) as client:
        response = await client.post(
            "/companion/credentials",
            headers={"X-Remote-User-Id": "fake", "X-Jarvis-Setup": "1"},
        )
        assert response.status == 403
        assert "app-test" not in await response.text()
    server.companion_address.assert_not_awaited()
