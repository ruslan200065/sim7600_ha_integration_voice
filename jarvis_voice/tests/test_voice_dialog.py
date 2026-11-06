import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voice_dialog import run_dialog


class TestVoiceDialog(unittest.IsolatedAsyncioTestCase):
    async def test_echo_is_ignored_and_caller_can_interrupt(self):
        greeting = "Здравствуйте. Это Джарвис. Слушаю вас."
        greeting_started = asyncio.Event()
        greeting_stopped = asyncio.Event()
        answered = asyncio.Event()

        async def play(text):
            if text == greeting:
                greeting_started.set()
                try:
                    await asyncio.Future()
                finally:
                    greeting_stopped.set()
            elif text == "Проверяю погоду":
                answered.set()

        class Speech:
            turn = 0

            async def recognize_live(self, source, *, threshold, on_voice_start, on_partial):
                self.turn += 1
                if self.turn == 1:
                    await greeting_started.wait()
                    on_voice_start()
                    on_partial("Это Джарвис")
                    return "Это Джарвис"
                if self.turn == 2:
                    if greeting_stopped.is_set():
                        raise AssertionError("The greeting was interrupted by its echo")
                    on_voice_start()
                    on_partial("Какая погода")
                    await greeting_stopped.wait()
                    return "Какая погода сегодня?"
                await answered.wait()
                on_voice_start()
                return "До свидания"

        brain = unittest.mock.Mock()
        brain.reply = AsyncMock(return_value="Проверяю погоду")
        hangup = AsyncMock()
        async with asyncio.timeout(2):
            await run_dialog(Speech(), brain.reply, asyncio.Queue(), play, hangup, greeting)

        brain.reply.assert_awaited_once_with("Какая погода сегодня?")
        hangup.assert_awaited_once()

    async def test_tool_survives_interruption_and_reports_after_new_reply(self):
        progress_played = asyncio.Event()
        new_answer_played = asyncio.Event()
        completion_played = asyncio.Event()

        async def play(text):
            if text == "Готовлю снимок":
                progress_played.set()
                await asyncio.Future()
            elif text == "Свет включён":
                new_answer_played.set()
            elif text == "Фото отправлено":
                completion_played.set()

        async def brain(phrase, *, on_progress):
            if phrase == "Отправь фото":
                on_progress("Готовлю снимок")
                await new_answer_played.wait()
                return "Фото отправлено"
            return "Свет включён"

        class Speech:
            turn = 0

            async def recognize_live(self, source, *, threshold, on_voice_start, on_partial):
                self.turn += 1
                if self.turn == 1:
                    return "Отправь фото"
                if self.turn == 2:
                    await progress_played.wait()
                    on_partial("А свет")
                    return "А свет включён?"
                await completion_played.wait()
                return "До свидания"

        async with asyncio.timeout(2):
            await run_dialog(Speech(), brain, asyncio.Queue(), play, AsyncMock(), "Привет",
                             background_tools=True)
