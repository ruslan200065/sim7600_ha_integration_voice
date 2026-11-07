import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voice_dialog import CallOutput, run_dialog


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
                if self.turn == 3:
                    await answered.wait()
                    on_voice_start()
                    return "До свидания"
                await asyncio.Future()

        brain = unittest.mock.Mock()
        brain.reply = AsyncMock(side_effect=["Проверяю погоду", CallOutput("До связи.", True)])
        hangup = AsyncMock()
        async with asyncio.timeout(2):
            await run_dialog(Speech(), brain.reply, asyncio.Queue(), play, hangup, greeting, echo_filter=True)

        self.assertEqual([call.args[0] for call in brain.reply.await_args_list],
                         ["Какая погода сегодня?", "До свидания"])
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
            if phrase == "До свидания":
                return CallOutput("До связи.", True)
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
                if self.turn == 3:
                    await tool_completed.wait()
                    await asyncio.sleep(0)
                    return "До свидания"
                await asyncio.Future()

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

    async def test_only_model_can_choose_to_end_a_call(self):
        response_spoken = asyncio.Event()
        heard = []

        async def play(text):
            heard.append(text)
            if text == "Продолжаю слушать.":
                response_spoken.set()

        class Speech:
            turn = 0

            async def recognize_live(self, _source, **kwargs):
                self.turn += 1
                if self.turn == 1:
                    return "Что значит до свидания?"
                if self.turn == 2:
                    await response_spoken.wait()
                    return "Этого достаточно."
                await asyncio.Future()

        reply = AsyncMock(side_effect=["Продолжаю слушать.", CallOutput("Хорошего вечера!", True)])
        hangup = AsyncMock()
        async with asyncio.timeout(2):
            await run_dialog(Speech(), reply, asyncio.Queue(), play, hangup, "Привет")
        self.assertEqual(heard[-1], "Хорошего вечера!")
        hangup.assert_awaited_once()

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
                if self.turn == 2:
                    await answered.wait()
                    return "До свидания"
                await asyncio.Future()

        reply = AsyncMock(side_effect=["Привет.", CallOutput("До связи.", True)])
        async with asyncio.timeout(2):
            await run_dialog(Speech(), reply, asyncio.Queue(), play, AsyncMock(), greeting)
        self.assertEqual([call.args[0] for call in reply.await_args_list], [greeting, "До свидания"])
