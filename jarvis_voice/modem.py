"""Single-owner SIM7600 AT controller. USB audio lives on a separate raw PCM port."""
import asyncio
import logging
import re
import time

import serial

LOG = logging.getLogger(__name__)
NUMBER = re.compile(r"^\+?[0-9]{7,16}$")
CALL_END = {"NO CARRIER", "BUSY", "NO ANSWER", "NO DIALTONE"}
VOICE_RING = re.compile(r"(?:RING|\+CRING:\s*VOICE(?:/[^ ]+)?)$")
CALL_STATUS = re.compile(
    r'\+CLCC:\s*\d+\s*,\s*([01])\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*[01]'
    r'(?:\s*,\s*"(\+?[0-9]+)"\s*,\s*(\d+))?(?:,.*)?$'
)


class Modem:
    def __init__(self, port, baudrate=115200, on_event=None, max_calls_per_hour=4):
        self.port = port
        self.baudrate = baudrate
        self.on_event = on_event
        self.max_calls_per_hour = max_calls_per_hour
        self.serial = None
        self.lock = asyncio.Lock()
        self.pending = None
        self.pending_command = ""
        self.reader_task = None
        self.call_state = "offline"
        self.active_number = ""
        self.call_id = 0
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
                if name in {
                    "incoming_call", "outgoing_call", "caller_id", "call_connected",
                    "call_ended", "call_failed",
                }:
                    kwargs.setdefault("call_id", self.call_id)
                await self.on_event(name, kwargs)
            except Exception:
                LOG.exception("Event listener failed: %s", name)

    async def _supervise(self):
        while True:
            rx = None
            disconnected = False
            try:
                self.serial = serial.Serial(self.port, self.baudrate, timeout=0.15, write_timeout=2)
                rx = asyncio.create_task(self._reader())
                await asyncio.sleep(0.3)
                await self.command("ATE0")
                await self.command("AT+CMEE=2")
                await self.command("AT+CLIP=1")
                await self.command("AT+CLCC=1")
                self.model = " ".join((await self.command("AT+CGMM"))[:-1]).strip()
                self.firmware = " ".join((await self.command("AT+CGMR"))[:-1]).strip()
                self.call_state = "idle"
                LOG.info("SIM7600 connected: %s %s", self.model, self.firmware)
                await self._emit("modem_connected", model=self.model, firmware=self.firmware)
                while True:
                    done, _ = await asyncio.wait({rx}, timeout=15)
                    if done:
                        rx.result()
                    res = await self.command("AT+CSQ", timeout=3)
                    for line in res:
                        if line.startswith("+CSQ:"):
                            self.signal = line.split(":", 1)[1].strip()
            except asyncio.CancelledError:
                break
            except Exception:
                LOG.exception("SIM7600 disconnected; retrying")
                disconnected = True
            finally:
                self.call_state = "offline"
                self.active_number = ""
                self.signal = None
                port, self.serial = self.serial, None
                if self.pending and not self.pending.done():
                    self.pending.set_exception(ConnectionError("Modem disconnected"))
                if rx:
                    rx.cancel()
                    await asyncio.gather(rx, return_exceptions=True)
                if port:
                    port.close()
            if disconnected:
                await self._emit("modem_offline")
            await asyncio.sleep(5)

    async def _reader(self):
        port = self.serial
        partial = b""
        response = []
        previous = None
        while True:
            line = await asyncio.to_thread(port.readline)
            if not line:
                continue
            partial += line
            if not partial.endswith(b"\n"):
                continue
            value = partial.decode("utf-8", "replace").strip()
            partial = b""
            if not value:
                continue
            if VOICE_RING.fullmatch(value) or value.startswith(("VOICE CALL:", "+CLIP:")):
                await self._urc(value)
                continue
            status = CALL_STATUS.fullmatch(value)
            if status and self.pending_command != "AT+CLCC":
                await self._call_status(status)
                continue
            future = self.pending
            if value in CALL_END:
                if future and not future.done() and self.pending_command.startswith(
                    ("ATD", "ATA", "AT+CHUP")
                ):
                    future.set_result([value])
                await self._urc(value)
                continue
            if future is None or future.done():
                LOG.debug("Modem unsolicited: %s", value)
                continue
            if future is not previous:
                response = []
                previous = future
            if value != self.pending_command:
                response.append(value)
            if value in ("OK", "ERROR") or value.startswith(
                ("+CME ERROR:", "+CMS ERROR:")
            ):
                future.set_result(response)

    async def _incoming(self):
        self.call_id += 1
        self.active_number = ""
        self.call_state = "ringing"
        await self._emit("incoming_call", number=self.active_number)

    async def _caller(self, number, address_type):
        if address_type == "145" and not number.startswith("+"):
            number = "+" + number
        if number != self.active_number:
            self.active_number = number
            await self._emit("caller_id", number=number)

    async def _connected(self):
        self.call_state = "connected"
        await self._emit("call_connected", number=self.active_number)

    async def _call_status(self, status):
        direction, state, mode, number, address_type = status.groups()
        if mode != "0":
            return
        if direction == "1" and state == "4":
            if self.call_state == "idle":
                await self._incoming()
            if self.call_state == "ringing" and number:
                await self._caller(number, address_type)
        elif state == "0" and (direction, self.call_state) in (
            ("0", "dialing"), ("1", "answering")
        ):
            await self._connected()

    async def _urc(self, value):
        if value.startswith("+CLIP:") and self.call_state == "ringing":
            match = re.match(r'\+CLIP:\s*"(\+?[0-9]+)"\s*,\s*(\d+)(?:,|$)', value)
            if match:
                await self._caller(*match.groups())
        elif VOICE_RING.fullmatch(value) and self.call_state == "idle":
            await self._incoming()
        elif re.match(r"VOICE CALL:\s*BEGIN\b", value) and self.call_state in ("dialing", "answering"):
            await self._connected()
        elif (value in CALL_END or re.match(r"VOICE CALL:\s*END\b", value)) and self.call_state not in (
            "offline", "idle"
        ):
            self.call_state = "idle"
            self.active_number = ""
            await self._emit("call_ended", reason=value)

    async def phone_diagnostics(self):
        """Read network and call indicators without altering the modem settings."""
        fields = {}
        for name, command, prefix in (
            ("SIM", "AT+CPIN?", "+CPIN:"),
            ("CREG", "AT+CREG?", "+CREG:"),
            ("CEREG", "AT+CEREG?", "+CEREG:"),
            ("CGATT", "AT+CGATT?", "+CGATT:"),
            ("CPSI", "AT+CPSI?", "+CPSI:"),
            ("COPS", "AT+COPS?", "+COPS:"),
            ("CNMP", "AT+CNMP?", "+CNMP:"),
            ("CRC", "AT+CRC?", "+CRC:"),
            ("CLCC", "AT+CLCC?", "+CLCC:"),
            ("CEER", "AT+CEER", "+CEER:"),
            ("USB PID", "AT+CUSBPIDSWITCH?", "+CUSBPIDSWITCH:"),
            ("IMS", "AT+CIREG?", "+CIREG:"),
            ("VoLTE", "AT+VOLTESETTING?", "+VOLTESETTING:"),
            ("VoLTE modes", "AT+VOLTESETTING=?", "+VOLTESETTING:"),
        ):
            response = await self.command(command, timeout=3)
            value = next(
                (line.removeprefix(prefix).strip() for line in response if line.startswith(prefix)),
                None,
            )
            if value is not None:
                if name in {"CREG", "CEREG", "CPSI"}:
                    value = ",".join(value.split(",")[:2])
                fields[name] = value
            elif name in {"SIM", "IMS", "VoLTE", "VoLTE modes"} and response and response[-1] != "OK":
                fields[name] = response[-1]
        return fields

    async def data_diagnostics(self):
        response = await self.command("AT+CGDCONT?", timeout=3)
        return [
            {"cid": int(cid), "type": kind, "apn": apn}
            for line in response
            if (match := re.match(r'\+CGDCONT:\s*(\d+)\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"', line))
            for cid, kind, apn in [match.groups()]
        ]

    async def read_sms(self, on_messages):
        """Import both SIM and flash stores and restore modem SMS preferences."""
        idle = (self.call_id, "idle")
        saved = await self.command("AT+CPMS?", timeout=3, expected_call=idle)
        previous = next(
            (match[1] for line in saved if (match := re.match(r'\+CPMS:\s*"(ME|SM|MT|SR)"', line))),
            None,
        )
        format_response = await self.command("AT+CMGF?", timeout=3, expected_call=idle)
        old_format = next(
            (match[1] for line in format_response if (match := re.fullmatch(r"\+CMGF:\s*([01])", line))),
            None,
        )
        if previous is None or old_format is None:
            raise RuntimeError("Не удалось прочитать настройки памяти SMS.")
        selected = previous
        usage = {}
        try:
            if old_format != "0":
                result = await self.command("AT+CMGF=0", timeout=3, expected_call=idle)
                if result[-1] != "OK":
                    raise RuntimeError("Модем не принял формат PDU для SMS.")
            for storage in ("SM", "ME"):
                result = await self.command(f'AT+CPMS="{storage}"', timeout=3, expected_call=idle)
                if result[-1] != "OK":
                    continue
                selected = storage
                for line in result:
                    if match := re.match(r"\+CPMS:\s*(\d+)\s*,\s*(\d+)", line):
                        usage[storage] = {"used": int(match[1]), "total": int(match[2])}
                lines = await self.command("AT+CMGL=4", timeout=8, expected_call=idle)
                if lines[-1] != "OK":
                    raise RuntimeError(f"Модем не прочитал SMS из памяти {storage}.")
                await on_messages(lines)
            if not usage:
                raise RuntimeError("Модем не открыл память SMS.")
            return usage
        finally:
            if self.serial and self.serial.is_open:
                if selected != previous:
                    await self.command(f'AT+CPMS="{previous}"', timeout=3)
                if old_format != "0":
                    await self.command(f"AT+CMGF={old_format}", timeout=3)

    async def command(self, cmd, timeout=6, *, expected_call=None):
        if not re.fullmatch(r"[\x20-\x7e]+", cmd) or "\r" in cmd or "\n" in cmd:
            raise ValueError("Unsafe AT command")
        async with self.lock:
            port = self.serial
            if not port or not port.is_open:
                raise ConnectionError("Modem is offline")
            if expected_call is not None and expected_call != (self.call_id, self.call_state):
                raise RuntimeError("Call state changed before the command could be sent")
            loop = asyncio.get_running_loop()
            future = self.pending = loop.create_future()
            self.pending_command = cmd
            try:
                await asyncio.to_thread(port.write, (cmd + "\r").encode("ascii"))
                return await asyncio.wait_for(asyncio.shield(future), timeout)
            except (TimeoutError, serial.SerialException, asyncio.CancelledError):
                if not future.done():
                    # A late terminal response must not complete the next command.
                    port.close()
                raise
            finally:
                future.cancel()
                self.pending = None
                self.pending_command = ""

    async def dial(self, number):
        if not NUMBER.fullmatch(number):
            raise ValueError("Invalid phone number")
        if self.call_state != "idle":
            raise RuntimeError("Modem is busy")
        now = time.monotonic()
        self.calls = [x for x in self.calls if now - x < 3600]
        if len(self.calls) >= self.max_calls_per_hour:
            raise RuntimeError("Hourly call limit reached")
        self.call_id += 1
        call = (self.call_id, "dialing")
        self.active_number = number
        self.call_state = "dialing"
        try:
            await self._emit("outgoing_call", number=number)
            lines = await self.command("ATD" + number + ";", timeout=12, expected_call=call)
            if lines[-1] != "OK":
                raise RuntimeError("Dial failed: " + " ".join(lines))
            self.calls.append(now)
            if (self.call_id, self.call_state) == call:
                await self._emit("call_dialing", number=number)
        except (Exception, asyncio.CancelledError) as exc:
            if (self.call_id, self.call_state) == call:
                self.call_state = "idle"
                self.active_number = ""
                await self._emit("call_failed", reason=str(exc) or type(exc).__name__)
            raise

    async def answer(self):
        if self.call_state != "ringing":
            raise RuntimeError("No incoming call")
        self.call_state = "answering"
        call = (self.call_id, "answering")
        try:
            lines = await self.command("ATA", expected_call=call)
            if lines[-1] != "OK":
                raise RuntimeError("Answer failed: " + " ".join(lines))
            return lines
        except (Exception, asyncio.CancelledError):
            if (self.call_id, self.call_state) == call:
                self.call_state = "ringing"
            raise

    async def hangup(self):
        if self.call_state in ("offline", "idle"):
            return
        call = (self.call_id, self.call_state)
        lines = await self.command("AT+CHUP", timeout=5, expected_call=call)
        if lines[-1] not in ("OK", "NO CARRIER"):
            raise RuntimeError("Hangup failed: " + " ".join(lines))
        if self.call_id == call[0]:
            await self._urc("NO CARRIER")

    async def enable_pcm(self):
        # These commands generally fail when no voice call is active.
        if self.call_state != "connected":
            raise RuntimeError("No connected call")
        call = (self.call_id, "connected")
        for cmd in ("AT+CPCMFRM=1", "AT+CPCMREG=1"):
            result = await self.command(cmd, expected_call=call)
            if not result or result[-1] != "OK":
                raise RuntimeError(cmd + " unsupported by firmware: " + str(result))
        reported = await self.command("AT+CPCMFRM?", expected_call=call)
        if "+CPCMFRM: 1" in reported:
            LOG.info("Modem reports 16 kHz USB call audio")
        else:
            LOG.warning("Could not confirm 16 kHz USB call audio: %s", reported)
