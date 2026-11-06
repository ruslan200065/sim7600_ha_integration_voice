"""Optional UAC/ALSA Q1 input/output, only when ALSA device nodes exist."""
import asyncio
import logging

LOG = logging.getLogger(__name__)


async def capture_pcm(device, seconds=4):
    if not device:
        raise RuntimeError("q1_mic_device not set")
    proc=await asyncio.create_subprocess_exec(
        "arecord","-q","-D",device,"-f","S16_LE","-r","16000","-c","1",
        "-d",str(max(1,min(int(seconds),15))),"-t","raw",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        data,err=await asyncio.wait_for(proc.communicate(),seconds+6)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError("Q1 microphone capture timeout")
    if proc.returncode:
        raise RuntimeError("arecord failed: "+err.decode(errors="replace")[:200])
    return data


async def play_pcm(device, pcm):
    if not device:
        raise RuntimeError("q1_speaker_device not set")
    proc=await asyncio.create_subprocess_exec(
        "aplay","-q","-D",device,"-f","S16_LE","-r","16000","-c","1",
        stdin=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:
        _,err=await asyncio.wait_for(proc.communicate(pcm), timeout=8)
    except (TimeoutError, asyncio.CancelledError):
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.communicate()
        raise
    if proc.returncode:
        raise RuntimeError("aplay failed: "+err.decode(errors="replace")[:200])


async def play_stream(device, stream):
    """Start output on the first SpeechKit chunk rather than buffering the full TTS."""
    if not device:
        raise RuntimeError("q1_speaker_device not set")
    proc=await asyncio.create_subprocess_exec(
        "aplay","-q","-D",device,"-f","S16_LE","-r","16000","-c","1",
        stdin=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
    try:
        async for frame in stream:
            proc.stdin.write(frame)
            await proc.stdin.drain()
        proc.stdin.close()
        await proc.wait()
        if proc.returncode:
            raise RuntimeError("aplay failed")
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()


async def recognize_microphone_live(speech, device, timeout=12):
    """Stream Q1 ALSA/Pulse PCM to SpeechKit *as it arrives*, not after recording."""
    if not device:
        raise RuntimeError("Configure q1_mic_device first")
    proc=await asyncio.create_subprocess_exec(
        "arecord","-q","-D",device,"-f","S16_LE","-r","16000","-c","1",
        "-t","raw", stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    queue=asyncio.Queue(maxsize=30)

    async def producer():
        while True:
            try:
                frame=await proc.stdout.readexactly(3200)
            except asyncio.IncompleteReadError:
                return
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(frame)

    reader=asyncio.create_task(producer())
    try:
        return await asyncio.wait_for(speech.recognize_live(queue), timeout=timeout)
    except asyncio.TimeoutError:
        if proc.returncode not in (None,0):
            raise RuntimeError("arecord failed: no usable Q1 microphone capture")
        return ""
    finally:
        reader.cancel()
        try:
            await reader
        except asyncio.CancelledError:
            pass
        if proc.returncode is None:
            proc.kill()
        await proc.communicate()
