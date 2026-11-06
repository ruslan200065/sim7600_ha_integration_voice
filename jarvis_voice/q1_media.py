"""Sequential URL playback on Q1's separately verified USB speaker.

ffmpeg decodes HTTP(S) audio into 16 kHz signed mono PCM, aplay sends it to
the configured ALSA/Pulse sink. No direct shell use, no arbitrary file URLs.
"""
import asyncio
from urllib.parse import urlsplit
import logging

LOG = logging.getLogger(__name__)

class Q1Media:
    def __init__(self):
        self.task = None
        self.playing = False

    def validate(self, url, allowed_hosts):
        try:
            parts = urlsplit(url)
            host = parts.hostname or ""
            if parts.scheme not in ("http", "https") or not host:
                raise ValueError("Only HTTP(S) audio URLs are allowed")
            if parts.username or parts.password or host.lower() not in {x.lower() for x in allowed_hosts}:
                raise ValueError("Audio URL host not in media_allowed_hosts")
        except ValueError as exc:
            raise ValueError(f"Invalid media URL: {exc}") from exc
        return url

    async def play(self, url, sink, allowed_hosts):
        self.validate(url, allowed_hosts)
        if not sink:
            raise RuntimeError("Q1 speaker not configured")
        await self.stop()
        self.task = asyncio.create_task(self._play(url, sink))

    async def stop(self):
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None
        self.playing = False

    async def _play(self, url, sink):
        source = dest = None
        self.playing = True
        try:
            source = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
                "-i", url, "-vn", "-f", "s16le", "-ar", "16000", "-ac", "1", "pipe:1",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            dest = await asyncio.create_subprocess_exec(
                "aplay","-q","-D",sink,"-f","S16_LE","-r","16000","-c","1",
                stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            while chunk := await source.stdout.read(8192):
                dest.stdin.write(chunk)
                await dest.stdin.drain()
            dest.stdin.close()
            await dest.wait()
            await source.wait()
            if dest.returncode != 0 or source.returncode != 0:
                LOG.warning("Q1 media playback ended with errors ffmpeg=%s aplay=%s",source.returncode,dest.returncode)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("Q1 media playback failed")
        finally:
            for proc in (source,dest):
                if proc and proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            self.playing = False
