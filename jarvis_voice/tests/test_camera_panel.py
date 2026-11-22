"""On-demand camera mode and stream lifecycle regression tests."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from camera_panel import (
    CameraPanel,
    default_mode,
    parse_controls,
    parse_modes,
    preview_args,
    read_frame,
)

MODES = """
    [0]: 'YUYV' (YUYV 4:2:2)
        Size: Discrete 640x480
            Interval: Discrete 0.033s (30.000 fps)
    [1]: 'MJPG' (Motion-JPEG, compressed)
        Size: Discrete 640x480
            Interval: Discrete 0.033s (30.000 fps)
            Interval: Discrete 0.067s (15.000 fps)
        Size: Discrete 1280x720
            Interval: Discrete 0.033s (30.000 fps)
"""
CONTROLS = """
brightness 0x00980900 (int) : min=-64 max=64 step=2 default=0 value=20
white_balance_automatic 0x0098090c (bool) : default=1 value=1
exposure_auto 0x009a0901 (menu) : min=0 max=3 default=3 value=3
    1: Manual Mode
    3: Aperture Priority Mode
exposure_absolute 0x009a0902 (int) : min=3 max=2047 step=1 default=250 value=250 flags=inactive
pixel_rate 0x009f0902 (int64) : min=0 max=100 step=1 default=0 value=50 flags=read-only
"""


def test_modes_prefer_camera_jpeg():
    modes = parse_modes(MODES)
    assert len(modes) == 4
    mode = default_mode(modes)
    assert mode == {"format": "MJPG", "width": 640, "height": 480, "fps": 15.0}
    args = preview_args("/dev/video0", mode)
    assert args[args.index("-c:v") + 1] == "copy"
    assert "copy" not in preview_args("/dev/video0", modes[0])


def test_driver_controls_and_flags():
    rows = {row["id"]: row for row in parse_controls(CONTROLS)}
    assert rows["brightness"]["min"] == -64
    assert rows["brightness"]["step"] == 2
    assert rows["white_balance_automatic"]["max"] == 1
    assert rows["exposure_auto"]["choices"][0]["value"] == 1
    assert rows["exposure_absolute"]["inactive"]
    assert "pixel_rate" not in rows


def camera_panel(tmp_path):
    return CameraPanel(
        SimpleNamespace(
            camera_settings_path=tmp_path / "camera_settings.json",
            o={"q1_video_device": "/dev/video0"},
            camera_lock=asyncio.Lock(),
            camera_frame=None,
        )
    )


async def test_capture_exists_only_while_viewing(monkeypatch, tmp_path):
    panel = camera_panel(tmp_path)
    panel.modes = AsyncMock(return_value=parse_modes(MODES))
    panel.controls = AsyncMock(return_value=parse_controls(CONTROLS))
    # Exercise the real subprocess lifecycle and multipart reader with a camera
    # substitute that writes valid boundaries and a constant JPEG payload.
    spawn = asyncio.create_subprocess_exec
    spawned = []
    child = r"""
import sys, time
while True:
    sys.stdout.buffer.write(b'--jarvis\r\nContent-type: image/jpeg\r\nContent-length: 4\r\n\r\n\xff\xd8\xff\xd9\r\n')
    sys.stdout.buffer.flush()
    time.sleep(.03)
"""

    async def start_ffmpeg(*args, **kwargs):
        process = await spawn(sys.executable, "-u", "-c", child, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr("camera_panel.asyncio.create_subprocess_exec", start_ffmpeg)
    app = web.Application()
    panel.register(app)
    app.router.add_get("/q1/camera/live", panel.mjpeg)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/camera")).status == 200
        assert not spawned
        async with client.ws_connect("/camera/preview") as viewer:
            assert await viewer.receive_bytes(timeout=3) == bytes.fromhex("ffd8ffd9")
            assert panel.last_frame[1] == bytes.fromhex("ffd8ffd9")
            assert len(spawned) == 1
            assert spawned[0].returncode is None
            stream = await client.get("/q1/camera/live")
            assert stream.status == 200
            assert await read_frame(stream.content) == bytes.fromhex("ffd8ffd9")
            assert len(spawned) == 1
        assert await read_frame(stream.content) == bytes.fromhex("ffd8ffd9")
        assert spawned[0].returncode is None
        stream.close()
        await asyncio.wait_for(panel.released.wait(), 3)
        assert panel.last_frame is None
        assert spawned[0].returncode is not None


async def test_unsupported_mode_and_control_rejected(monkeypatch, tmp_path):
    panel = camera_panel(tmp_path)
    modes = parse_modes(MODES)
    panel.modes = AsyncMock(return_value=modes)
    panel.controls = AsyncMock(return_value=parse_controls(CONTROLS))
    run = AsyncMock()
    monkeypatch.setattr("camera_panel._run", run)
    app = web.Application()
    panel.register(app)
    async with TestClient(TestServer(app)) as client:
        assert (
            await client.patch("/camera", json={"mode": {"fps": 120}})
        ).status == 409
        assert (
            await client.patch("/camera", json={"control": "brightness", "value": 19})
        ).status == 409
        assert (
            await client.patch("/camera", json={"control": "exposure_auto", "value": 2})
        ).status == 409
        run.assert_not_awaited()
        assert (await client.patch("/camera", json={"mode": modes[-1]})).status == 200
        assert (
            await client.patch("/camera", json={"control": "brightness", "value": 24})
        ).status == 200
        run.assert_awaited_once_with(
            "v4l2-ctl", "-d", "/dev/video0", "-c", "brightness=24"
        )
        assert panel.read_settings("/dev/video0")["mode"] == modes[-1]


async def test_shutdown_releases_capture_and_rejects_new_subscribers(tmp_path):
    panel = camera_panel(tmp_path)
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def capture(_device):
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    panel.capture = capture
    async with panel.subscribe() as queue:
        await asyncio.wait_for(started.wait(), 1)
        await panel.shutdown(None)
        assert stopped.is_set()
        assert panel.capture_task is None
        assert panel.released.is_set()
        with pytest.raises(RuntimeError, match="остановлен"):
            await panel.next_frame(queue)
        with pytest.raises(RuntimeError, match="остановлен"):
            async with panel.subscribe():
                pytest.fail("Shutdown allowed a new capture")
