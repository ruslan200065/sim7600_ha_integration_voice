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
    import main as server_module
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
        self.assertEqual(service.next_prompt, (1, "test prompt", None))

    async def test_dial_reports_modem_rejection_and_clears_prompt(self):
        service = Server({"api_token": "test", "allowed_number": "+79991234567"})
        service.modem.call_state = "idle"
        service.modem.dial = AsyncMock(side_effect=RuntimeError("Dial failed: BUSY"))
        with self.assertRaisesRegex(web.HTTPConflict, "Conflict") as error:
            await service.dial_number()
        self.assertEqual(error.exception.text, "Dial failed: BUSY")
        self.assertIsNone(service.next_prompt)

    async def test_named_family_recipient_is_resolved_without_using_owner(self):
        service = Server({"api_token": "test", "allowed_number": "+12025550101",
                          "family_members": ["Test family | +12025550102"]})
        service.modem.call_state = "idle"
        service.modem.dial = AsyncMock()
        await service.dial_number(member="test FAMILY")
        service.modem.dial.assert_awaited_once_with("+12025550102")

    async def test_gate_with_optional_pin_and_keypad(self):
        for pin in ("", "00123456789", "7"):
            with self.subTest(pin_length=len(pin)):
                service = Server({"api_token": "test", "gate_pin": pin,
                                  "family_members": ["Member | +12025550102 | | cover.test"]})
                service.modem.active_number = "+12025550102"
                service.modem.call_state = "connected"
                service.modem.enable_pcm = AsyncMock()
                service.audio.start = AsyncMock()
                service.audio.stop = AsyncMock()
                service.brain = SimpleNamespace(_ha=AsyncMock())
                service.caller_brain = lambda member: None

                async def dialog(_speech, reply, _source, _play, _hangup, _prompt, *, keypad, on_key, **_):
                    progress = lambda text: None
                    await on_key("#")
                    service.brain._ha.assert_not_awaited()
                    text = await reply("Открой калитку", on_progress=progress)
                    if pin:
                        self.assertIn("решётку", text)
                        service.brain._ha.assert_not_awaited()
                        for key in "3*" + pin + "#":
                            await service.event("dtmf_received", {"digit": key, "call_id": service.modem.call_id})
                            result = await on_key(keypad.get_nowait())
                        self.assertIn("отправлена", result)
                        await on_key("#")
                    else:
                        self.assertIn("отправлена", text)
                with patch.object(server_module, "run_dialog", dialog):
                    await service._dialog("", None)
                service.brain._ha.assert_awaited_once_with("POST", "/api/services/cover/open_cover", {"entity_id": "cover.test"})
                self.assertEqual(service.events, [])
                self.assertIsNone(service.keypad)
