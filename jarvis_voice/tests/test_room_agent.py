import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp.web  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
with patch.dict(sys.modules, {"speech": SimpleNamespace(SpeechKit=object)}):
    from main import Server
    from setup_panel import validate_changes


def make_service(**options):
    service = Server({"api_token": "test", "allowed_number": "+79991234567", **options})
    service.brain = SimpleNamespace(hass_token="token", _ha=AsyncMock())
    return service


class RoomAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_agent_is_room_brain(self):
        service = make_service()
        room = SimpleNamespace(reply=AsyncMock(return_value="Свет включён."))
        service.room_brain = lambda: room
        self.assertEqual(await service.room_reply("включи свет"), "Свет включён.")
        service.brain._ha.assert_not_awaited()

    async def test_assist_agent_keeps_conversation(self):
        service = make_service(room_agent="assist", assist_agent_id="conversation.home_assistant")
        service.brain._ha.return_value = {
            "conversation_id": "abc",
            "response": {"speech": {"plain": {"speech": "Включила свет"}}},
        }
        self.assertEqual(await service.room_reply("включи свет"), "Включила свет")
        method, path, payload = service.brain._ha.await_args.args
        self.assertEqual((method, path), ("POST", "/api/conversation/process"))
        self.assertEqual(payload["agent_id"], "conversation.home_assistant")
        self.assertNotIn("conversation_id", payload)
        await service.room_reply("и выключи")
        self.assertEqual(service.brain._ha.await_args.args[2]["conversation_id"], "abc")

    async def test_assist_failure_is_spoken(self):
        service = make_service(room_agent="assist")
        service.brain._ha.side_effect = RuntimeError("HTTP 500")
        self.assertIn("недоступен", await service.room_reply("привет"))


class RoomAgentValidationTests(unittest.TestCase):
    def test_rejects_unknown_agent(self):
        with self.assertRaises(ValueError):
            validate_changes({"room_agent": "other"}, {})
        with self.assertRaises(ValueError):
            validate_changes({"assist_agent_id": "bad id"}, {})
        options = validate_changes({"room_agent": "assist", "assist_agent_id": "conversation.x"}, {})
        self.assertEqual(options["room_agent"], "assist")


if __name__ == "__main__":
    unittest.main()
