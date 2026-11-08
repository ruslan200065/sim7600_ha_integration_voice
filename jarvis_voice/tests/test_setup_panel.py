"""Check settings validation, secrets, and ingress authorization."""

import json
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from setup_panel import SetupPanel, validate_changes


class PanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.options = {
            "at_port": "/dev/ttyUSB2",
            "pcm_port": "/dev/ttyUSB4",
            "auto_answer": False,
            "api_token": "private-rest-token",
            "yandex_api_key": "!secret speechkit",
            "deepseek_api_key": "private-cloud-token",
        }
        service = SimpleNamespace(
            o=self.options.copy(), modem=SimpleNamespace(call_state="idle")
        )
        self.panel = SetupPanel(service)
        self.info = {"version": "0.4.0", "options": self.options, "audio_input": None}
        self.panel.supervisor = AsyncMock(return_value=self.info)

    async def test_state_never_returns_keys(self):
        with (
            patch("setup_panel.probe_audio", AsyncMock(return_value={})),
            patch("setup_panel.list_serial", return_value=[]),
            patch("setup_panel.list_video", return_value=[]),
        ):
            response = await self.panel.state(None)
        self.assertNotIn("private", response.text)
        self.assertNotIn("!secret", response.text)
        self.assertEqual(
            json.loads(response.text)["keys"],
            {"yandex_api_key": True, "deepseek_api_key": True, "gate_pin": False, "speech_fallback_api_key": False},
        )

    async def test_save_preserves_advanced_options_and_secret_reference(self):
        request = Mock(
            json=AsyncMock(
                return_value={
                    "revision": self.panel.revision(self.info),
                    "options": {"at_port": "/dev/serial/by-id/usb-SIM-if02-port0"},
                    "audio": {"audio_input": "alsa_input.usb-Sonix.analog-stereo"},
                }
            )
        )
        response = await self.panel.save(request)
        self.assertEqual(response.status, 200)
        method, route, data = self.panel.supervisor.call_args.args
        self.assertEqual((method, route), ("POST", "options"))
        self.assertEqual(data["options"]["api_token"], "private-rest-token")
        self.assertEqual(data["options"]["yandex_api_key"], "!secret speechkit")
        self.assertEqual(
            data["options"]["at_port"], "/dev/serial/by-id/usb-SIM-if02-port0"
        )

    async def test_stale_revision_never_writes(self):
        request = Mock(
            json=AsyncMock(return_value={"revision": "stale", "options": {}})
        )
        with self.assertRaises(web.HTTPConflict):
            await self.panel.save(request)
        self.panel.supervisor.assert_awaited_once_with("GET", "info")

    async def test_ingress_requires_supervisor_and_mutation_header(self):
        handler = AsyncMock(return_value=web.Response())
        request = Mock(
            remote="192.168.0.10", method="GET", headers={"X-Remote-User-Id": "fake"}
        )
        with self.assertRaises(web.HTTPForbidden):
            await self.panel.authorize(request, handler)
        request.remote = "172.30.32.2"
        request.method = "PUT"
        with self.assertRaises(web.HTTPForbidden):
            await self.panel.authorize(request, handler)
        request.headers["X-Jarvis-Setup"] = "1"
        response = await self.panel.authorize(request, handler)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        handler.assert_awaited_once()

    def test_conflicting_devices_and_missing_allowlist(self):
        with self.assertRaisesRegex(ValueError, "разные порты"):
            validate_changes({"pcm_port": "/dev/ttyUSB2"}, self.options)
        with self.assertRaisesRegex(ValueError, "разрешённый номер"):
            validate_changes({"auto_answer": True}, self.options)
