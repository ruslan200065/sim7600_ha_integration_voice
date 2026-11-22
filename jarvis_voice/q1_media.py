"""Sequential URL playback on Q1's separately verified USB speaker.

ffmpeg decodes HTTP(S) audio into 16 kHz signed mono PCM, aplay sends it to
the configured ALSA/Pulse sink. No direct shell use, no arbitrary file URLs.
"""

import asyncio
import logging

from aiohttp import ClientTimeout
from processes import cancel_task, start_process, stop_process
from q1_audio import audio_route, start_pcm_process
from yarl import URL

LOG = logging.getLogger(__name__)
# Audio containers only: no playlists (hls, concat) that could pull other inputs.
FORMAT_WHITELIST = "mp3,ogg,flac,wav,aac,mov,mp4,m4a,3gp,3g2,mj2,matroska,webm"


class Q1Media:
    def __init__(self):
        self.task = None
        self.lock = asyncio.Lock()

    @property
    def playing(self):
        return self.task is not None and not self.task.done()

    def validate(self, url, allowed_hosts):
        try:
            parts = URL(url)
            host = parts.host or ""
            if parts.scheme not in ("http", "https") or not host:
                raise ValueError("Only HTTP(S) audio URLs are allowed")
            if parts.user is not None or parts.password is not None:
                raise ValueError("Credentials in audio URLs are not allowed")
            allowed = {str(x).strip().lower() for x in allowed_hosts}
            host = host.lower()
            if parts.is_default_port():
                permitted = host in allowed
            else:
                # Non-standard ports must be listed explicitly as host:port.
                permitted = f"{host}:{parts.port}" in allowed
            if not permitted:
                raise ValueError("Audio URL host not in media_allowed_hosts")
        except ValueError as exc:
            raise ValueError(f"Invalid media URL: {exc}") from exc
        return parts

    async def play(self, session, url, sink, allowed_hosts):
        self.validate(url, allowed_hosts)
        if not sink:
            raise RuntimeError("Q1 speaker not configured")
        # Resolve the sink before reporting success, so a bad sink fails the API call.
        route = audio_route(sink, "playback")
        async with self.lock:
            await self._stop_current()
            self.task = asyncio.create_task(
                self._play(session, url, route, allowed_hosts)
            )

    async def stop(self):
        async with self.lock:
            await self._stop_current()

    async def _stop_current(self):
        if self.task is None:
            return
        try:
            await cancel_task(self.task)
        finally:
            self.task = None

    async def _download(self, session, url, allowed_hosts, stdin):
        try:
            for _ in range(6):
                url = self.validate(url, allowed_hosts)
                async with session.get(
                    url,
                    allow_redirects=False,
                    timeout=ClientTimeout(total=None, sock_connect=8, sock_read=15),
                ) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        url = response.url.join(URL(response.headers["Location"]))
                        continue
                    response.raise_for_status()
                    async for chunk in response.content.iter_chunked(8192):
                        stdin.write(chunk)
                        await asyncio.wait_for(stdin.drain(), 5)
                    return
            raise ValueError("Too many media redirects")
        finally:
            stdin.close()

    async def _play(self, session, url, route, allowed_hosts):
        source = dest = feed = None
        try:
            source = await start_process(
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-protocol_whitelist",
                "pipe",
                "-format_whitelist",
                FORMAT_WHITELIST,
                "-i",
                "pipe:0",
                "-vn",
                "-f",
                "s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            feed = asyncio.create_task(
                self._download(session, url, allowed_hosts, source.stdin)
            )
            dest = await start_pcm_process(route, "playback")
            while chunk := await asyncio.wait_for(source.stdout.read(8192), 20):
                dest.stdin.write(chunk)
                await asyncio.wait_for(dest.stdin.drain(), 5)
            dest.stdin.close()
            await asyncio.wait_for(dest.wait(), 8)
            await asyncio.wait_for(source.wait(), 8)
            await feed
            if dest.returncode != 0 or source.returncode != 0:
                LOG.warning(
                    "Q1 media playback ended with errors ffmpeg=%s aplay=%s",
                    source.returncode,
                    dest.returncode,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("Q1 media playback failed")
        finally:
            if feed:
                feed.cancel()
                await asyncio.gather(feed, return_exceptions=True)
            processes = [proc for proc in (source, dest) if proc is not None]
            await asyncio.gather(*(stop_process(proc) for proc in processes))
