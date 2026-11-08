"""Call summaries are posted to Home Assistant after a call."""

import asyncio
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp.web  # noqa: F401  # import before patch.dict so it is not unloaded afterwards

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
with patch.dict(sys.modules, {"speech": SimpleNamespace(SpeechKit=object)}):
    from brain import Brain
    from main import Server


def make_service(**options):
    service = Server({"api_token": "test", "allowed_number": "+79991234567",
                      "family_members": ["Member | +79990000001"], **options})
    service.brain = SimpleNamespace(hass_token="token", _ha=AsyncMock(return_value={}))
    return service


class CallSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_posts_notification_and_event(self):
        service = make_service(call_summary=True, ha_events=True)
        brain = SimpleNamespace(summarize=AsyncMock(return_value="Звонил участник, просил включить свет."))
        service.schedule_call_summary(brain, "79990000001", 7)
        await asyncio.gather(*service.__dict__["summary_tasks"])
        await asyncio.gather(*service.__dict__.get("ha_event_tasks", set()))
        calls = [call.args for call in service.brain._ha.await_args_list]
        notification = next(args for args in calls if args[1] == "/api/services/persistent_notification/create")
        self.assertIn("Member", notification[2]["title"])
        self.assertEqual(notification[2]["notification_id"], "jarvis_call_7")
        self.assertTrue(any(args[1] == "/api/events/jarvis_call_summary" for args in calls))

    async def test_summary_disabled_by_default(self):
        service = make_service()
        brain = SimpleNamespace(summarize=AsyncMock(return_value="text"))
        service.schedule_call_summary(brain, "79990000001", 1)
        self.assertFalse(service.__dict__.get("summary_tasks"))
        brain.summarize.assert_not_awaited()

    async def test_empty_summary_posts_nothing(self):
        service = make_service(call_summary=True)
        brain = SimpleNamespace(summarize=AsyncMock(return_value=""))
        service.schedule_call_summary(brain, "", 2)
        await asyncio.gather(*service.__dict__["summary_tasks"])
        service.brain._ha.assert_not_awaited()


class TranscriptTests(unittest.TestCase):
    def test_transcript_keeps_only_spoken_turns(self):
        brain = Brain.__new__(Brain)
        brain.history = [
            {"role": "user", "content": "Включи   свет"},
            {"role": "assistant", "content": None, "tool_calls": []},
            {"role": "tool", "content": "{}"},
            {"role": "assistant", "content": "Готово."},
        ]
        self.assertEqual(brain.transcript(), "Собеседник: Включи свет\nДжарвис: Готово.")


if __name__ == "__main__":
    unittest.main()
