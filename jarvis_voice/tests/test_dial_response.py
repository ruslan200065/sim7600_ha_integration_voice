"""An accepted modem dial must return success to both HTTP entry points."""

import json
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
with patch.dict(sys.modules, {"speech": SimpleNamespace(SpeechKit=object)}):
    from main import Server


class DialResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_dial_returns_success_and_keeps_prompt_for_current_call(self):
        service = Server({"api_token": "test", "allowed_number": "+79991234567"})

        async def dial(_number):
            service.modem.call_id += 1
            service.modem.call_state = "dialing"

        service.modem.call_state = "idle"
        service.modem.dial = AsyncMock(side_effect=dial)
        response = await service.dial_number(prompt="test prompt")
        self.assertEqual(json.loads(response.text), {"ok": True, "state": "dialing"})
        self.assertEqual(service.next_prompt, (1, "test prompt"))

    async def test_dial_reports_modem_rejection_and_clears_prompt(self):
        service = Server({"api_token": "test", "allowed_number": "+79991234567"})
        service.modem.call_state = "idle"
        service.modem.dial = AsyncMock(side_effect=RuntimeError("Dial failed: BUSY"))
        with self.assertRaisesRegex(web.HTTPConflict, "Conflict") as error:
            await service.dial_number()
        self.assertEqual(error.exception.text, "Dial failed: BUSY")
        self.assertIsNone(service.next_prompt)
