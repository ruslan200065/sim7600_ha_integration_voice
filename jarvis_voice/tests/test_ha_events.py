"""Telephony and SMS events are forwarded to the Home Assistant bus."""

import asyncio
import pathlib
import sys
import unittest
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
with patch.dict(sys.modules, {"speech": SimpleNamespace(SpeechKit=object)}):
    from main import Server
from call_outbox import CallOutbox


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

    async def test_short_call_is_buffered_when_direct_delivery_fails(self):
        service = self.service()
        service.brain._ha.side_effect = OSError("Home Assistant unavailable")
        service.modem.call_id = 8
        await service.event("incoming_call", {"number": ""})
        await service.event("caller_id", {"number": "+79990000001"})
        await service.event("call_ended", {"call_id": 8})
        await self.settle()

        calls = list(service.call_events)
        self.assertEqual([e["sequence"] for e in calls], [1, 2])
        self.assertEqual([e["name"] for e in calls],
                         ["jarvis_call_incoming", "jarvis_call_ended"])
        self.assertTrue(all(e["delivery"] == "companion" for e in calls))
        self.assertTrue(all(e["data"]["call_id"] == 8 for e in calls))

    async def test_call_events_survive_restart_and_acknowledgement(self):
        with TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "outbox.sqlite3"
            service = self.service()
            service.call_outbox = CallOutbox(path)
            service.call_event_stream = service.call_outbox.stream
            service.brain._ha.side_effect = OSError("Home Assistant offline")

            await service.event("incoming_call", {"number": ""})
            await service.event("caller_id", {"number": "+79990000001"})
            await service.event("call_ended", {"call_id": 4})
            await self.settle()

            recovered = CallOutbox(path)
            self.assertEqual(recovered.stream, service.call_event_stream)
            calls = recovered.batch()
            self.assertEqual([call["sequence"] for call in calls], [1, 2])
            self.assertEqual(calls[0]["data"]["number"], "+79990000001")
            self.assertEqual(
                self.fired(service)[0][1]["event_id"], f"{recovered.stream}:1"
            )
            self.assertFalse(recovered.acknowledge("wrong-stream", 2))
            self.assertEqual(recovered.batch(), calls)
            self.assertTrue(recovered.acknowledge(recovered.stream, 1))
            self.assertEqual(CallOutbox(path).batch(), calls[1:])

    async def test_interrupted_send_becomes_replayable_after_restart(self):
        with TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "outbox.sqlite3"
            outbox = CallOutbox(path)
            sequence = outbox.record({
                "name": "jarvis_call_incoming",
                "data": {"call_id": 1},
                "delivery": "pending",
            })
            self.assertEqual(outbox.batch()[0]["delivery"], "pending")
            recovered = CallOutbox(path)
            self.assertEqual(recovered.batch()[0]["sequence"], sequence)
            self.assertEqual(recovered.batch()[0]["delivery"], "companion")

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
