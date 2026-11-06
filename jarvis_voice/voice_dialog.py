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


async def run_dialog(speech, reply_to, source, play, hangup, prompt):
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

    async def answer(phrase):
        text = await reply_to(phrase)
        LOG.info("LLM answer length=%d", len(text))
        await speak(text)

    async with asyncio.TaskGroup() as tasks:
        reply = tasks.create_task(speak(prompt))
        while True:
            playback = []

            def remember_output():
                if clock() <= echo_until and last_spoken and last_spoken not in playback:
                    playback.append(last_spoken)

            def interrupt(partial):
                remember_output()
                tokens = words(partial)
                confirmed = len(tokens) >= 2 or (len(tokens) == 1 and tokens[0] in STOP_WORDS)
                if (
                    confirmed and not reply.done() and not reply.cancelling()
                    and not echoes_playback(tokens, playback)
                ):
                    LOG.info("Caller interrupted JARVIS playback")
                    reply.cancel()

            phrase = await speech.recognize_live(
                source, threshold=450, on_voice_start=remember_output, on_partial=interrupt
            )
            remember_output()
            tokens = words(phrase)
            if not tokens or echoes_playback(tokens, playback):
                continue
            LOG.info("Live STT recognized %d chars", len(phrase))
            if not reply.done():
                if not reply.cancelling():
                    reply.cancel()
                await asyncio.gather(reply, return_exceptions=True)
            normalized = " ".join(tokens)
            if normalized in STOP_WORDS:
                continue
            if normalized in FAREWELLS:
                await speak("До связи.")
                await hangup()
                return
            reply = tasks.create_task(answer(phrase))
