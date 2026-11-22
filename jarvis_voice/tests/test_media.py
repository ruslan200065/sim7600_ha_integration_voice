"""Check that Q1 media requests enforce the allowlist after redirects."""

import asyncio
import pathlib
import sys
import unittest
from unittest.mock import AsyncMock, Mock

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from q1_media import Q1Media


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_requests_never_overlap_playback(self):
        media = Q1Media()
        first_started = asyncio.Event()
        stop_started = asyncio.Event()
        may_stop = asyncio.Event()
        playing = set()

        async def play(_session, url, _route, _allowed_hosts):
            self.assertFalse(playing)
            playing.add(url)
            try:
                if url.endswith("/one"):
                    first_started.set()
                await asyncio.Future()
            finally:
                if url.endswith("/one"):
                    stop_started.set()
                    await may_stop.wait()
                playing.remove(url)

        media._play = play
        allowed = ["homeassistant"]
        await media.play(None, "http://homeassistant/one", "default", allowed)
        await asyncio.wait_for(first_started.wait(), 1)
        second = asyncio.create_task(
            media.play(None, "http://homeassistant/two", "default", allowed)
        )
        await asyncio.wait_for(stop_started.wait(), 1)
        third = asyncio.create_task(
            media.play(None, "http://homeassistant/three", "default", allowed)
        )
        may_stop.set()
        await asyncio.wait_for(asyncio.gather(second, third), 1)
        await asyncio.sleep(0)
        self.assertEqual(playing, {"http://homeassistant/three"})
        self.assertTrue(media.playing)
        await media.stop()
        self.assertFalse(playing)
        self.assertFalse(media.playing)

    async def test_redirects_are_checked_before_fetching_the_next_url(self):
        async def redirect(request):
            raise web.HTTPFound(request.query["to"])

        async def audio(_request):
            return web.Response(body=b"test audio")

        app = web.Application()
        app.router.add_get("/redirect", redirect)
        app.router.add_get("/audio", audio)
        async with TestServer(app) as server, ClientSession() as session:
            media = Q1Media()
            stdin = Mock(drain=AsyncMock())
            allowed = [f"{server.host}:{server.port}"]
            good = server.make_url("/redirect").with_query(to="/audio")
            await media._download(session, good, allowed, stdin)
            stdin.write.assert_called_once_with(b"test audio")
            stdin.close.assert_called_once()

            stdin.reset_mock()
            bad = server.make_url("/redirect").with_query(
                to=str(server.make_url("/audio").with_host("localhost"))
            )
            with self.assertRaisesRegex(ValueError, "host not in media_allowed_hosts"):
                await media._download(session, bad, allowed, stdin)
            stdin.write.assert_not_called()
            stdin.close.assert_called_once()

    def test_validate_rejects_credentials_and_unlisted_ports(self):
        media = Q1Media()
        media.validate("http://homeassistant/a.mp3", ["homeassistant"])
        media.validate("https://homeassistant/a.mp3", ["homeassistant"])
        media.validate("http://homeassistant:8123/a.mp3", ["homeassistant:8123"])
        for url in (
            "http://homeassistant:22/a.mp3",
            "http://user@homeassistant/a.mp3",
            "http://:pw@homeassistant/a.mp3",
            "file:///etc/passwd",
            "http://supervisor/a.mp3",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                media.validate(url, ["homeassistant"])

    def test_alsa_device_names_are_restricted(self):
        from q1_audio import audio_route

        for device in ("default", "hw:1,0", "plughw:CARD=Q1,DEV=0", "pulse"):
            self.assertEqual(audio_route(device, "playback"), (device, None))
        self.assertEqual(audio_route("pulse:q1_sink", "playback")[0], "pulse")
        for device in ("file:FILE=/data/x", "hw:1;rm", "plug:{type file}"):
            with self.subTest(device=device), self.assertRaises(ValueError):
                audio_route(device, "playback")
