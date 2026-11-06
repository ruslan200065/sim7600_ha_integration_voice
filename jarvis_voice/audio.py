"""SIM7600 raw S16LE, 16kHz/mono transport. Modem firmware must expose USB PCM."""
import asyncio
import logging
import struct
import time
import serial

LOG = logging.getLogger(__name__)
RATE = 16000
FRAME = 3200  # 100ms, 16kHz 16-bit mono
PLAYBACK_FRAME = 640  # 20ms, 16kHz 16-bit mono
PLAYBACK_PREFILL = 6  # 120ms of audio before writing to the modem
PLAYBACK_SAMPLES = struct.Struct(f"<{PLAYBACK_FRAME // 2}h")


async def playback_frames(chunks):
    """Frame a byte stream across SpeechKit message boundaries."""
    pending = bytearray()
    async for chunk in chunks:
        pending.extend(chunk)
        complete = len(pending) // PLAYBACK_FRAME * PLAYBACK_FRAME
        for offset in range(0, complete, PLAYBACK_FRAME):
            yield bytes(pending[offset : offset + PLAYBACK_FRAME])
        del pending[:complete]
    if len(pending) % 2:
        raise ValueError("Incomplete 16-bit PCM sample")
    if pending:
        yield bytes(pending).ljust(PLAYBACK_FRAME, b"\x00")


class PCMTransport:
    def __init__(self, port, playback_volume=100):
        self.port = port
        self.playback_gain = playback_volume / 100
        self.serial = None
        self.rx_task = None
        self.queue = asyncio.Queue(maxsize=40)
        self.running = False

    async def start(self):
        if self.running:
            return
        self.serial = serial.Serial(self.port, baudrate=115200, timeout=0.1, write_timeout=2)
        while not self.queue.empty():
            self.queue.get_nowait()
        self.running = True
        self.rx_task = asyncio.create_task(self._reader())

    async def stop(self):
        self.running = False
        if self.rx_task:
            self.rx_task.cancel()
            try:
                await self.rx_task
            except asyncio.CancelledError:
                pass
            self.rx_task = None
        if self.serial:
            self.serial.close()
            self.serial = None

    async def _reader(self):
        pending = bytearray()
        while self.running:
            chunk = await asyncio.to_thread(self.serial.read, FRAME - len(pending))
            if not chunk:
                continue
            pending.extend(chunk)
            if len(pending) < FRAME:
                continue
            if self.queue.full():
                self.queue.get_nowait()
            self.queue.put_nowait(bytes(pending))
            pending.clear()

    async def write_stream(self, chunks):
        port = self.serial
        if not self.running or port is None:
            raise RuntimeError("PCM audio unavailable")
        frames = asyncio.Queue(maxsize=24)
        ready = asyncio.Event()
        interval = PLAYBACK_FRAME / (RATE * 2)
        clock = asyncio.get_running_loop().time

        async def fill():
            try:
                async for frame in playback_frames(chunks):
                    if self.playback_gain != 1:
                        samples = PLAYBACK_SAMPLES.unpack(frame)
                        frame = PLAYBACK_SAMPLES.pack(
                            *(int(sample * self.playback_gain) for sample in samples)
                        )
                    await frames.put(frame)
                    if frames.qsize() == PLAYBACK_PREFILL:
                        ready.set()
                await frames.put(None)
            finally:
                ready.set()

        async with asyncio.TaskGroup() as group:
            group.create_task(fill())
            await ready.wait()
            deadline = clock()
            while (frame := await frames.get()) is not None:
                now = clock()
                if now - deadline >= interval:
                    # Restart the schedule after an underrun instead of sending a burst.
                    deadline = now
                if deadline > now:
                    await asyncio.sleep(deadline - now)
                if not self.running or self.serial is not port:
                    raise RuntimeError("PCM audio unavailable")
                write = asyncio.create_task(asyncio.to_thread(port.write, frame))
                try:
                    written = await asyncio.shield(write)
                except asyncio.CancelledError:
                    # A cancelled await cannot stop the serial write in its thread.
                    await asyncio.gather(write, return_exceptions=True)
                    raise
                if written != len(frame):
                    raise RuntimeError("Incomplete PCM write")
                deadline += interval
            await asyncio.sleep(max(0, deadline - clock()))


def rms(chunk):
    if len(chunk) < 2:
        return 0
    n = len(chunk) // 2
    samples = struct.unpack("<%dh" % n, chunk[:n*2])
    return int((sum(int(s) * int(s) for s in samples) / n) ** 0.5)


async def read_utterances(source, threshold=700, silence_seconds=0.75, max_seconds=14):
    """Low-compute turn detection; caller owns acoustic echo avoidance."""
    recording = bytearray()
    start = last_voice = None
    while True:
        data = await source.get()
        now = time.monotonic()
        voice = rms(data) >= threshold
        if voice and start is None:
            start = now
            last_voice = now
        if start is None:
            continue
        recording.extend(data)
        if voice:
            last_voice = now
        if (now - last_voice > silence_seconds) or (now-start > max_seconds):
            if len(recording) > RATE * 2 // 3:
                yield bytes(recording)
            recording.clear()
            start = last_voice = None
