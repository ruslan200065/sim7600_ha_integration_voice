"""Optional UAC/ALSA Q1 input/output, only when ALSA device nodes exist."""

import asyncio
import os
import re

from audio import FRAME, RATE
from processes import cancel_task, start_process, stop_process

# Plain hardware/default PCMs only: ALSA strings can otherwise describe plugins
# such as file:FILE=... that write arbitrary files.
ALSA_DEVICE = re.compile(
    r"(?:default|pulse|(?:plughw|hw|sysdefault|front|dmix|dsnoop):[A-Za-z0-9_,=.-]+)"
)


def audio_route(device, direction):
    """Use a specific Pulse source or sink while keeping ALSA device support."""
    if not device.startswith("pulse:"):
        if not ALSA_DEVICE.fullmatch(device):
            raise ValueError("Unsupported ALSA device name")
        return device, None
    name = device.removeprefix("pulse:")
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", name):
        raise ValueError("Invalid PulseAudio device name")
    variable = "PULSE_SOURCE" if direction == "capture" else "PULSE_SINK"
    return "pulse", {**os.environ, variable: name}


async def capture_pcm(device, seconds=4):
    if not device:
        raise RuntimeError("q1_mic_device not set")
    duration = max(1, min(int(seconds), 15))
    route = audio_route(device, "capture")
    proc = await start_pcm_process(
        route, "capture", seconds=duration, stderr=asyncio.subprocess.PIPE
    )
    try:
        data, err = await asyncio.wait_for(proc.communicate(), duration + 6)
    except TimeoutError as exc:
        await stop_process(proc)
        raise RuntimeError("Q1 microphone capture timeout") from exc
    except BaseException:
        await stop_process(proc)
        raise
    if proc.returncode:
        raise RuntimeError("arecord failed: " + err.decode(errors="replace")[:200])
    return data


async def start_pcm_process(
    route, direction, *, seconds=None, stderr=asyncio.subprocess.DEVNULL
):
    """Start ALSA with the mono S16 format shared by modem, capture, and playback."""
    device, env = route
    capture = direction == "capture"
    command = "arecord" if capture else "aplay"
    duration = ("-d", str(seconds)) if seconds is not None else ()
    return await start_process(
        command,
        "-q",
        "-D",
        device,
        "-t",
        "raw",
        "-f",
        "S16_LE",
        "-r",
        str(RATE),
        "-c",
        "1",
        *duration,
        stdin=None if capture else asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE if capture else None,
        stderr=stderr,
        env=env,
    )


async def play_pcm(device, pcm):
    if not device:
        raise RuntimeError("q1_speaker_device not set")
    route = audio_route(device, "playback")
    proc = await start_pcm_process(route, "playback", stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(pcm), timeout=8)
    except BaseException:
        await stop_process(proc)
        raise
    if proc.returncode:
        raise RuntimeError("aplay failed: " + err.decode(errors="replace")[:200])


async def play_stream(device, stream):
    """Start output on the first SpeechKit chunk rather than buffering the full TTS."""
    if not device:
        raise RuntimeError("q1_speaker_device not set")
    proc = await start_pcm_process(audio_route(device, "playback"), "playback")
    try:
        async for frame in stream:
            proc.stdin.write(frame)
            await asyncio.wait_for(proc.stdin.drain(), 5)
        proc.stdin.close()
        await asyncio.wait_for(proc.wait(), 8)
        if proc.returncode:
            raise RuntimeError("aplay failed")
    finally:
        await stop_process(proc)


async def recognize_microphone_live(speech, device, timeout=12):
    """Stream Q1 ALSA/Pulse PCM to SpeechKit *as it arrives*, not after recording."""
    if not device:
        raise RuntimeError("Configure q1_mic_device first")
    proc = await start_pcm_process(audio_route(device, "capture"), "capture")
    queue = asyncio.Queue(maxsize=30)

    async def producer():
        while True:
            try:
                frame = await proc.stdout.readexactly(FRAME)
            except asyncio.IncompleteReadError:
                return
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(frame)

    reader = asyncio.create_task(producer())
    try:
        return await asyncio.wait_for(speech.recognize_live(queue), timeout=timeout)
    except TimeoutError:
        if proc.returncode not in (None, 0):
            raise RuntimeError("arecord failed: no usable Q1 microphone capture")
        return ""
    finally:
        try:
            await cancel_task(reader)
        finally:
            await stop_process(proc)
