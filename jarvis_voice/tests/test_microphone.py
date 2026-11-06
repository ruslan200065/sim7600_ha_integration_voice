import asyncio
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from microphone import MicrophoneMonitor


class MicrophoneTests(unittest.IsolatedAsyncioTestCase):
    async def test_monitor_is_opt_in_and_reports_sound_without_storing_audio(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "monitor"
            monitor = MicrophoneMonitor("pulse", asyncio.Lock(), path)
            pcm = struct.pack("<h", 4096) * 16000
            with patch("microphone.capture_pcm", AsyncMock(return_value=pcm)) as capture:
                task = asyncio.create_task(monitor.run())
                try:
                    await asyncio.sleep(0)
                    capture.assert_not_awaited()
                    monitor.set_enabled(True)
                    async with asyncio.timeout(1):
                        while monitor.level is None:
                            await asyncio.sleep(0)
                    self.assertEqual(monitor.status(), {
                        "configured": True, "enabled": True, "level": 12.5, "sound": True,
                    })
                    self.assertTrue(MicrophoneMonitor("pulse", asyncio.Lock(), path).enabled)
                    monitor.set_enabled(False)
                    self.assertIsNone(monitor.status()["sound"])
                    self.assertFalse(path.exists())
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
