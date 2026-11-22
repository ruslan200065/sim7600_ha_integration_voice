"""Authenticated JARVIS App HTTP client using Home Assistant shared aiohttp session."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_HOST, CONF_PORT, CONF_TOKEN
from .helpers import format_url_host

_MAX_SNAPSHOT_BYTES = 8_000_000


class JarvisError(HomeAssistantError):
    """Base error talking to the JARVIS App."""


class JarvisAuthError(JarvisError):
    """The App rejected the API token (HTTP 401/403)."""


class JarvisConnectionError(JarvisError):
    """The App could not be reached."""


class JarvisResponseError(JarvisError):
    """The App answered with an error status or a malformed body."""


class JarvisClient:
    def __init__(self, session: ClientSession, host: str, port: int, token: str):
        self.session = session
        self.url = f"http://{format_url_host(str(host))}:{int(port)}"
        self.headers = {"Authorization": f"Bearer {token}"}

    @classmethod
    def from_entry(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> JarvisClient:
        return cls(
            async_get_clientsession(hass),
            data[CONF_HOST],
            data[CONF_PORT],
            data[CONF_TOKEN],
        )

    async def request(self, method, path, json=None, timeout=25, raw=False):
        try:
            async with self.session.request(
                method,
                self.url + path,
                headers=self.headers,
                json=json,
                timeout=ClientTimeout(total=timeout),
                allow_redirects=False,
            ) as response:
                if response.status in (401, 403):
                    raise JarvisAuthError(
                        f"JARVIS {path} rejected the API token (HTTP {response.status})"
                    )
                if not 200 <= response.status < 300:
                    detail = (await response.content.read(200)).decode(
                        "utf-8", errors="replace"
                    )
                    raise JarvisResponseError(
                        f"JARVIS {path} HTTP {response.status}: {detail}"
                    )
                if raw:
                    snapshot = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        snapshot.extend(chunk)
                        if len(snapshot) > _MAX_SNAPSHOT_BYTES:
                            raise JarvisResponseError(
                                f"JARVIS {path} returned an oversized image"
                            )
                    return bytes(snapshot)
                try:
                    return await response.json()
                except (ClientError, ValueError) as exc:
                    raise JarvisResponseError(
                        f"JARVIS {path} returned invalid JSON: {exc}"
                    ) from exc
        except JarvisError:
            raise
        except (ClientError, TimeoutError, OSError) as exc:
            raise JarvisConnectionError(f"JARVIS App unavailable: {exc}") from exc

    async def get(self, path, *, timeout=15, raw=False):
        return await self.request("GET", path, timeout=timeout, raw=raw)

    async def post(self, path, data=None, *, timeout=30):
        return await self.request("POST", path, json=data or {}, timeout=timeout)
