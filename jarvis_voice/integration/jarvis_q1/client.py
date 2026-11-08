"""Authenticated JARVIS App HTTP client using Home Assistant shared aiohttp session."""
from __future__ import annotations

import json as jsonlib

from aiohttp import ClientError, ClientTimeout, ContentTypeError
from homeassistant.exceptions import HomeAssistantError

from .helpers import format_url_host


class JarvisError(HomeAssistantError):
    """Base error talking to the JARVIS App."""


class JarvisAuthError(JarvisError):
    """The App rejected the API token (HTTP 401/403)."""


class JarvisConnectionError(JarvisError):
    """The App could not be reached."""


class JarvisResponseError(JarvisError):
    """The App answered with an error status or a malformed body."""


class JarvisClient:
    def __init__(self, session, host, port, token):
        self.session = session
        self.url = f"http://{format_url_host(str(host))}:{int(port)}"
        self.headers = {"Authorization": f"Bearer {token}"}

    async def request(self, method, path, json=None, timeout=25, raw=False):
        try:
            async with self.session.request(
                method, self.url + path, headers=self.headers,
                json=json, timeout=ClientTimeout(total=timeout)
            ) as response:
                if response.status in (401, 403):
                    raise JarvisAuthError(f"JARVIS {path} rejected the API token (HTTP {response.status})")
                if response.status >= 400:
                    detail = (await response.text())[:200]
                    raise JarvisResponseError(f"JARVIS {path} HTTP {response.status}: {detail}")
                if raw:
                    return await response.read()
                try:
                    return await response.json()
                except (ContentTypeError, jsonlib.JSONDecodeError, ValueError) as exc:
                    raise JarvisResponseError(f"JARVIS {path} returned invalid JSON: {exc}") from exc
        except JarvisError:
            raise
        except (ClientError, TimeoutError, OSError) as exc:
            raise JarvisConnectionError(f"JARVIS App unavailable: {exc}") from exc

    async def get(self, path, *, timeout=15, raw=False):
        return await self.request("GET", path, timeout=timeout, raw=raw)

    async def post(self, path, data=None, *, timeout=30):
        return await self.request("POST", path, json=data or {}, timeout=timeout)
