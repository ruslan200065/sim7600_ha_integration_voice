"""Modem communication layer for SIM7600."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import re
import time
from collections.abc import Callable
from typing import Any

import serial
import serial_asyncio_fast

from .const import LOGGER
from .sms_codec import (
    USSD_CODE_RE,
    CusdReply,
    SmsAssembler,
    build_submit_pdus,
    is_error_result,
    is_final_result,
    is_valid_phone_number,
    parse_cmgl_pdu,
    parse_cusd,
    sanitize_sms_text,
)
from .types import GpsData, SmsData

# Lines that are always unsolicited result codes (never part of a response).
ALWAYS_URC_PREFIXES = (
    "RING",
    "+CRING:",
    "+CLIP:",
    "+CMTI:",
    "+CMT:",
    "+CDSI:",
    "+CDS:",
    "+CUSD:",
    "NO CARRIER",
    "MISSED_CALL:",
    "VOICE CALL:",
    "+CGEV:",
    "+CTZV:",
    "+CTZE:",
    "SMS DONE",
    "PB DONE",
    "RDY",
    "*ATREADY",
)
# Lines that are URCs unless they answer the command that is currently pending
# (for example "+CREG: 1" is a URC, "+CREG: 0,1" answers AT+CREG?).
AMBIGUOUS_URC_PREFIXES = ("+CPIN:", "+CREG:", "+CGREG:", "+CEREG:", "+CLCC:")
# URCs that are followed by one payload line.
URC_WITH_BODY = ("+CMT:", "+CDS:")
# Response headers that are followed by one raw body line which must not be
# interpreted as result code, echo or URC (e.g. an SMS body "OK").
BODY_HEADERS = ("+CMGL:", "+CMGR:")

INIT_COMMANDS = (
    "ATE0",
    "AT+CMEE=2",
    "AT+CMGF=0",
    "AT+CNMI=2,1,0,0,0",
    "AT+CLIP=1",
)

DEFAULT_TIMEOUT = 5.0
INIT_TIMEOUT = 2.0
MAX_CONSECUTIVE_TIMEOUTS = 3
SETTLE_QUIET = 0.1
SETTLE_MAX = 0.5
SMS_PROMPT_TIMEOUT = 10.0
SMS_SEND_TIMEOUT = 60.0
USSD_REPLY_TIMEOUT = 30.0
CMGL_TIMEOUT = 15.0

_PENDING_PREFIX_RE = re.compile(r"^AT([+^$#*][A-Z0-9]+)", re.IGNORECASE)


class ModemError(Exception):
    """Base error for modem communication."""


class ModemConnectionError(ModemError):
    """The serial connection failed or was lost."""


class ModemTimeoutError(ModemError):
    """The modem stopped answering."""


class ModemCommandError(ModemError):
    """The modem rejected a command."""


class UssdError(ModemCommandError):
    """A USSD request failed or got no reply."""


class InvalidInputError(ModemError, ValueError):
    """User input (number, text, USSD code) is invalid."""


UrcCallback = Callable[[str], None]


class SIM7600Modem:
    """Interface to communicate with SIM7600 via AT commands.

    A background task reads the serial port. Lines are classified as:
    - echo (starts with "AT") -> dropped,
    - URC (RING, +CMTI, +CLIP, +CUSD, ...) -> `urc_callback`,
    - everything else -> response queue of the pending command.
    """

    def __init__(self, port: str, baudrate: int, debug: bool = False) -> None:
        """Initialize the modem interface."""
        self.port = port
        self.baudrate = baudrate
        self.debug = debug
        self.urc_callback: UrcCallback | None = None
        self.prompt_timeout = SMS_PROMPT_TIMEOUT
        self.send_timeout = SMS_SEND_TIMEOUT
        self.ussd_timeout = USSD_REPLY_TIMEOUT
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._ussd_lock = asyncio.Lock()
        self._ussd_future: asyncio.Future[CusdReply] | None = None
        self._closed = False
        self._resync = False
        self._consecutive_timeouts = 0
        self._pending_prefix: str | None = None
        self._expect_body = False
        self._expect_prompt = False
        self._urc_partial: str | None = None
        self._urc_body_for: str | None = None
        self._sms_reference = itertools.count(int(time.time()) & 0xFF)
        self._assembler = SmsAssembler()

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """Return True while the serial port is open."""
        return self._writer is not None

    @property
    def has_pending_sms(self) -> bool:
        """Return True while incomplete multipart SMS wait for more parts."""
        return self._assembler.has_pending

    async def connect(self) -> None:
        """Open the serial port and initialise the modem."""
        if self._closed:
            raise ModemConnectionError(f"Modem on {self.port} was closed")
        if self._writer is not None:
            return
        try:
            reader, writer = await serial_asyncio_fast.open_serial_connection(
                url=self.port, baudrate=self.baudrate
            )
        except (serial.SerialException, OSError) as err:
            LOGGER.error("Failed to connect to SIM7600 on %s: %s", self.port, err)
            raise ModemConnectionError(str(err)) from err

        self._reader, self._writer = reader, writer
        self._queue = asyncio.Queue()
        self._resync = False
        self._consecutive_timeouts = 0
        self._expect_body = False
        self._expect_prompt = False
        self._urc_partial = None
        self._urc_body_for = None
        self._read_task = asyncio.get_running_loop().create_task(
            self._read_loop(reader), name=f"sim7600_reader_{self.port}"
        )
        for command in INIT_COMMANDS:
            lines = await self._execute(command, INIT_TIMEOUT, count_timeout=False)
            if not lines or lines[-1] != "OK":
                LOGGER.debug("Init command %s answered %s", command, lines)

    async def disconnect(self) -> None:
        """Close the serial port. A later command reconnects."""
        task, self._read_task = self._read_task, None
        writer = self._writer
        self._reader = None
        self._writer = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def close(self) -> None:
        """Close the port permanently (config entry unloaded)."""
        self._closed = True
        if self._ussd_future is not None and not self._ussd_future.done():
            self._ussd_future.set_exception(ModemConnectionError("Modem closed"))
        await self.disconnect()

    def _handle_connection_lost(
        self,
        err: BaseException | None,
        reader: asyncio.StreamReader | None = None,
    ) -> None:
        """Reset the transport so the next command reconnects.

        `reader` is set when called from a reader task; a stale task of an
        already replaced connection must not tear down the new one.
        """
        if reader is not None and reader is not self._reader:
            return
        if self._writer is None:
            return
        LOGGER.warning("Serial connection to %s lost: %s", self.port, err)
        writer = self._writer
        task = self._read_task
        self._reader = None
        self._writer = None
        self._read_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
        self._queue.put_nowait(("lost", str(err)))
        if self._ussd_future is not None and not self._ussd_future.done():
            self._ussd_future.set_exception(ModemConnectionError(str(err)))

    # ------------------------------------------------------------------
    # Reader task and line classification
    # ------------------------------------------------------------------

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        buffer = b""
        try:
            while True:
                chunk = await reader.read(1024)
                if not chunk:
                    raise ModemConnectionError("Serial port closed")
                buffer = self._process_buffer(buffer + chunk)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - any read failure means reconnect
            self._handle_connection_lost(err, reader)

    def _process_buffer(self, buffer: bytes) -> bytes:
        while b"\n" in buffer:
            raw, buffer = buffer.split(b"\n", 1)
            self._handle_line(raw.decode(errors="replace").rstrip("\r"))
        if self._expect_prompt and buffer.strip() == b">":
            self._expect_prompt = False
            self._queue.put_nowait(("prompt", ">"))
            return b""
        return buffer

    def _handle_line(self, line: str) -> None:
        if self.debug:
            LOGGER.log(5, "Trace: raw line: %r", line)
        stripped = line.strip()

        if self._expect_body:
            self._expect_body = False
            self._queue.put_nowait(("body", stripped))
            return
        if self._urc_body_for is not None:
            header, self._urc_body_for = self._urc_body_for, None
            self._dispatch_urc(f"{header}\n{stripped}")
            return
        if self._urc_partial is not None:
            self._urc_partial = f"{self._urc_partial}\n{line}"
            if self._urc_partial.count('"') % 2 == 0:
                partial, self._urc_partial = self._urc_partial, None
                self._dispatch_urc(partial)
            return
        if not stripped:
            return
        if stripped[:2].upper() == "AT":
            return  # command echo
        if self._is_urc(stripped):
            if stripped.startswith(URC_WITH_BODY):
                self._urc_body_for = stripped
            elif stripped.startswith("+CUSD:") and stripped.count('"') % 2:
                self._urc_partial = stripped
            else:
                self._dispatch_urc(stripped)
            return
        if stripped.startswith(BODY_HEADERS):
            self._expect_body = True
        self._queue.put_nowait(("line", stripped))

    def _is_urc(self, line: str) -> bool:
        pending = self._pending_prefix
        if line.startswith(AMBIGUOUS_URC_PREFIXES) and pending is not None:
            if line.startswith(pending):
                return False
        if line.startswith(ALWAYS_URC_PREFIXES):
            return True
        return line.startswith(AMBIGUOUS_URC_PREFIXES)

    def _dispatch_urc(self, line: str) -> None:
        LOGGER.debug("URC: %s", line)
        if line.startswith("+CUSD:"):
            reply = parse_cusd(line)
            future = self._ussd_future
            if reply is not None and future is not None and not future.done():
                future.set_result(reply)
        if self.urc_callback is not None:
            try:
                self.urc_callback(line)
            except Exception:  # noqa: BLE001 - never break the reader
                LOGGER.exception("Error in SIM7600 URC callback")

    # ------------------------------------------------------------------
    # Command execution (callers must hold self._lock)
    # ------------------------------------------------------------------

    def _flush_input(self) -> None:
        """Drop stale response lines (late replies of timed-out commands)."""
        while True:
            try:
                kind, line = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if kind == "lost":
                raise ModemConnectionError(line)
            LOGGER.debug("Discarding stale modem line: %s", line)

    async def _settle(self) -> None:
        """After a timeout wait until the modem stops talking, then flush."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SETTLE_MAX
        while loop.time() < deadline:
            try:
                kind, line = await asyncio.wait_for(self._queue.get(), SETTLE_QUIET)
            except TimeoutError:
                break
            if kind == "lost":
                raise ModemConnectionError(line)
            LOGGER.debug("Discarding late modem line: %s", line)
        self._resync = False

    async def _write(self, data: bytes) -> None:
        writer = self._writer
        if writer is None:
            raise ModemConnectionError("Serial port is not open")
        try:
            writer.write(data)
            await writer.drain()
        except (serial.SerialException, OSError) as err:
            self._handle_connection_lost(err)
            raise ModemConnectionError(str(err)) from err

    async def _prepare(self) -> None:
        await self.connect()
        if self._resync:
            await self._settle()
        self._flush_input()

    async def _register_timeout(self, what: str, count_timeout: bool) -> None:
        self._resync = True
        LOGGER.warning("Timeout waiting for response to: %s", what)
        if not count_timeout:
            return
        self._consecutive_timeouts += 1
        if self._consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
            count = self._consecutive_timeouts
            self._consecutive_timeouts = 0
            await self.disconnect()
            raise ModemTimeoutError(
                f"SIM7600 on {self.port} did not answer {count} commands in a row"
            )

    async def _collect(
        self,
        what: str,
        timeout: float,  # noqa: ASYNC109
        count_timeout: bool = True,
    ) -> list[str]:
        """Read lines until a final result code or timeout."""
        lines: list[str] = []
        try:
            async with asyncio.timeout(timeout):
                while True:
                    kind, line = await self._queue.get()
                    if kind == "lost":
                        raise ModemConnectionError(line)
                    if kind == "prompt":
                        continue
                    lines.append(line)
                    if kind == "line" and is_final_result(line):
                        self._consecutive_timeouts = 0
                        return lines
        except TimeoutError:
            await self._register_timeout(what, count_timeout)
        return lines

    async def _execute(
        self,
        command: str,
        timeout: float,  # noqa: ASYNC109
        *,
        count_timeout: bool = True,
    ) -> list[str]:
        await self._prepare()
        match = _PENDING_PREFIX_RE.match(command)
        self._pending_prefix = f"{match.group(1).upper()}:" if match else None
        try:
            LOGGER.debug("Sending AT command: %s", command)
            await self._write(f"{command}\r".encode())
            lines = await self._collect(command, timeout, count_timeout)
        finally:
            self._pending_prefix = None
            self._expect_body = False
        LOGGER.debug("Response: %s", lines)
        return lines

    async def send_command(
        self,
        command: str,
        timeout: float = DEFAULT_TIMEOUT,  # noqa: ASYNC109
    ) -> list[str]:
        """Send an AT command and return its response lines.

        A single timeout returns the lines received so far. After
        MAX_CONSECUTIVE_TIMEOUTS timeouts in a row ModemTimeoutError is raised
        and the port is reopened on the next command.
        """
        async with self._lock:
            return await self._execute(command, timeout)

    # ------------------------------------------------------------------
    # Information queries
    # ------------------------------------------------------------------

    async def get_signal_quality(self) -> int | None:
        """Get RSSI signal quality (0-31 or 99)."""
        lines = await self.send_command("AT+CSQ")
        for line in lines:
            if match := re.search(r"\+CSQ:\s*(\d+),", line):
                rssi = int(match.group(1))
                return rssi if rssi != 99 else None
        return None

    async def get_operator(self) -> str | None:
        """Get the current operator name."""
        lines = await self.send_command("AT+COPS?")
        for line in lines:
            if match := re.search(r'\+COPS:\s*\d+,\d+,"([^"]+)"', line):
                return match.group(1)
        return None

    async def get_network_info(self) -> dict[str, Any]:
        """Get detailed network info via AT+CPSI?."""
        lines = await self.send_command("AT+CPSI?")
        info: dict[str, Any] = {}
        for line in lines:
            if line.startswith("+CPSI:"):
                parts = line.replace("+CPSI:", "").strip().split(",")
                if len(parts) >= 2:
                    info["mode"] = parts[0]
                    info["system_mode"] = parts[1]
                if len(parts) >= 3:
                    info["mcc_mnc"] = parts[2]
                break
        return info

    async def get_imei(self) -> str | None:
        """Get the modem IMEI."""
        lines = await self.send_command("AT+CGSN")
        for line in lines:
            if re.match(r"^\d{15}$", line):
                return line
        return None

    async def _get_single_value(self, command: str) -> str | None:
        lines = await self.send_command(command)
        if lines and is_error_result(lines[-1]):
            return None
        for line in lines:
            if line and not is_final_result(line):
                return line
        return None

    async def get_firmware(self) -> str | None:
        """Get the firmware version."""
        value = await self._get_single_value("AT+CGMR")
        return (
            value.replace("+CGMR:", "").replace("Revision:", "").strip()
            if value
            else None
        )

    async def get_manufacturer(self) -> str | None:
        """Get the modem manufacturer."""
        return await self._get_single_value("AT+CGMI")

    async def get_model(self) -> str | None:
        """Get the modem model."""
        return await self._get_single_value("AT+CGMM")

    async def get_sim_status(self) -> str | None:
        """Get SIM card status via AT+CPIN?."""
        lines = await self.send_command("AT+CPIN?")
        for line in lines:
            if match := re.search(r"\+CPIN:\s*(.+)", line):
                return match.group(1).strip()
        return None

    async def get_registration_status(self) -> int | None:
        """Get network registration status."""
        lines = await self.send_command("AT+CREG?")
        for line in lines:
            if match := re.search(r"\+CREG:\s*\d+,(\d+)", line):
                return int(match.group(1))
        return None

    async def get_gprs_registration_status(self) -> int | None:
        """Get GPRS network registration status."""
        lines = await self.send_command("AT+CGREG?")
        for line in lines:
            if match := re.search(r"\+CGREG:\s*\d+,(\d+)", line):
                return int(match.group(1))
        return None

    # ------------------------------------------------------------------
    # SMS
    # ------------------------------------------------------------------

    async def send_sms(self, number: str, message: str) -> None:
        """Send an SMS (PDU mode, UCS2 when needed, multipart when long).

        Raises InvalidInputError, ModemCommandError, ModemTimeoutError or
        ModemConnectionError.
        """
        number = number.strip()
        if not is_valid_phone_number(number):
            raise InvalidInputError(f"Invalid phone number: {number!r}")
        text = sanitize_sms_text(message)
        if not text:
            raise InvalidInputError("SMS text is empty")
        try:
            pdus = build_submit_pdus(number, text, next(self._sms_reference))
        except ValueError as err:
            raise InvalidInputError(str(err)) from err

        async with self._lock:
            lines = await self._execute("AT+CMGF=0", DEFAULT_TIMEOUT)
            if not lines or lines[-1] != "OK":
                raise ModemCommandError(f"Cannot enable PDU mode: {lines}")
            LOGGER.debug("Sending SMS in %d part(s)", len(pdus))
            for part, pdu in enumerate(pdus, start=1):
                await self._send_pdu(pdu.tpdu_length, pdu.hex, part, len(pdus))

    async def _wait_prompt(self) -> tuple[str, list[str]]:
        lines: list[str] = []
        try:
            async with asyncio.timeout(self.prompt_timeout):
                while True:
                    kind, line = await self._queue.get()
                    if kind == "lost":
                        raise ModemConnectionError(line)
                    if kind == "prompt":
                        return "prompt", lines
                    lines.append(line)
                    if kind == "line" and is_final_result(line):
                        return "final", lines
        except TimeoutError:
            return "timeout", lines

    async def _send_pdu(self, length: int, pdu_hex: str, part: int, total: int) -> None:
        await self._prepare()
        self._expect_prompt = True
        self._pending_prefix = "+CMGS:"
        try:
            await self._write(f"AT+CMGS={length}\r".encode())
            result, lines = await self._wait_prompt()
            if result == "timeout":
                # Abort the pending input state so the modem accepts commands.
                await self._write(b"\x1b")
                self._resync = True
                raise ModemTimeoutError(f"No SMS prompt for part {part}/{total}")
            if result == "final":
                raise ModemCommandError(f"Modem rejected AT+CMGS: {lines}")
            self._expect_prompt = False
            await self._write(pdu_hex.encode() + b"\x1a")
            lines = await self._collect(
                f"SMS part {part}/{total}", self.send_timeout, count_timeout=False
            )
        finally:
            self._expect_prompt = False
            self._pending_prefix = None
        if not lines or not is_final_result(lines[-1]):
            raise ModemTimeoutError(f"No confirmation for SMS part {part}/{total}")
        if lines[-1] != "OK":
            raise ModemCommandError(f"Sending SMS part {part}/{total} failed: {lines}")

    async def get_unread_sms(self) -> list[SmsData]:
        """Return new messages; multipart messages are joined.

        Uses AT+CMGL=4 (all, PDU mode). Listing marks unread messages as read;
        the assembler remembers incomplete multipart groups between calls.
        """
        await self.send_command("AT+CMGF=0")
        lines = await self.send_command("AT+CMGL=4", timeout=CMGL_TIMEOUT)
        if not lines or lines[-1] != "OK":
            LOGGER.debug("Listing SMS failed: %s", lines)
            return []
        fragments = parse_cmgl_pdu(lines)
        return self._assembler.assemble(fragments, time.time())

    async def delete_sms(self, index: int) -> bool:
        """Delete an SMS from modem storage by index."""
        lines = await self.send_command(f"AT+CMGD={index},0")
        if "OK" in lines:
            return True
        LOGGER.warning("Failed to delete SMS %s from modem storage: %s", index, lines)
        return False

    async def delete_read_sms(self) -> bool:
        """Delete all read messages (AT+CMGD=1,1)."""
        lines = await self.send_command("AT+CMGD=1,1", timeout=15)
        if "OK" in lines:
            return True
        LOGGER.warning("Failed to delete read SMS from modem storage: %s", lines)
        return False

    # ------------------------------------------------------------------
    # USSD
    # ------------------------------------------------------------------

    async def send_ussd(self, code: str) -> CusdReply:
        """Send a USSD request and wait for the +CUSD reply."""
        code = code.strip()
        if not USSD_CODE_RE.fullmatch(code):
            raise InvalidInputError(f"Invalid USSD code: {code!r}")
        async with self._ussd_lock:
            future: asyncio.Future[CusdReply] = (
                asyncio.get_running_loop().create_future()
            )
            self._ussd_future = future
            try:
                lines = await self.send_command(f'AT+CUSD=1,"{code}",15', timeout=10)
                if (not lines or lines[-1] != "OK") and not future.done():
                    raise UssdError(f"Modem rejected USSD request: {lines}")
                try:
                    reply = await asyncio.wait_for(future, self.ussd_timeout)
                except TimeoutError as err:
                    await self.send_command("AT+CUSD=2")
                    raise UssdError("No USSD reply from the network") from err
            finally:
                self._ussd_future = None
            if reply.status == 1:
                # Network waits for user input; close the session.
                await self.send_command("AT+CUSD=2")
            if reply.status >= 2 and reply.message is None:
                raise UssdError(f"USSD request failed with status {reply.status}")
            return reply

    # ------------------------------------------------------------------
    # GPS
    # ------------------------------------------------------------------

    async def set_gps(self, enable: bool) -> bool:
        """Enable or disable GPS.

        ERROR often means GPS is already enabled -> True.
        No OK and no ERROR (timeout) -> one retry after 500 ms.
        """
        cmd = f"AT+CGPS={1 if enable else 0}"
        lines = await self.send_command(cmd)
        if "OK" not in lines and "ERROR" not in lines:
            await asyncio.sleep(0.5)
            lines = await self.send_command(cmd)
        return "OK" in lines or "ERROR" in lines

    async def get_gps_info(self) -> GpsData | None:
        """Get GPS location information (speed in knots).

        All fields are mandatory; a partial fix returns None.
        """
        lines = await self.send_command("AT+CGPSINFO")
        for line in lines:
            if not line.startswith("+CGPSINFO:"):
                continue
            content = line.replace("+CGPSINFO:", "").strip()
            if not content or content == ",,,,,,,,":
                return None
            parts = content.split(",")
            if len(parts) < 9:
                return None
            lat_raw, lat_dir, lon_raw, lon_dir, date_str, time_str = parts[:6]
            alt_str, speed_str = parts[6], parts[7]
            required = [lat_raw, lat_dir, lon_raw, lon_dir, date_str, time_str]
            if not all(required + [alt_str, speed_str]):
                return None
            try:
                latitude = float(lat_raw[:2]) + float(lat_raw[2:]) / 60.0
                if lat_dir == "S":
                    latitude = -latitude
                longitude = float(lon_raw[:3]) + float(lon_raw[3:]) / 60.0
                if lon_dir == "W":
                    longitude = -longitude
                return GpsData(
                    latitude=latitude,
                    longitude=longitude,
                    altitude=float(alt_str),
                    speed=float(speed_str),
                    date=date_str,
                    time=time_str,
                    fix_time=time.time(),
                )
            except (ValueError, IndexError) as err:
                LOGGER.debug("Error parsing GPS data: %s", err)
                return None
        return None
