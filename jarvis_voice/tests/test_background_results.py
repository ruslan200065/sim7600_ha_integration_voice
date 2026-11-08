"""Concurrency regressions: fast turns, background results and playback failures."""
import asyncio
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from brain import Brain, REPLY_TURN
from voice_dialog import CallOutput, run_dialog


class Response:
    status = 200
    content_type = "application/json"
    def __init__(self, message):
        self.message = {"role": "assistant", **message}
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    async def json(self):
        return {"choices": [{"message": self.message, "finish_reason": "stop"}]}


class BackgroundTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_action_does_not_block_new_turn_and_result_reaches_context(self):
        started, release = asyncio.Event(), asyncio.Event()
        payloads = []
        class Http:
            def post(self, _url, *, json, **kwargs):
                payloads.append(copy.deepcopy(json))
                users = [m["content"] for m in json["messages"] if m["role"] == "user"]
                if users[-1] == "Включи свет" and not any(m["role"] == "tool" for m in json["messages"]):
                    return Response({"content": None, "tool_calls": [{"id": "act1", "function": {
                        "name": "call_ha_service", "arguments": '{"entity_id":"light.hall","service":"turn_on"}'}}]})
                return Response({"content": "Свет включён." if users[-1] == "Включи свет" else "Слушаю."})
        brain = Brain({"deepseek_api_key": "test", "deepseek_model": "deepseek-chat", "stream_replies": False}, Http(), None)
        async def tool(*args, **kwargs):
            started.set()
            await release.wait()
            return {"accepted": True, "device": {"entity_id": "light.hall", "state": "on"}}
        brain._tool = tool
        first = asyncio.create_task(brain.reply("Включи свет", on_progress=lambda text: None))
        await started.wait()
        # A second answer is ready while the action is still waiting, without serialization.
        async with asyncio.timeout(1):
            second = await brain.reply("Ты тут?", on_progress=lambda text: None)
        self.assertEqual(second, "Слушаю.")
        self.assertFalse(first.done())
        release.set()
        result = await first
        self.assertIsInstance(result, CallOutput)
        self.assertTrue(result.background_result)
        self.assertEqual(result.text, "Свет включён.")
        self.assertEqual(len(brain.completed_actions), 1)
        await brain.reply("Что сделал?", on_progress=lambda text: None)
        context = " ".join(m["content"] for m in payloads[-1]["messages"] if m["role"] == "system")
        self.assertIn("light.hall", context)
        self.assertIn("call_ha_service", context)
        self.assertEqual([m["content"] for m in brain.history if m["role"] == "user"],
                         ["Включи свет", "Ты тут?", "Что сделал?"])

    async def test_old_turn_cannot_confirm_itself_after_another_turn_starts(self):
        brain = Brain({"confirm_risky_actions": True}, None, None)
        key = ("action", "light.hall")
        brain.turn = 1
        token = REPLY_TURN.set(1)
        try:
            self.assertTrue(brain._needs_confirmation(key, {}, "Выполнить?")["needs_confirmation"])
            brain.turn = 2
            # Global turn advanced, but this is still the original LLM request.
            self.assertTrue(brain._needs_confirmation(key, {"confirmed": True}, "Выполнить?")["needs_confirmation"])
        finally:
            REPLY_TURN.reset(token)
        token = REPLY_TURN.set(2)
        try:
            self.assertIsNone(brain._needs_confirmation(key, {"confirmed": True}, "Выполнить?"))
        finally:
            REPLY_TURN.reset(token)

    async def test_background_result_is_spoken_after_newer_turn(self):
        older_started, newer_started, heard_result = asyncio.Event(), asyncio.Event(), asyncio.Event()
        heard = []
        class Speech:
            turn = 0
            async def recognize_live(self, *args, **kwargs):
                self.turn += 1
                if self.turn == 1:
                    return "Включи свет"
                if self.turn == 2:
                    await older_started.wait()
                    return "Ты тут?"
                if self.turn == 3:
                    await heard_result.wait()
                    return "Пока"
                await asyncio.Future()
        async def reply(phrase):
            if phrase == "Включи свет":
                older_started.set()
                await newer_started.wait()
                return CallOutput("Свет включён.", background_result=True)
            if phrase == "Ты тут?":
                newer_started.set()
                return "Да."
            return CallOutput("До связи.", True)
        async def play(text):
            heard.append(text)
            if text == "Свет включён.":
                heard_result.set()
        async with asyncio.timeout(2):
            await run_dialog(Speech(), reply, asyncio.Queue(), play, AsyncMock(), "Привет")
        self.assertIn("Свет включён.", heard)

    async def test_queued_background_result_survives_another_generation(self):
        first_started, release_old, result_queued, heard_result = (
            asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event())
        heard = []
        class Speech:
            turn = 0
            async def recognize_live(self, *args, on_partial, **kwargs):
                self.turn += 1
                if self.turn == 1:
                    return "Включи свет"
                if self.turn == 2:
                    await first_started.wait()
                    on_partial("Еще вопрос")  # Hold playback until this phrase is final.
                    release_old.set()
                    await result_queued.wait()
                    # Let the old answer enqueue while audio_ready remains clear.
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    return "Еще вопрос"
                if self.turn == 3:
                    await heard_result.wait()
                    return "Пока"
                await asyncio.Future()
        async def reply(phrase):
            if phrase == "Включи свет":
                first_started.set()
                await release_old.wait()
                result_queued.set()
                return CallOutput("Свет включён.", background_result=True)
            return CallOutput("До связи.", True) if phrase == "Пока" else "Да."
        async def play(text):
            heard.append(text)
            if text == "Свет включён.":
                heard_result.set()
        async with asyncio.timeout(2):
            await run_dialog(Speech(), reply, asyncio.Queue(), play, AsyncMock(), "Привет")
        self.assertIn("Свет включён.", heard)

    async def test_playback_failure_reaches_session_recovery(self):
        class Speech:
            async def recognize_live(self, *args, **kwargs):
                await asyncio.Future()
        async def play(_text):
            raise RuntimeError("speech transport exhausted")
        with self.assertRaises(ExceptionGroup) as raised:
            async with asyncio.timeout(2):
                await run_dialog(Speech(), AsyncMock(), asyncio.Queue(), play, AsyncMock(), "Привет")
        self.assertIn("speech transport exhausted", str(raised.exception.exceptions[0]))
