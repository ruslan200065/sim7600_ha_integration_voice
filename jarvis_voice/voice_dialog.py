"""Coordinate recognition and playback for a single modem call."""

import asyncio
import logging
import re
from difflib import SequenceMatcher

LOG = logging.getLogger(__name__)
STOP_WORDS = {"стоп", "подожди", "секунду", "погоди"}
FAREWELLS = {"пока", "до свидания", "заверши звонок"}


def words(text):
    return re.findall(r"[а-яa-z0-9]+", text.lower().replace("ё", "е"))


def echoes_playback(phrase, outputs):
    count = len(phrase)
    if not count:
        return False
    heard = " ".join(phrase)
    for output in outputs:
        for index in range(len(output) - count + 1):
            candidate = output[index : index + count]
            if phrase == candidate:
                return True
            if count >= 2 and SequenceMatcher(None, heard, " ".join(candidate)).ratio() >= 0.86:
                return True
    return False


async def run_dialog(speech, reply_to, source, play, hangup, prompt, *, background_tools=False):
    """Listen throughout a reply and interrupt once the caller is recognized."""
    clock = asyncio.get_running_loop().time
    last_spoken = []
    echo_until = 0.0

    async def speak(text):
        nonlocal last_spoken, echo_until
        last_spoken = words(text)
        echo_until = float("inf")
        try:
            await play(text)
        finally:
            # Remote speaker echo and delayed transcripts can outlast playback.
            echo_until = clock() + 2.0

    output = asyncio.Queue()
    audio_ready = asyncio.Event()
    audio_ready.set()
    playing = None
    generation = 0
    pending = set()

    async def playback():
        nonlocal playing
        while True:
            text, turn, background = await output.get()
            await audio_ready.wait()
            if background or turn == generation:
                playing = asyncio.create_task(speak(text))
                try:
                    await asyncio.gather(playing, return_exceptions=True)
                finally:
                    playing = None

    async def answer(phrase, turn):
        has_tool = False

        def progress(text):
            nonlocal has_tool
            has_tool = True
            output.put_nowait((text, turn, False))

        if background_tools:
            text = await reply_to(phrase, on_progress=progress)
        else:
            text = await reply_to(phrase)
        LOG.info("LLM answer length=%d", len(text))
        output.put_nowait((text, turn, has_tool))

    output.put_nowait((prompt, generation, False))
    async with asyncio.TaskGroup() as tasks:
        player = tasks.create_task(playback())
        try:
            while True:
                heard_playback = []

                def remember_output():
                    if clock() <= echo_until and last_spoken and last_spoken not in heard_playback:
                        heard_playback.append(last_spoken)

                def interrupt(partial):
                    remember_output()
                    tokens = words(partial)
                    confirmed = len(tokens) >= 2 or (len(tokens) == 1 and tokens[0] in STOP_WORDS)
                    if confirmed and not echoes_playback(tokens, heard_playback):
                        audio_ready.clear()
                        if playing and not playing.done():
                            playing.cancel()

                phrase = await speech.recognize_live(
                    source, threshold=450, on_voice_start=remember_output, on_partial=interrupt
                )
                remember_output()
                tokens = words(phrase)
                if not tokens or echoes_playback(tokens, heard_playback):
                    audio_ready.set()
                    continue
                LOG.info("Live STT recognized %d chars", len(phrase))
                generation += 1
                if playing and not playing.done():
                    playing.cancel()
                audio_ready.set()
                normalized = " ".join(tokens)
                if normalized in STOP_WORDS:
                    continue
                if normalized in FAREWELLS:
                    player.cancel()
                    await asyncio.gather(player, return_exceptions=True)
                    await speak("До связи.")
                    await hangup()
                    return
                if len(pending) >= 4:
                    output.put_nowait(("Секунду, ещё выполняю предыдущие запросы.", generation, False))
                    continue
                task = tasks.create_task(answer(phrase, generation))
                pending.add(task)
                task.add_done_callback(pending.discard)
        finally:
            player.cancel()
            for task in pending:
                task.cancel()
