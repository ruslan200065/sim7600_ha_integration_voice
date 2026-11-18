"""On-demand UVC preview, camera mode discovery and controls."""
import asyncio
from contextlib import suppress
from fractions import Fraction
import json
from pathlib import Path
import re

from aiohttp import web

from q1_hardware import _run, camera_device

# Prefer the camera's encoded JPEG to avoid transcoding on the host.
FORMATS = {"MJPG": "mjpeg", "YUYV": "yuyv422", "NV12": "nv12", "H264": "h264"}
MAX_FRAME = 4_000_000
CONTROL = re.compile(r"^\s*([a-z][a-z0-9_]+)\s+0x[0-9a-f]+\s+\((\w+)\)\s*:\s*(.*)$")
CONTROL_VALUE = re.compile(r"(min|max|step|default|value)=(-?\d+)")
BOUNDARY = b"--jarvis\r\n"


def parse_modes(text):
    """Read discrete format/size/frame-interval combinations from V4L2."""
    modes = []
    pixel_format = size = None
    for line in text.splitlines():
        if match := re.search(r"^\s*\[\d+\]: '(\w+)'", line):
            pixel_format = match[1]
            size = None
        elif match := re.search(r"Size: Discrete (\d+)x(\d+)", line):
            size = tuple(map(int, match.groups()))
        elif match := re.search(r"Interval: Discrete .* \(([\d.]+) fps\)", line):
            if not size or pixel_format not in FORMATS:
                continue
            width, height = size
            fps = float(match[1])
            if not (0 < width <= 3840 and 0 < height <= 2160 and 0 < fps <= 60):
                continue
            # Bound the workload of devices that require software JPEG encoding.
            if pixel_format != "MJPG" and (width * height > 1920 * 1080 or fps > 30):
                continue
            mode = {"format": pixel_format, "width": width, "height": height, "fps": fps}
            if mode not in modes:
                modes.append(mode)
    return modes


def default_mode(modes):
    return min(modes, key=lambda mode: (
        mode["format"] != "MJPG",
        abs(mode["width"] - 640) + abs(mode["height"] - 480),
        abs(mode["fps"] - 15),
    )) if modes else None


def parse_controls(text):
    """Return scalar writable controls and the menu values supported by the driver."""
    controls = []
    current = None
    for line in text.splitlines():
        if match := CONTROL.match(line):
            name, kind, details = match.groups()
            current = None
            flags = details.partition("flags=")[2]
            if kind not in {"int", "bool", "menu", "intmenu"} or any(
                flag in flags for flag in ("read-only", "write-only", "disabled", "volatile")
            ):
                continue
            values = {key: int(value) for key, value in CONTROL_VALUE.findall(details)}
            if kind == "bool":
                values.update(min=0, max=1, step=1)
            if not {"min", "max", "value"} <= values.keys():
                continue
            current = {
                "id": name, "type": kind, **values, "choices": [],
                "inactive": "inactive" in flags or "grabbed" in flags,
            }
            controls.append(current)
        elif current and current["type"] in {"menu", "intmenu"}:
            if match := re.match(r"^\s*(-?\d+):\s*(.+)$", line):
                current["choices"].append({"value": int(match[1]), "label": match[2]})
    return controls


def preview_args(device, mode):
    fps = Fraction(str(mode["fps"]))
    cmd = (
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-f", "v4l2", "-input_format", FORMATS[mode["format"]],
        "-video_size", f'{mode["width"]}x{mode["height"]}',
        "-framerate", str(fps), "-i", device, "-an",
    )
    codec = ("-c:v", "copy") if mode["format"] == "MJPG" else (
        "-c:v", "mjpeg", "-threads", "1", "-q:v", "5",
    )
    return (*cmd, *codec, "-flush_packets", "1", "-f", "mpjpeg", "-boundary_tag", "jarvis", "pipe:1")


