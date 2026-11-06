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
    _,err=await proc.communicate(pcm)
    if proc.returncode:
        raise RuntimeError("aplay failed: "+err.decode(errors="replace")[:200])


async def play_stream(device, stream):
    """Start output on the first SpeechKit chunk rather than buffering the full TTS."""
    if not device:
        raise RuntimeError("q1_speaker_device not set")
    proc=await asyncio.create_subprocess_exec(
        "aplay","-q","-D",device,"-f","S16_LE","-r","16000","-c","1",
        stdin=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:
        async for frame in stream:
            proc.stdin.write(frame)
            await proc.stdin.drain()
        proc.stdin.close()
        await proc.wait()
        if proc.returncode:
            raise RuntimeError("aplay failed")
    except Exception:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
