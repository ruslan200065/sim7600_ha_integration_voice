"""Regression tests for serial disconnects and call command framing."""

import asyncio
import pathlib
import queue
import sys
import unittest
from unittest.mock import AsyncMock, patch

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

    async def test_extended_voice_ring_and_international_caller_id(self):
        await self.modem._urc("+CRING: GPRS")
        self.assertEqual(self.modem.call_state, "idle")
        await self.modem._urc("+CRING:VOICE")
        self.assertEqual(self.modem.call_state, "ringing")
        await self.modem._urc('+CLIP: "79991234567",145,,,0')
        self.assertEqual(self.modem.active_number, "+79991234567")
        await self.modem.answer()
        await self.modem._urc("VOICE CALL:BEGIN")
        self.assertEqual(self.modem.call_state, "connected")
        await self.modem._urc("VOICE CALL:END")
        self.assertEqual(self.modem.call_state, "idle")

    async def test_unsolicited_call_status_recovers_missing_voice_events(self):
        self.modem.on_event = AsyncMock()
        self.port.reply = [
            b'+CLCC: 1,1,4,0,0,"79991234567",145\r\n',
            b'RING\r\n',
            b'+CLIP: "+79991234567",145\r\n',
            b'+CSQ: 20,99\r\n',
            b'OK\r\n',
        ]
        self.assertEqual(await self.modem.command("AT+CSQ"), ["+CSQ: 20,99", "OK"])
        self.assertEqual(self.modem.call_state, "ringing")
        self.assertEqual(self.modem.active_number, "+79991234567")
        self.assertEqual(self.modem.call_id, 1)
        self.port.reply = [b"+CLCC: 1,1,0,0,0\r\n", b"VOICE CALL: BEGIN\r\n", b"OK\r\n"]
        await self.modem.answer()
        self.assertEqual(self.modem.call_state, "connected")
        self.assertEqual(
            [item.args[0] for item in self.modem.on_event.call_args_list],
            ["incoming_call", "caller_id", "call_connected"],
        )

    async def test_audio_registration_can_follow_call_connection(self):
        self.modem.call_state = "connected"
        self.modem.command = AsyncMock(side_effect=[["OK"], ["ERROR"], ["OK"], ["+CPCMFRM: 1", "OK"]])
        await self.modem.enable_pcm()
        self.assertEqual(
            [entry.args[0] for entry in self.modem.command.call_args_list],
            ["AT+CPCMFRM=1", "AT+CPCMREG=1", "AT+CPCMREG=1", "AT+CPCMFRM?"],
        )
        self.assertTrue(all(
            entry.kwargs["expected_call"] == (self.modem.call_id, "connected")
            for entry in self.modem.command.call_args_list
        ))

    async def test_phone_diagnostics_expose_ims_support_without_cell_identifiers(self):
        async def command(cmd, **_kwargs):
            return {
                "AT+CPIN?": ["+CME ERROR: SIM not inserted"],
                "AT+CPSI?": ["+CPSI: LTE,Online,250-01,0x1234", "OK"],
                "AT+COPS?": ['+COPS: 0,0,"Operator",7', "OK"],
                "AT+CIREG?": ["+CIREG: 0,1", "OK"],
                "AT+VOLTESETTING?": ["ERROR"],
            }.get(cmd, ["OK"])

        self.modem.command = AsyncMock(side_effect=command)
        diagnostics = await self.modem.phone_diagnostics()
        self.assertEqual(diagnostics["SIM"], "+CME ERROR: SIM not inserted")
        self.assertEqual(diagnostics["CPSI"], "LTE,Online")
        self.assertEqual(diagnostics["COPS"], '0,0,"Operator",7')
        self.assertEqual(diagnostics["IMS"], "0,1")
        self.assertEqual(diagnostics["VoLTE"], "ERROR")

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
