"""Authenticated JARVIS App HTTP client using Home Assistant shared aiohttp session."""
from aiohttp import ClientError, ClientTimeout
from homeassistant.exceptions import HomeAssistantError


class JarvisClient:
    def __init__(self, session, host, port, token):
        self.session = session
        self.url = f"http://{host}:{port}"
        self.headers = {"Authorization": f"Bearer {token}"}

    async def request(self, method, path, json=None, timeout=25, raw=False):
        try:
            async with self.session.request(
                method, self.url + path, headers=self.headers,
                json=json, timeout=ClientTimeout(total=timeout)
            ) as response:
                if response.status >= 400:
                    detail = (await response.text())[:200]
                    raise HomeAssistantError(f"JARVIS {path} HTTP {response.status}: {detail}")
                if raw:
                    return await response.read()
                return await response.json()
        except (ClientError, TimeoutError) as exc:
            raise HomeAssistantError(f"JARVIS App unavailable: {exc}") from exc

    async def get(self, path, *, timeout=15, raw=False):
        return await self.request("GET",path,timeout=timeout,raw=raw)

    async def post(self, path, data=None, *, timeout=30):
        return await self.request("POST",path,json=data or {},timeout=timeout)
