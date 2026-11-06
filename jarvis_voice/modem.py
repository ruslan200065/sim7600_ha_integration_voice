"""Single-owner SIM7600 AT controller. USB audio lives on a separate raw PCM port."""
import asyncio
import logging
import re
import time

import serial

LOG = logging.getLogger(__name__)
NUMBER = re.compile(r"^\+?[0-9]{7,16}$")


class Modem:
    def __init__(self, port, baudrate=115200, on_event=None, max_calls_per_hour=4):
        self.port = port
        self.baudrate = baudrate
        self.on_event = on_event
        self.max_calls_per_hour = max_calls_per_hour
        self.serial = None
        self.lock = asyncio.Lock()
        self.pending = None
        self.response = []
        self.reader_task = None
        self.call_state = "offline"
        self.active_number = ""
        self.calls = []
        self.signal = None
        self.model = ""
        self.firmware = ""

    async def start(self):
        self.reader_task = asyncio.create_task(self._supervise())

    async def stop(self):
        if self.reader_task:
            self.reader_task.cancel()
            try:
                await self.reader_task
            except asyncio.CancelledError:
                pass
        if self.serial:
            self.serial.close()
            self.serial = None

    async def _emit(self, name, **kwargs):
        if self.on_event:
            try:
                await self.on_event(name, kwargs)
            except Exception:
                LOG.exception("Event listener failed: %s", name)

    async def _supervise(self):
        while True:
            try:
                self.serial = serial.Serial(self.port, self.baudrate, timeout=0.15, write_timeout=2)
                self.call_state = "idle"
                rx = asyncio.create_task(self._reader())
                await asyncio.sleep(0.3)
                await self.command("ATE0")
                await self.command("AT+CMEE=2")
                await self.command("AT+CLIP=1")
                self.model = " ".join((await self.command("AT+CGMM"))[:-1]).strip()
                self.firmware = " ".join((await self.command("AT+CGMR"))[:-1]).strip()
                LOG.info("SIM7600 connected: %s %s", self.model, self.firmware)
                await self._emit("modem_connected", model=self.model, firmware=self.firmware)
                while True:
                    await asyncio.sleep(15)
                    res = await self.command("AT+CSQ", timeout=3)
                    for line in res:
                        if line.startswith("+CSQ:"):
                            self.signal = line.split(":", 1)[1].strip()
            except asyncio.CancelledError:
                break
            except Exception:
                LOG.exception("SIM7600 disconnected; retrying")
                self.call_state = "offline"
                await self._emit("modem_offline")
            finally:
                if "rx" in locals():
                    rx.cancel()
                    try:
                        await rx
                    except asyncio.CancelledError:
                        pass
                    del rx
                if self.serial:
                    self.serial.close()
                    self.serial = None
                if self.pending and not self.pending.done():
                    self.pending.set_exception(ConnectionError("Modem disconnected"))
                    self.pending = None
            await asyncio.sleep(5)

    async def _reader(self):
        while True:
            line = await asyncio.to_thread(self.serial.readline)
            if not line:
                await asyncio.sleep(0)
                continue
            value = line.decode("utf-8", "replace").strip()
            if not value:
                continue
            if value in ("RING", "NO CARRIER", "BUSY") or value.startswith(
                ("VOICE CALL:", "+CLIP:", "+CME ERROR:", "+CLCC:")
            ):
                asyncio.create_task(self._urc(value))
            if self.pending:
                self.response.append(value)
                if value in ("OK", "ERROR", "NO CARRIER") or value.startswith("+CME ERROR:"):
                    future = self.pending
                    self.pending = None
                    if not future.done():
                        future.set_result(self.response[:])
            else:
                LOG.debug("Modem unsolicited: %s", value)

    async def _urc(self, value):
        if value.startswith("+CLIP:"):
            match = re.search(r'"([^"]+)"', value)
            if match:
                self.active_number = match.group(1)
        elif value == "RING":
            self.call_state = "ringing"
            await self._emit("incoming_call", number=self.active_number)
        elif "VOICE CALL: BEGIN" in value:
            self.call_state = "connected"
            await self._emit("call_connected", number=self.active_number)
        elif value in ("NO CARRIER", "BUSY") or "VOICE CALL: END" in value:
            previous = self.call_state
            self.call_state = "idle"
            self.active_number = ""
            if previous in ("connected", "dialing", "ringing"):
                await self._emit("call_ended", reason=value)

    async def command(self, cmd, timeout=6):
        if not re.fullmatch(r"[\x20-\x7e]+", cmd) or "\r" in cmd or "\n" in cmd:
            raise ValueError("Unsafe AT command")
        async with self.lock:
            if not self.serial or not self.serial.is_open:
                raise ConnectionError("Modem is offline")
            loop = asyncio.get_running_loop()
            self.response = []
            self.pending = loop.create_future()
            try:
                await asyncio.to_thread(self.serial.write, (cmd + "\r").encode("ascii"))
                return await asyncio.wait_for(self.pending, timeout)
            finally:
                self.pending = None

    async def dial(self, number):
        if not NUMBER.fullmatch(number):
            raise ValueError("Invalid phone number")
        if self.call_state != "idle":
            raise RuntimeError("Modem is busy")
        now = time.monotonic()
        self.calls = [x for x in self.calls if now - x < 3600]
        if len(self.calls) >= self.max_calls_per_hour:
            raise RuntimeError("Hourly call limit reached")
        self.active_number = number
        self.call_state = "dialing"
        try:
            lines = await self.command("ATD" + number + ";", timeout=12)
            if any(x in ("ERROR", "NO CARRIER") or x.startswith("+CME ERROR:") for x in lines):
                self.call_state = "idle"
                raise RuntimeError("Dial failed: " + " ".join(lines))
            self.calls.append(now)
            await self._emit("call_dialing", number=number)
        except Exception:
            self.call_state = "idle"
            raise

    async def answer(self):
        if self.call_state != "ringing":
            raise RuntimeError("No incoming call")
        return await self.command("ATA")

    async def hangup(self):
        if self.call_state == "offline":
            return
        await self.command("AT+CHUP", timeout=5)
        await self._urc("NO CARRIER")

    async def enable_pcm(self):
        # These commands generally fail when no voice call is active.
        if self.call_state != "connected":
            raise RuntimeError("No connected call")
        for cmd in ("AT+CPCMFRM=1", "AT+CPCMREG=1"):
            result = await self.command(cmd)
            if not result or result[-1] != "OK":
                raise RuntimeError(cmd + " unsupported by firmware: " + str(result))
