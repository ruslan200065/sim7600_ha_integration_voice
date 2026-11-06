"""Check that Q1 media requests enforce the allowlist after redirects."""

import pathlib
import sys
import unittest
from unittest.mock import AsyncMock, Mock

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from q1_media import Q1Media


class MediaTests(unittest.IsolatedAsyncioTestCase):
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
            good = server.make_url("/redirect").with_query(to="/audio")
            await media._download(session, good, [server.host], stdin)
            stdin.write.assert_called_once_with(b"test audio")
            stdin.close.assert_called_once()

            stdin.reset_mock()
            bad = server.make_url("/redirect").with_query(
                to=str(server.make_url("/audio").with_host("localhost"))
            )
            with self.assertRaisesRegex(ValueError, "host not in media_allowed_hosts"):
                await media._download(session, bad, [server.host], stdin)
            stdin.write.assert_not_called()
            stdin.close.assert_called_once()
