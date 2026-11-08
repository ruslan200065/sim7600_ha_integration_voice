"""Telephony and SMS events are forwarded to the Home Assistant bus."""

import asyncio
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
with patch.dict(sys.modules, {"speech": SimpleNamespace(SpeechKit=object)}):
    from main import Server


class HomeAssistantEventTests(unittest.IsolatedAsyncioTestCase):
    def service(self, **options):
        service = Server({"api_token": "test", "allowed_number": "+79991234567",
                          "family_members": ["Member | +79990000001"], **options})
        service.brain = SimpleNamespace(hass_token="token", _ha=AsyncMock())
        return service

    def fired(self, service):
        return [(call.args[1], call.args[2]) for call in service.brain._ha.await_args_list]

    async def settle(self):
        for _ in range(3):
            await asyncio.sleep(0)

    async def test_call_lifecycle_events_keep_number_until_end(self):
        service = self.service()
        service.modem.call_id = 7
        await service.event("incoming_call", {"number": ""})
        service.modem.active_number = "89990000001"
        await service.event("caller_id", {"number": "89990000001"})
        service.modem.active_number = ""
        await service.event("call_ended", {"call_id": 7, "reason": "NO CARRIER"})
        await self.settle()
        self.assertEqual(self.fired(service), [
            ("/api/events/jarvis_call_incoming",
             {"number": "89990000001", "known": True, "call_id": 7}),
            ("/api/events/jarvis_call_ended",
             {"number": "89990000001", "reason": "NO CARRIER", "call_id": 7}),
        ])

    async def test_dtmf_inside_jarvis_dialog_is_not_forwarded(self):
        service = self.service()
        service.keypad = (3, asyncio.Queue(maxsize=8))
        await service.event("dtmf_received", {"call_id": 3, "digit": "1"})
        await self.settle()
        self.assertEqual(self.fired(service), [])
        service.keypad = None
        await service.event("dtmf_received", {"call_id": 4, "digit": "5"})
        await self.settle()
        self.assertEqual(self.fired(service),
                         [("/api/events/jarvis_dtmf", {"digit": "5", "call_id": 4})])

    async def test_events_can_be_disabled(self):
        service = self.service(ha_events=False)
        await service.event("call_ended", {"call_id": 1})
        await self.settle()
        self.assertEqual(self.fired(service), [])

    async def test_completed_sms_fires_event(self):
        service = self.service()
        service.history = SimpleNamespace(import_sms=AsyncMock(return_value=[
            {"number": "+70000000000", "text": "Привет", "received_at": 10, "parts": 1},
        ]))
        await service.import_sms(["+CMGL: ..."])
        await self.settle()
        self.assertEqual(self.fired(service), [(
            "/api/events/jarvis_sms_received",
            {"number": "+70000000000", "text": "Привет", "received_at": 10,
             "parts": 1, "known": False},
        )])


if __name__ == "__main__":
    unittest.main()
