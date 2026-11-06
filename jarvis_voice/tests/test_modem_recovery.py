"""Regression tests for serial disconnects and call command framing."""

import asyncio
import pathlib
import queue
import sys
import unittest
from unittest.mock import patch

import serial

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from modem import Modem


class FakePort:
    def __init__(self):
        self.lines = queue.Queue()
        self.is_open = True
        self.reply = [b"OK\r\n"]

    def write(self, data):
        for line in self.reply:
            self.lines.put(line)
        return len(data)

    def readline(self):
        if not self.is_open:
            raise serial.SerialException("Closed")
        try:
            line = self.lines.get(timeout=0.02)
        except queue.Empty:
            return b""
        if isinstance(line, Exception):
            raise line
        return line

    def close(self):
        self.is_open = False


class ModemTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.port = FakePort()
        self.modem = Modem("/dev/mock")
        self.modem.serial = self.port
        self.modem.call_state = "idle"
        self.reader = asyncio.create_task(self.modem._reader())

    async def asyncTearDown(self):
        self.reader.cancel()
        await asyncio.gather(self.reader, return_exceptions=True)
        self.port.close()

    async def test_busy_completes_dial_and_clears_caller(self):
        self.port.reply = [b"BUSY\r\n"]
        async with asyncio.timeout(1):
            with self.assertRaisesRegex(RuntimeError, "Dial failed: BUSY"):
                await self.modem.dial("+79991234567")
        self.assertEqual(self.modem.call_state, "idle")
        self.assertEqual(self.modem.active_number, "")

    async def test_call_end_is_not_gps_response_and_fragments_are_joined(self):
        self.modem.call_state = "connected"
        self.port.reply = [b"NO CARRIER\r\n", b"+CG", b"PS: 1,2\r\n", b"OK\r\n"]
        self.assertEqual(await self.modem.command("AT+CGPS?", timeout=1), ["+CGPS: 1,2", "OK"])
        self.assertEqual(self.modem.call_state, "idle")

    async def test_timeout_closes_out_of_sync_port(self):
        self.port.reply = []
        with self.assertRaises(TimeoutError):
            await self.modem.command("AT", timeout=0.05)
        with self.assertRaises(ConnectionError):
            await self.modem.command("AT")

    async def test_stale_answer_is_never_sent(self):
        self.modem.call_state = "ringing"
        async with self.modem.lock:
            answer = asyncio.create_task(self.modem.answer())
            await asyncio.sleep(0)
            await self.modem._urc("NO CARRIER")
            await self.modem._urc("RING")
        with self.assertRaisesRegex(RuntimeError, "Call state changed"):
            await answer
        self.assertTrue(self.port.lines.empty())

    async def test_read_failure_reopens_port(self):
        ports = [FakePort(), FakePort()]
        opened_twice = asyncio.Event()
        connected = 0

        async def event(name, _data):
            nonlocal connected
            if name == "modem_connected":
                connected += 1
                if connected == 2:
                    opened_twice.set()
            elif name == "modem_offline":
                self.assertEqual(modem.call_state, "offline")
                self.assertFalse(ports[0].is_open)

        modem = Modem("/dev/mock", on_event=event)
        with patch("modem.serial.Serial", side_effect=ports):
            await modem.start()
            try:
                async with asyncio.timeout(10):
                    while not connected:
                        await asyncio.sleep(0.01)
                    ports[0].lines.put(serial.SerialException("USB disconnected"))
                    await opened_twice.wait()
                self.assertIs(modem.serial, ports[1])
                self.assertEqual(modem.call_state, "idle")
            finally:
                await modem.stop()
