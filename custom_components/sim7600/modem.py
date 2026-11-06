"""Modem communication layer for SIM7600."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import serial_asyncio_fast

from .const import LOGGER
from .types import GpsData, SmsData


class SIM7600Modem:
    """Interface to communicate with SIM7600 via AT commands."""

    def __init__(self, port: str, baudrate: int, debug: bool = False) -> None:
        """Initialize the modem interface."""
        self.port = port
        self.baudrate = baudrate
        self.debug = debug
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Connect to the modem."""
        if self._reader is None:
            try:
                (
                    self._reader,
                    self._writer,
                ) = await serial_asyncio_fast.open_serial_connection(
                    url=self.port, baudrate=self.baudrate
                )
            except Exception as err:
                LOGGER.error("Failed to connect to SIM7600 on %s: %s", self.port, err)
                raise

    async def disconnect(self) -> None:
        """Disconnect from the modem."""
        if self._writer is not None:
            self._writer.close()
            await self._writer.wait_closed()
            self._reader = None
            self._writer = None

    async def send_command(
        self,
        command: str,
        timeout: float = 5.0,  # noqa: ASYNC109
    ) -> list[str]:
        """Send an AT command and return the response lines."""
        async with self._lock:
            await self.connect()
            if self._writer is None or self._reader is None:
                return []

            LOGGER.debug("Sending AT command: %s", command)
            self._writer.write(f"{command}\r\n".encode())
            await self._writer.drain()

            lines = []
            try:
                async with asyncio.timeout(timeout):
                    while line_bytes := await self._reader.readline():
                        line = line_bytes.decode(errors="replace").strip()
                        if self.debug:
                            LOGGER.log(5, "Trace: Raw line: %s", line)
                        if not line:
                            continue
                        lines.append(line)
                        if line in ("OK", "ERROR") or line.startswith(
                            ("+CME ERROR:", "+CMS ERROR:")
                        ):
                            break
            except TimeoutError:
                LOGGER.warning("Timeout waiting for response to: %s", command)

            LOGGER.debug("Response: %s", lines)
            return lines

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
                parts = line.replace("+CPSI: ", "").split(",")
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

    async def get_firmware(self) -> str | None:
        """Get the firmware version."""
        lines = await self.send_command("AT+CGMR")
        for line in lines:
            if line and line not in ("OK", "ERROR"):
                return line.replace("Revision:", "").strip()
        return None

    async def get_manufacturer(self) -> str | None:
        """Get the modem manufacturer."""
        lines = await self.send_command("AT+CGMI")
        for line in lines:
            if line and line not in ("OK", "ERROR"):
                return line
        return None

    async def get_model(self) -> str | None:
        """Get the modem model."""
        lines = await self.send_command("AT+CGMM")
        for line in lines:
            if line and line not in ("OK", "ERROR"):
                return line
        return None

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

    async def send_sms(self, number: str, message: str) -> bool:
        """Send an SMS message."""
        async with self._lock:
            await self.connect()
            if self._writer is None or self._reader is None:
                return False

            # Set text mode
            self._writer.write(b"AT+CMGF=1\r\n")
            await self._writer.drain()
            try:
                await asyncio.wait_for(self._reader.readuntil(b"OK\r\n"), timeout=5)
            except (TimeoutError, asyncio.IncompleteReadError):
                LOGGER.warning("No response to SMS text mode setup")
                return False

            # Start SMS
            LOGGER.debug("Sending SMS to %s", number)
            self._writer.write(f'AT+CMGS="{number}"\r\n'.encode())
            await self._writer.drain()

            # Wait for >
            try:
                response = await asyncio.wait_for(
                    self._reader.readuntil(b"> "), timeout=5
                )
                if b"> " not in response:
                    return False
            except (TimeoutError, asyncio.IncompleteReadError):
                LOGGER.warning("Timeout waiting for SMS prompt")
                return False

            # Send message body and Ctrl+Z
            self._writer.write(f"{message}\x1a".encode())
            await self._writer.drain()

            # Wait for OK
            try:
                async with asyncio.timeout(10):
                    while line_bytes := await self._reader.readline():
                        line = line_bytes.decode(errors="replace").strip()
                        if line == "OK":
                            return True
                        if line == "ERROR" or line.startswith(
                            ("+CME ERROR:", "+CMS ERROR:")
                        ):
                            return False
            except TimeoutError:
                LOGGER.warning("Timeout waiting for SMS confirmation")
            return False

    async def get_unread_sms(self) -> list[SmsData]:
        """Check for unread SMS messages and return them with their storage index."""
        await self.send_command("AT+CMGF=1")
        lines = await self.send_command('AT+CMGL="REC UNREAD"')

        messages: list[SmsData] = []
        pattern = r'\+CMGL:\s*(\d+),"[^"]+","([^"]+)",[^,]*,"([^"]+)"'
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("+CMGL:"):
                match = re.search(pattern, line)
                if match and i + 1 < len(lines):
                    index = int(match.group(1))
                    sender = match.group(2)
                    timestamp = match.group(3)
                    body = lines[i + 1]
                    messages.append(
                        SmsData(
                            index=index,
                            sender=sender,
                            timestamp=timestamp,
                            message=body,
                        )
                    )
                    i += 1
            i += 1
        return messages

    async def delete_sms(self, index: int) -> bool:
        """Delete an SMS from modem storage by index."""
        lines = await self.send_command(f"AT+CMGD={index},0")
        return "OK" in lines

    async def set_gps(self, enable: bool) -> bool:
        """Enable or disable GPS.

        ERROR-Antwort bedeutet oft, dass GPS bereits aktiv ist → True.
        Kein OK und kein ERROR (Timeout) → einmaliger Retry nach 500 ms.
        """
        cmd = f"AT+CGPS={1 if enable else 0}"
        lines = await self.send_command(cmd)
        if "OK" not in lines and "ERROR" not in lines:
            await asyncio.sleep(0.5)
            lines = await self.send_command(cmd)
        return "OK" in lines or "ERROR" in lines

    async def get_gps_info(self) -> GpsData | None:
        """Get GPS location information.

        Alle Felder (lat, lon, altitude, speed, date, time) sind verpflichtend.
        Fehlt ein Feld → None zurückgeben, kein partielles GpsData-Objekt.
        """
        lines = await self.send_command("AT+CGPSINFO")
        LOGGER.debug("GPS info response: %s", lines)
        for line in lines:
            if line.startswith("+CGPSINFO:"):
                content = (
                    line.replace("+CGPSINFO: ", "").replace("+CGPSINFO:", "").strip()
                )
                if not content or content == ",,,,,,,,":
                    return None

                parts = content.split(",")
                LOGGER.debug("GPS parts: %s", parts)
                if len(parts) < 9:
                    return None

                try:
                    lat_raw = parts[0]
                    lat_dir = parts[1]
                    lon_raw = parts[2]
                    lon_dir = parts[3]
                    date_str = parts[4]
                    time_str = parts[5]
                    alt_str = parts[6]
                    speed_str = parts[7]

                    # Alle Pflichtfelder müssen vorhanden sein
                    required = [
                        lat_raw,
                        lat_dir,
                        lon_raw,
                        lon_dir,
                        date_str,
                        time_str,
                        alt_str,
                        speed_str,
                    ]
                    if not all(required):
                        return None

                    # Latitude: ddmm.mmmmmm → Dezimalgrad
                    lat_deg = float(lat_raw[:2])
                    lat_min = float(lat_raw[2:])
                    latitude = lat_deg + (lat_min / 60.0)
                    if lat_dir == "S":
                        latitude = -latitude

                    # Longitude: dddmm.mmmmmm → Dezimalgrad
                    lon_deg = float(lon_raw[:3])
                    lon_min = float(lon_raw[3:])
                    longitude = lon_deg + (lon_min / 60.0)
                    if lon_dir == "W":
                        longitude = -longitude

                    altitude = float(alt_str)
                    speed = float(speed_str)

                    return GpsData(
                        latitude=latitude,
                        longitude=longitude,
                        altitude=altitude,
                        speed=speed,
                        date=date_str,
                        time=time_str,
                    )
                except (ValueError, IndexError) as err:
                    LOGGER.debug("Error parsing GPS data: %s", err)
                    return None
        return None
