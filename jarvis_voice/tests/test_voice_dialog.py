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
            await run_dialog(Speech(), brain.reply, asyncio.Queue(), play, hangup, greeting, echo_filter=True)

        brain.reply.assert_awaited_once_with("Какая погода сегодня?")
        hangup.assert_awaited_once()

    async def test_tool_survives_interruption_without_replaying_stale_reply(self):
        progress_played = asyncio.Event()
        new_answer_played = asyncio.Event()
        tool_completed = asyncio.Event()
        spoken = []

        async def play(text):
            spoken.append(text)
            if text == "Готовлю снимок":
                progress_played.set()
                await asyncio.Future()
            elif text == "Свет включён":
                new_answer_played.set()

        async def brain(phrase, *, on_progress):
            if phrase == "Отправь фото":
                on_progress("Готовлю снимок")
                await new_answer_played.wait()
                tool_completed.set()
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
                await tool_completed.wait()
                await asyncio.sleep(0)
                return "До свидания"

        async with asyncio.timeout(2):
            await run_dialog(Speech(), brain, asyncio.Queue(), play, AsyncMock(), "Привет",
                             background_tools=True)
        self.assertNotIn("Фото отправлено", spoken)

    async def test_hangs_up_on_silence(self):
        class SilentSpeech:
            async def recognize_live(self, *args, **kwargs):
                await asyncio.Future()

        hangup = AsyncMock()
        play = AsyncMock()
        async with asyncio.timeout(2):
            await run_dialog(SilentSpeech(), AsyncMock(), asyncio.Queue(), play, hangup,
                             "Привет", idle_seconds=0.02)
        hangup.assert_awaited_once()
        play.assert_any_await("До связи.")

    async def test_status_question_keeps_the_original_reply(self):
        status = asyncio.Event()
        completed = asyncio.Event()

        async def play(text):
            if text == "Я услышал, ещё разбираюсь.":
                status.set()
            if text == "Температура двадцать градусов.":
                completed.set()

        class Speech:
            turn = 0
            async def recognize_live(self, _source, *, on_partial, **kwargs):
                self.turn += 1
                if self.turn == 1:
                    return "Какая температура?"
                if self.turn == 2:
                    on_partial("Чё молчишь")
                    return "Чё молчишь?"
                await completed.wait()
                return "До свидания"

        async def reply(text):
            self.assertEqual(text, "Какая температура?")
            await status.wait()
            return "Температура двадцать градусов."

        async with asyncio.timeout(2):
            await run_dialog(Speech(), reply, asyncio.Queue(), play, AsyncMock(), "Привет")

    async def test_echo_filter_is_off_by_default(self):
        greeting = "Добрый вечер слушаю"
        started = asyncio.Event()
        answered = asyncio.Event()

        async def play(text):
            if text == greeting:
                started.set()
                await asyncio.Future()
            if text == "Привет.":
                answered.set()

        class Speech:
            turn = 0
            async def recognize_live(self, _source, *, on_partial, **kwargs):
                self.turn += 1
                if self.turn == 1:
                    await started.wait()
                    on_partial(greeting)
                    return greeting
                await answered.wait()
                return "До свидания"

        reply = AsyncMock(return_value="Привет.")
        async with asyncio.timeout(2):
            await run_dialog(Speech(), reply, asyncio.Queue(), play, AsyncMock(), greeting)
        reply.assert_awaited_once_with(greeting)
