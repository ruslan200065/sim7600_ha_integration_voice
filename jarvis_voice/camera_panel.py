"""On-demand UVC preview, camera mode discovery and controls."""

import asyncio
import json
import re
from contextlib import asynccontextmanager, suppress
from fractions import Fraction
from pathlib import Path

from aiohttp import web
from processes import cancel_task, start_process, stop_process
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
            mode = {
                "format": pixel_format,
                "width": width,
                "height": height,
                "fps": fps,
            }
            if mode not in modes:
                modes.append(mode)
    return modes


def default_mode(modes):
    return (
        min(
            modes,
            key=lambda mode: (
                mode["format"] != "MJPG",
                abs(mode["width"] - 640) + abs(mode["height"] - 480),
                abs(mode["fps"] - 15),
            ),
        )
        if modes
        else None
    )


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
                flag in flags
                for flag in ("read-only", "write-only", "disabled", "volatile")
            ):
                continue
            values = {key: int(value) for key, value in CONTROL_VALUE.findall(details)}
            if kind == "bool":
                values.update(min=0, max=1, step=1)
            if not {"min", "max", "value"} <= values.keys():
                continue
            current = {
                "id": name,
                "type": kind,
                **values,
                "choices": [],
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
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "v4l2",
        "-input_format",
        FORMATS[mode["format"]],
        "-video_size",
        f"{mode['width']}x{mode['height']}",
        "-framerate",
        str(fps),
        "-threads",
        "1",
        "-i",
        device,
        "-an",
    )
    codec = (
        ("-c:v", "copy")
        if mode["format"] == "MJPG"
        else (
            "-c:v",
            "mjpeg",
            "-threads",
            "1",
            "-q:v",
            "5",
        )
    )
    return (
        *cmd,
        *codec,
        "-flush_packets",
        "1",
        "-f",
        "mpjpeg",
        "-boundary_tag",
        "jarvis",
        "pipe:1",
    )


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
        self.capture_task = None
        self.subscribers = set()
        self.lifecycle_lock = asyncio.Lock()
        self.closing = False
        self.viewers = set()
        self.released = asyncio.Event()
        self.released.set()
        self.last_frame = None
        self.settings_lock = asyncio.Lock()

    def configured_device(self):
        value = self.service.o.get("q1_video_device", "")
        if not value:
            raise ValueError(
                "Выберите видеоустройство в разделе «Оборудование» и примените настройки."
            )
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
        if not isinstance(data, dict) or data.get("device") != self.device_key(device):
            return {}
        if not isinstance(data.get("controls"), dict):
            data.pop("controls", None)
        return data

    def write_settings(self, device, settings):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"device": self.device_key(device), **settings}),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    async def state(self, _request):
        try:
            device = self.configured_device()
            modes, controls, saved = await asyncio.gather(
                self.modes(device),
                self.controls(device),
                asyncio.to_thread(self.read_settings, device),
            )
            return web.json_response(
                {
                    "device": device,
                    "modes": modes,
                    "mode": saved.get("mode")
                    if saved.get("mode") in modes
                    else default_mode(modes),
                    "controls": controls,
                }
            )
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
                    mode = next(
                        (
                            item
                            for item in await self.modes(device)
                            if item == data["mode"]
                        ),
                        None,
                    )
                    if mode is None:
                        raise ValueError("Выберите поддерживаемый режим камеры.")
                    settings["mode"] = mode
                elif set(data) == {"control", "value"}:
                    control = next(
                        (
                            row
                            for row in await self.controls(device)
                            if row["id"] == data["control"]
                        ),
                        None,
                    )
                    value = data["value"]
                    if not control or control["inactive"] or type(value) is not int:
                        raise ValueError("Эта настройка камеры сейчас недоступна.")
                    allowed = [choice["value"] for choice in control["choices"]]
                    step = max(1, control.get("step", 1))
                    if (
                        not control["min"] <= value <= control["max"]
                        or (value - control["min"]) % step
                        or (allowed and value not in allowed)
                    ):
                        raise ValueError("Значение вне диапазона камеры.")
                    await _run(
                        "v4l2-ctl", "-d", device, "-c", f"{control['id']}={value}"
                    )
                    settings.setdefault("controls", {})[control["id"]] = value
                    self.service.camera_frame = None
                else:
                    raise ValueError("Неверные настройки камеры.")
                await asyncio.to_thread(self.write_settings, device, settings)
            if "mode" in data:
                await self.restart_capture(device)
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
                    await _run(
                        "v4l2-ctl", "-d", device, "-c", f"{name}={value}", timeout=2
                    )

    @asynccontextmanager
    async def subscribe(self):
        """Start one capture for the first viewer and stop it after the last."""
        device = self.configured_device()
        queue = asyncio.Queue(maxsize=1)
        async with self.lifecycle_lock:
            if self.closing:
                raise RuntimeError("Захват камеры остановлен.")
            if len(self.subscribers) >= 8:
                raise RuntimeError("Слишком много подключений к камере.")
            self.subscribers.add(queue)
            if self.capture_task is None or self.capture_task.done():
                self.start_capture(device)
        try:
            yield queue
        finally:
            async with self.lifecycle_lock:
                self.subscribers.discard(queue)
                if not self.subscribers:
                    await self.stop_capture()

    def start_capture(self, device):
        self.device = device
        self.released.clear()
        self.capture_task = asyncio.create_task(self.capture(device))

    async def restart_capture(self, device):
        async with self.lifecycle_lock:
            if self.closing or not self.subscribers:
                return
            await self.stop_capture()
            for queue in self.subscribers:
                if not queue.empty():
                    queue.get_nowait()
            self.start_capture(device)

    async def stop_capture(self):
        """Stop the current worker while the caller holds the lifecycle lock."""
        try:
            if self.capture_task is not None:
                await cancel_task(self.capture_task)
        finally:
            self.capture_task = None
            self.last_frame = None
            self.device = None
            self.released.set()

    def publish(self, item):
        for queue in self.subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(item)

    async def capture(self, device):
        proc = None
        try:
            async with self.service.camera_lock, self.settings_lock:
                modes = await self.modes(device)
                saved = await asyncio.to_thread(self.read_settings, device)
                mode = saved.get("mode")
                if mode not in modes:
                    mode = default_mode(modes)
                if mode is None:
                    raise RuntimeError("Камера не сообщила поддерживаемый режим видео.")
                await self.restore_controls(device, saved.get("controls", {}))
                proc = await start_process(
                    *preview_args(device, mode),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            clock = asyncio.get_running_loop().time
            while True:
                frame = await asyncio.wait_for(read_frame(proc.stdout), 4)
                self.last_frame = (clock(), frame)
                self.publish(frame)
        except (
            TimeoutError,
            asyncio.IncompleteReadError,
            ValueError,
            RuntimeError,
            OSError,
        ) as exc:
            message = str(exc) or "Камера перестала передавать кадры."
            self.publish(RuntimeError(message[:250]))
        finally:
            try:
                if proc is not None:
                    await stop_process(proc)
            finally:
                self.last_frame = None
                self.device = None
                self.released.set()

    @staticmethod
    async def next_frame(queue):
        frame = await asyncio.wait_for(queue.get(), 12)
        if isinstance(frame, RuntimeError):
            raise frame
        return frame

    async def mjpeg(self, request):
        """Authenticated app API used by the Home Assistant camera entity."""
        response = web.StreamResponse(
            headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=jarvis",
                "Cache-Control": "no-store",
            }
        )
        try:
            async with self.subscribe() as queue:
                frame = await self.next_frame(queue)
                await response.prepare(request)
                while request.transport and not request.transport.is_closing():
                    header = BOUNDARY + (
                        f"Content-Type: image/jpeg\r\nContent-Length: {len(frame)}\r\n\r\n"
                    ).encode("ascii")
                    async with asyncio.timeout(3):
                        # Each viewer shares the same JPEG bytes.
                        await response.write(header)
                        await response.write(frame)
                        await response.write(b"\r\n")
                    frame = await self.next_frame(queue)
        except (RuntimeError, TimeoutError, ValueError, OSError) as exc:
            if not response.prepared:
                raise web.HTTPServiceUnavailable(text=str(exc)[:250]) from exc
        return response

    async def preview(self, request):
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            raise web.HTTPForbidden()
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=1024)
        await ws.prepare(request)
        self.viewers.add(ws)
        ready = asyncio.Event()
        ready.set()
        receiver = asyncio.create_task(self.wait_for_viewer(ws, ready))
        try:
            async with self.subscribe() as queue:
                producer = asyncio.create_task(self.stream_frames(ws, queue, ready))
                try:
                    done, _ = await asyncio.wait(
                        {producer, receiver},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if producer in done:
                        await producer
                finally:
                    producer.cancel()
                    await asyncio.gather(producer, return_exceptions=True)
        except (RuntimeError, TimeoutError, ValueError, OSError) as exc:
            if not ws.closed:
                with suppress(TimeoutError, RuntimeError, OSError):
                    await asyncio.wait_for(
                        ws.send_json(
                            {
                                "error": str(exc)[:250]
                                or "Камера перестала передавать кадры.",
                            }
                        ),
                        1,
                    )
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
            self.viewers.discard(ws)
            await ws.close()
        return ws

    @staticmethod
    async def wait_for_viewer(ws, ready):
        async for message in ws:
            if message.data == "next":
                ready.set()

    async def stream_frames(self, ws, queue, ready):
        while not ws.closed:
            await asyncio.wait_for(ready.wait(), 6)
            ready.clear()
            frame = await self.next_frame(queue)
            await asyncio.wait_for(ws.send_bytes(frame), 2)

    async def shutdown(self, _app):
        self.closing = True
        async with self.lifecycle_lock:
            await self.stop_capture()
            self.publish(RuntimeError("Захват камеры остановлен."))
        await asyncio.gather(*(ws.close() for ws in tuple(self.viewers)))

    def register(self, app):
        app.on_shutdown.append(self.shutdown)
        app.router.add_get("/camera", self.state)
        app.router.add_patch("/camera", self.save)
        app.router.add_get("/camera/preview", self.preview, allow_head=False)
