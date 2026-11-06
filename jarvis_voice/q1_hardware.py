"""Signal Q1 hardware discovery and UVC camera capture.

Facts from original Q1 /opt code, not assumptions:
  camera VID:PID 0c45:6366 (Sonix), YUYV 640x480@30 used for vision,
  H264 also probed for video; original video controls in camera.opts,
  USB audio card identified by 'Microchip Tech USB2 Controller Hub',
  modem VID:PID 1e0e:9001, AT serial alias /dev/modemAT.

This module never exposes privileged shell access or automatically changes UVC controls.
"""
import asyncio
import glob
import json
import logging
import os
import re
from pathlib import Path

LOG = logging.getLogger(__name__)

Q1_USB_IDS = {
    ("0c45", "6366"): "Q1 Sonix USB camera",
    ("1e0e", "9001"): "SIM7600 LTE modem",
    ("0424", "494a"): "Microchip USB audio/controller",
    ("0424", "4714"): "USB4714 hub",
}
ORIGINAL_CONTROLS = {
    "brightness": 20,
    "contrast": 32,
    "saturation": 0,
    "hue": 0,
    "white_balance_temperature_auto": 1,
    "gamma": 100,
    "gain": 0,
    "power_line_frequency": 1,
    "sharpness": 2,
    "backlight_compensation": 1,
    "exposure_auto": 3,
    "exposure_auto_priority": 0,
}
VIDEO_PATH = re.compile(r"^/dev/video[0-9]{1,3}$")
VIDEO_LINK = re.compile(r"/dev/v4l/by-id/[a-zA-Z0-9_.+-]+-video-index[0-9]+")


def camera_device(device):
    """Resolve a stable udev path only if it points to a V4L2 device node."""
    if VIDEO_LINK.fullmatch(device):
        device = os.path.realpath(device)
    if not VIDEO_PATH.fullmatch(device):
        raise ValueError("Use /dev/videoN or a /dev/v4l/by-id link to that device")
    return device


async def _run(*args, timeout=8):
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError) as exc:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.communicate()
        if isinstance(exc, TimeoutError):
            raise RuntimeError(f"{args[0]} timed out") from exc
        raise
    if proc.returncode:
        raise RuntimeError(f"{args[0]} exit={proc.returncode}: {stderr.decode(errors='replace')[-250:]}")
    return stdout


def list_usb():
    """Read sysfs, without lsusb or root and without displaying modem identifiers."""
    found = []
    for path in glob.glob("/sys/bus/usb/devices/*/idVendor"):
        root = Path(path).parent
        try:
            vendor = (root / "idVendor").read_text().strip().lower()
            product = (root / "idProduct").read_text().strip().lower()
        except OSError:
            continue
        name = Q1_USB_IDS.get((vendor, product))
        if name:
            found.append({"vendor": vendor, "product": product, "description": name})
    return found


def list_serial():
    # Do not include device contents. /dev/modemAT is the original Q1 alias.
    known = ["/dev/modemAT", "/dev/cdc-wdm0"]
    return sorted(set(x for x in known + glob.glob("/dev/ttyUSB*") + glob.glob("/dev/serial/by-id/*") if os.path.exists(x)))


async def probe_audio():
    """ALSA / Pulse diagnostics. No assumption that a Q1 microphone exists."""
    results = {}
    for name, argv in {
        "playback": ("aplay", "-l"),
        "capture": ("arecord", "-l"),
        "pulseaudio_sinks": ("pactl", "list", "short", "sinks"),
        "pulseaudio_sources": ("pactl", "list", "short", "sources"),
    }.items():
        try:
            out = (await _run(*argv, timeout=5)).decode(errors="replace")
            results[name] = out[:3000]
        except (FileNotFoundError, RuntimeError) as exc:
            results[name] = "unavailable: " + str(exc)[:120]
    try:
        results["alsa_cards"] = Path("/proc/asound/cards").read_text(errors="replace")[:2000]
    except OSError:
        results["alsa_cards"] = "not mounted"
    return results


async def probe_camera(device):
    device = camera_device(device)
    return (await _run("v4l2-ctl", "-d", device, "--list-formats-ext", timeout=8)).decode(errors="replace")[:10000]


async def snapshot(device):
    """Original Q1 analytics uses uncompressed YUYV 640x480 @30. Try that first."""
    device = camera_device(device)
    presets = [
        ("-input_format", "yuyv422", "-video_size", "640x480", "-framerate", "30"),
        ("-video_size", "640x480"),
        (),
    ]
    errors = []
    for preset in presets:
        args = ("ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-f", "video4linux2", *preset, "-i", device,
                "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1")
        try:
            image = await _run(*args, timeout=12)
            if image.startswith(b"\xff\xd8") and image.endswith(b"\xff\xd9") and len(image) <= 4_000_000:
                return image
            errors.append("Output was not a valid JPEG")
        except (FileNotFoundError, RuntimeError) as exc:
            errors.append(str(exc))
    raise RuntimeError("Q1 UVC capture failed: " + " | ".join(errors)[-400:])


async def apply_original_camera_controls(device):
    """Opt-in copy of camera.opts values; never applied at service start."""
    device = camera_device(device)
    results = {}
    for control, value in ORIGINAL_CONTROLS.items():
        try:
            await _run("v4l2-ctl", "-d", device, "-c", f"{control}={value}", timeout=4)
            results[control] = "ok"
        except (FileNotFoundError, RuntimeError) as exc:
            results[control] = str(exc)[:120]
    return results


async def diagnostics(camera=""):
    result = {"usb_devices": list_usb(), "serial_ports": list_serial(),
              "video_nodes": sorted(glob.glob("/dev/video*") + glob.glob("/dev/v4l/by-id/*")),
              "audio": await probe_audio()}
    if camera:
        try:
            result["video_formats"] = await probe_camera(camera)
        except Exception as exc:
            result["video_formats_error"] = str(exc)[:200]
    return result
