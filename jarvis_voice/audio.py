"""SIM7600 raw S16LE, 16kHz/mono transport. Modem firmware must expose USB PCM."""
import asyncio
import logging
import os
import struct
import time
import serial

LOG = logging.getLogger(__name__)
RATE = 16000
FRAME = 3200  # 100ms, 16kHz 16-bit mono


class PCMTransport:
    def __init__(self, port):
        self.port = port
        self.serial = None
        self.rx_task = None
        self.queue = asyncio.Queue(maxsize=40)
        self.running = False

    async def start(self):
        if self.running:
            return
        self.serial = serial.Serial(self.port, baudrate=115200, timeout=0.1, write_timeout=2)
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
        while self.running:
            # ttyUSB4 is raw byte stream at the negotiated 16kHz PCM format.
            chunk = await asyncio.to_thread(self.serial.read, FRAME)
            if not chunk:
                continue
            if self.queue.full():
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            self.queue.put_nowait(chunk)

    async def write(self, pcm):
        if not self.running:
            raise RuntimeError("PCM audio unavailable")
        # Write at the sample rate so the modem's audio buffer cannot overflow.
        for offset in range(0, len(pcm), FRAME):
            data = pcm[offset:offset + FRAME]
            await asyncio.to_thread(self.serial.write, data)
            await asyncio.sleep(len(data) / (RATE * 2))


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
