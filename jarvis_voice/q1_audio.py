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
