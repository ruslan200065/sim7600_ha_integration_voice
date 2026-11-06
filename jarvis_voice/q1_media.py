"""Sequential URL playback on Q1's separately verified USB speaker.

ffmpeg decodes HTTP(S) audio into 16 kHz signed mono PCM, aplay sends it to
the configured ALSA/Pulse sink. No direct shell use, no arbitrary file URLs.
"""
import asyncio
import logging
from aiohttp import ClientTimeout
from yarl import URL

LOG = logging.getLogger(__name__)

class Q1Media:
    def __init__(self):
        self.task = None
        self.playing = False

    def validate(self, url, allowed_hosts):
        try:
            parts = URL(url)
            host = parts.host or ""
            if parts.scheme not in ("http", "https") or not host:
                raise ValueError("Only HTTP(S) audio URLs are allowed")
            if parts.user is not None or host.lower() not in {x.lower() for x in allowed_hosts}:
                raise ValueError("Audio URL host not in media_allowed_hosts")
        except ValueError as exc:
            raise ValueError(f"Invalid media URL: {exc}") from exc
        return parts

    async def play(self, session, url, sink, allowed_hosts):
        self.validate(url, allowed_hosts)
        if not sink:
            raise RuntimeError("Q1 speaker not configured")
        await self.stop()
        self.playing = True
        self.task = asyncio.create_task(self._play(session, url, sink, allowed_hosts))

    async def stop(self):
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None
        self.playing = False

    async def _download(self, session, url, allowed_hosts, stdin):
        try:
            for _ in range(6):
                url = self.validate(url, allowed_hosts)
                async with session.get(
                    url, allow_redirects=False, timeout=ClientTimeout(total=None, sock_connect=8, sock_read=15)
                ) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        url = response.url.join(URL(response.headers["Location"]))
                        continue
                    response.raise_for_status()
                    async for chunk in response.content.iter_chunked(8192):
                        stdin.write(chunk)
                        await stdin.drain()
                    return
            raise ValueError("Too many media redirects")
        finally:
            stdin.close()

    async def _play(self, session, url, sink, allowed_hosts):
        source = dest = feed = None
        try:
            source = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-protocol_whitelist", "pipe",
                "-i", "pipe:0", "-vn", "-f", "s16le", "-ar", "16000", "-ac", "1", "pipe:1",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            feed = asyncio.create_task(self._download(session, url, allowed_hosts, source.stdin))
            dest = await asyncio.create_subprocess_exec(
                "aplay","-q","-D",sink,"-f","S16_LE","-r","16000","-c","1",
                stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            while chunk := await source.stdout.read(8192):
                dest.stdin.write(chunk)
                await dest.stdin.drain()
            dest.stdin.close()
            await dest.wait()
            await source.wait()
            await feed
            if dest.returncode != 0 or source.returncode != 0:
                LOG.warning("Q1 media playback ended with errors ffmpeg=%s aplay=%s",source.returncode,dest.returncode)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("Q1 media playback failed")
        finally:
            if feed:
                feed.cancel()
                await asyncio.gather(feed, return_exceptions=True)
            for proc in (source,dest):
                if proc and proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    await proc.communicate()
            self.playing = False
