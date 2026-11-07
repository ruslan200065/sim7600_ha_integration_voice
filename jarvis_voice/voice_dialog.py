"""Coordinate recognition and playback for a single modem call."""

import asyncio
import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

LOG = logging.getLogger(__name__)

@dataclass(frozen=True)
class CallOutput:
    text: str
    end_call: bool = False


class CallFinished(Exception):
    """End the listening task after the farewell has played."""


def words(text):
    return re.findall(r"[а-яa-z0-9]+", text.lower().replace("ё", "е"))


def echoes_playback(phrase, outputs):
    count = len(phrase)
    if count < 2:
        return False
    heard = " ".join(phrase)
    for output in outputs:
        for index in range(len(output) - count + 1):
            candidate = output[index : index + count]
            if phrase == candidate and (count >= 3 or phrase == ["это", "джарвис"] or count == len(output)):
                return True
            if count >= 4 and SequenceMatcher(None, heard, " ".join(candidate)).ratio() >= 0.86:
                return True
    return False


async def run_dialog(speech, reply_to, source, play, hangup, prompt, *, background_tools=False,
                     keypad=None, on_key=None, idle_seconds=75, echo_filter=False):
    """Listen throughout a reply and interrupt once the caller is recognized."""
    clock = asyncio.get_running_loop().time
    last_spoken = []
    echo_until = 0.0

    async def speak(text):
        nonlocal last_spoken, echo_until
        if echo_filter:
            last_spoken = words(text)
            echo_until = float("inf")
        try:
            await play(text)
        finally:
            # Remote speaker echo and delayed transcripts can outlast playback.
            if echo_filter:
                echo_until = clock() + 2.0

    output = asyncio.Queue()
    audio_ready = asyncio.Event()
    audio_ready.set()
    playing = None
    generation = 0
    pending = set()
    idle = None
    last_recovery = float("-inf")

    async def playback():
        nonlocal playing
        while True:
            message, turn = await output.get()
            await audio_ready.wait()
            if turn == generation:
                text = message.text if isinstance(message, CallOutput) else message
                playing = asyncio.create_task(speak(text))
                try:
                    result, = await asyncio.gather(playing, return_exceptions=True)
                    if isinstance(result, Exception):
                        LOG.warning("Playback failed: %s", type(result).__name__)
                    if (isinstance(message, CallOutput) and message.end_call
                        and turn == generation and not isinstance(result, asyncio.CancelledError)):
                        await hangup()
                        raise CallFinished
                finally:
                    playing = None

    async def answer(phrase, turn):
        spoke = False

        def progress(text):
            nonlocal spoke
            spoke = True
            if turn == generation:
                output.put_nowait((text, turn))

        async def acknowledge():
            await asyncio.sleep(2.5)
            if not spoke and turn == generation:
                output.put_nowait(("Секунду, я услышал.", turn))

        fallback = asyncio.create_task(acknowledge())
        try:
            if background_tools:
                text = await reply_to(phrase, on_progress=progress)
            else:
                text = await reply_to(phrase)
        except Exception as exc:
            LOG.warning("Voice reply failed: %s", type(exc).__name__)
            text = "Возникла ошибка. Попробуй ещё раз."
        finally:
            fallback.cancel()
            await asyncio.gather(fallback, return_exceptions=True)
        LOG.info("LLM answer length=%d", len(text.text if isinstance(text, CallOutput) else text or ""))
        if turn == generation:
            output.put_nowait((text or "Не получилось ответить. Попробуй ещё раз.", turn))

    async def listen_keypad():
        while True:
            key = await keypad.get()
            if idle is not None and not idle.expired():
                idle.reschedule(clock() + idle_seconds)
            turn = generation
            text = await on_key(key)
            if text and turn == generation:
                output.put_nowait((text, turn))

    output.put_nowait((prompt, generation))
    try:
        async with asyncio.TaskGroup() as tasks:
            player = tasks.create_task(playback())
            keys = tasks.create_task(listen_keypad()) if keypad is not None and on_key else None
            try:
                while True:
                    heard_playback = []
                    user_partial = False

                    def remember_output():
                        if clock() <= echo_until and last_spoken and last_spoken not in heard_playback:
                            heard_playback.append(last_spoken)

                    def interrupt(partial):
                        nonlocal user_partial
                        remember_output()
                        tokens = words(partial)
                        confirmed = len(tokens) >= 2
                        if confirmed and not (echo_filter and echoes_playback(tokens, heard_playback)):
                            user_partial = True
                            audio_ready.clear()
                            if playing and not playing.done():
                                playing.cancel()

                    try:
                        async with asyncio.timeout(idle_seconds) as idle:
                            def voice_started():
                                remember_output()
                                idle.reschedule(clock() + idle_seconds)

                            phrase = await speech.recognize_live(
                                source, threshold=450, on_voice_start=voice_started, on_partial=interrupt
                            )
                    except TimeoutError:
                        audio_ready.set()
                        if idle.expired():
                            if pending or playing or not output.empty():
                                continue
                            await speak("До связи.")
                            await hangup()
                            return
                        phrase = ""
                        user_partial = True
                    except Exception as exc:
                        audio_ready.set()
                        LOG.warning("STT failed: %s", type(exc).__name__)
                        if clock() - last_recovery >= 10:
                            output.put_nowait(("Не получилось расслышать. Скажи ещё раз.", generation))
                            last_recovery = clock()
                        await asyncio.sleep(1)
                        continue
                    finally:
                        idle = None
                    remember_output()
                    tokens = words(phrase)
                    if not tokens or (echo_filter and echoes_playback(tokens, heard_playback)):
                        audio_ready.set()
                        if not tokens and user_partial and clock() - last_recovery >= 5:
                            output.put_nowait(("Не расслышал конец фразы. Повтори, пожалуйста.", generation))
                            last_recovery = clock()
                        continue
                    LOG.info("Live STT recognized %d chars", len(phrase))
                    generation += 1
                    if playing and not playing.done():
                        playing.cancel()
                    audio_ready.set()
                    if len(pending) >= 4:
                        output.put_nowait(("Секунду, ещё выполняю предыдущие запросы.", generation))
                        continue
                    task = tasks.create_task(answer(phrase, generation))
                    pending.add(task)
                    task.add_done_callback(pending.discard)
            finally:
                player.cancel()
                if keys:
                    keys.cancel()
                for task in pending:
                    task.cancel()
    except* CallFinished:
        pass