async def read_frame(stdout):
    """Read FFmpeg's bounded multipart JPEG frames without scanning JPEG bytes."""
    while (line := await stdout.readline()) == b"\r\n":
        pass
    if line != BOUNDARY:
        raise RuntimeError("Камера не передала кадр.")
    size = None
    for _ in range(8):
        line = await stdout.readline()
        if line.lower().startswith(b"content-length:"):
            size = int(line.split(b":", 1)[1])
        if line == b"\r\n":
            if size is None or not 0 < size <= MAX_FRAME:
                break
            return await stdout.readexactly(size)
    raise RuntimeError("Камера передала некорректный кадр.")


class CameraPanel:
    def __init__(self, service):
        self.service = service
        self.path = Path(service.camera_settings_path)
        self.device = None
        self.viewer = None
        self.released = asyncio.Event()
        self.released.set()
        self.last_frame = None
        self.settings_lock = asyncio.Lock()

    def configured_device(self):
        value = self.service.o.get("q1_video_device", "")
        if not value:
            raise ValueError("Выберите видеоустройство в разделе «Оборудование» и примените настройки.")
        return camera_device(value)

    async def modes(self, device):
        text = await _run("v4l2-ctl", "-d", device, "--list-formats-ext")
        return parse_modes(text.decode(errors="replace"))

    async def controls(self, device):
        text = await _run("v4l2-ctl", "-d", device, "--list-ctrls-menus")
        return parse_controls(text.decode(errors="replace"))

    def device_key(self, device):
        return self.service.o.get("q1_video_device") or device

    def read_settings(self, device):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) and data.get("device") == self.device_key(device) else {}

    def write_settings(self, device, settings):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"device": self.device_key(device), **settings}), encoding="utf-8")
        temporary.replace(self.path)

    async def state(self, _request):
        try:
            device = self.configured_device()
            modes, controls, saved = await asyncio.gather(
                self.modes(device), self.controls(device),
                asyncio.to_thread(self.read_settings, device),
            )
            return web.json_response({
                "device": device, "modes": modes,
                "mode": saved.get("mode") if saved.get("mode") in modes else default_mode(modes),
                "controls": controls,
            })
        except (ValueError, RuntimeError, OSError) as exc:
            raise web.HTTPConflict(text=str(exc)[:250]) from exc

    async def save(self, request):
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("Неверные настройки камеры.")
            device = self.configured_device()
            async with self.settings_lock:
                settings = await asyncio.to_thread(self.read_settings, device)
                if set(data) == {"mode"}:
                    mode = next((item for item in await self.modes(device) if item == data["mode"]), None)
                    if mode is None:
                        raise ValueError("Выберите поддерживаемый режим камеры.")
                    settings["mode"] = mode
                elif set(data) == {"control", "value"}:
                    control = next(
                        (row for row in await self.controls(device) if row["id"] == data["control"]),
                        None,
                    )
                    value = data["value"]
                    if not control or control["inactive"] or type(value) is not int:
                        raise ValueError("Эта настройка камеры сейчас недоступна.")
                    allowed = [choice["value"] for choice in control["choices"]]
                    step = max(1, control.get("step", 1))
                    if (not control["min"] <= value <= control["max"]
                            or (value - control["min"]) % step
                            or (allowed and value not in allowed)):
                        raise ValueError("Значение вне диапазона камеры.")
                    await _run("v4l2-ctl", "-d", device, "-c", f'{control["id"]}={value}')
                    settings.setdefault("controls", {})[control["id"]] = value
                    self.service.camera_frame = None
                else:
                    raise ValueError("Неверные настройки камеры.")
                await asyncio.to_thread(self.write_settings, device, settings)
            return web.json_response({"ok": True})
        except (ValueError, RuntimeError, OSError) as exc:
            raise web.HTTPConflict(text=str(exc)[:250]) from exc

    async def restore_controls(self, device, saved):
        if not saved:
            return
        # Driver flags can change after switching an automatic control off.
        # Apply remembered auto/mode controls before manual values.
        for name in sorted(saved, key=lambda key: ("auto" not in key, key)):
            value = saved[name]
            if re.fullmatch(r"[a-z][a-z0-9_]+", name) and type(value) is int:
                # Driver may reject values after a firmware or device change.
                with suppress(RuntimeError):
                    await _run("v4l2-ctl", "-d", device, "-c", f"{name}={value}", timeout=2)

    async def preview(self, request):
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            raise web.HTTPForbidden()
        try:
            device = self.configured_device()
        except ValueError as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        if self.device is not None:
            try:
                await asyncio.wait_for(self.released.wait(), 1)
            except TimeoutError as exc:
                raise web.HTTPConflict(text="Предпросмотр уже открыт.") from exc
            if self.device is not None:
                raise web.HTTPConflict(text="Предпросмотр уже открыт.")
        # Reserve before the first await, including capability probing.
        self.device = device
        self.released.clear()
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=1024)
        self.viewer = ws
        proc = receiver = None
        ready = asyncio.Event()
        ready.set()
        try:
            await ws.prepare(request)
            receiver = asyncio.create_task(self.wait_for_viewer(ws, ready))
            async with self.service.camera_lock, self.settings_lock:
                modes = await self.modes(device)
                saved = await asyncio.to_thread(self.read_settings, device)
                mode = saved.get("mode")
                if mode not in modes:
                    mode = default_mode(modes)
                if mode is None:
                    raise RuntimeError("Камера не сообщила поддерживаемый режим видео.")
                if receiver.done():
                    return ws
                await self.restore_controls(device, saved.get("controls", {}))
                if receiver.done():
                    return ws
                proc = await asyncio.create_subprocess_exec(
                    *preview_args(device, mode), stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            producer = asyncio.create_task(self.stream_frames(ws, proc.stdout, ready))
            try:
                done, _ = await asyncio.wait({producer, receiver}, return_when=asyncio.FIRST_COMPLETED)
                if producer in done:
                    await producer
            finally:
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)
        except (ConnectionError, asyncio.IncompleteReadError, TimeoutError, ValueError, RuntimeError, OSError) as exc:
            if ws.prepared and not ws.closed:
                with suppress(ConnectionError, RuntimeError):
                    message = "Камера перестала передавать кадры." if isinstance(exc, TimeoutError) else str(exc)
                    await asyncio.wait_for(ws.send_json({"error": message[:250]}), 1)
        finally:
            if receiver is not None:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
            if proc is not None and proc.returncode is None:
                with suppress(ProcessLookupError):
                    proc.kill()
                # Drain the pipe before wait so a full buffer cannot hold the process open.
                await proc.communicate()
            self.last_frame = None
            self.device = None
            self.viewer = None
            self.released.set()
            if ws.prepared:
                await ws.close()
        return ws

    @staticmethod
    async def wait_for_viewer(ws, ready):
        async for message in ws:
            if message.data == "next":
                ready.set()

    async def stream_frames(self, ws, stdout, ready):
        clock = asyncio.get_running_loop().time
        last_sent = clock()
        while not ws.closed:
            frame = await asyncio.wait_for(read_frame(stdout), 4)
            self.last_frame = (clock(), frame)
            if ready.is_set():
                ready.clear()
                await asyncio.wait_for(ws.send_bytes(frame), 2)
                last_sent = clock()
            elif clock() - last_sent > 6:
                # A suspended page may keep answering protocol pings without consuming frames.
                return

    async def shutdown(self, _app):
        if self.viewer is not None and self.viewer.prepared:
            await self.viewer.close()

    def register(self, app):
        app.on_shutdown.append(self.shutdown)
        app.router.add_get("/camera", self.state)
        app.router.add_patch("/camera", self.save)
        app.router.add_get("/camera/preview", self.preview, allow_head=False)
