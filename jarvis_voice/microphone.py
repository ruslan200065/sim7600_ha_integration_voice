"""Local microphone level and sound activity sampling."""

import asyncio

from audio import rms
from q1_audio import capture_pcm


class MicrophoneMonitor:
    def __init__(self, device, lock, preference):
        self.device = device
        self.lock = lock
        self.preference = preference
        self.enabled = bool(device and preference.exists())
        self.level = None
        self.sampling = False
        self.wake = asyncio.Event()

    def set_enabled(self, enabled):
        if enabled and not self.device:
            raise ValueError("Configure a microphone in the add-on")
        if enabled:
            self.preference.touch(mode=0o600)
        else:
            self.preference.unlink(missing_ok=True)
            self.level = None
        self.enabled = enabled
        self.wake.set()

    def status(self):
        return {
            "configured": bool(self.device),
            "enabled": self.enabled,
            "level": self.level,
            "sound": None if self.level is None else self.level >= 3.0,
        }

    async def _sample(self):
        if self.lock.locked():
            self.level = None
            return
        self.sampling = True
        try:
            async with self.lock:
                pcm = await capture_pcm(self.device, 1)
            self.level = (
                round(100 * rms(pcm) / 32768, 1) if self.enabled and pcm else None
            )
        except (RuntimeError, ValueError, OSError, TimeoutError):
            self.level = None
        finally:
            self.sampling = False

    async def run(self):
        while True:
            self.wake.clear()
            if not self.enabled:
                await self.wake.wait()
                continue
            await self._sample()
            try:
                async with asyncio.timeout(2):
                    await self.wake.wait()
            except TimeoutError:
                pass
