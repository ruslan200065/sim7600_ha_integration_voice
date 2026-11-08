"""Protocol tests for SIM7600Modem with a fake serial port (no HA needed)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import patch

import pytest
import serial
from pdu_helpers import make_deliver_pdu

from custom_components.sim7600 import modem as modem_module
from custom_components.sim7600.modem import (
    InvalidInputError,
    ModemCommandError,
    ModemConnectionError,
    ModemTimeoutError,
    SIM7600Modem,
    UssdError,
)
from custom_components.sim7600.sms_codec import ucs2_hex_encode

Responder = Callable[["FakeSerial", bytes], None]


class FakeWriter:
    """StreamWriter stand-in that hands written data to the fake modem."""

    def __init__(self, port: FakeSerial) -> None:
        self.port = port
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.port.fail_writes:
            raise serial.SerialException("device disconnected")
        self.port.on_write(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True
        self.port.reader.feed_eof()

    async def wait_closed(self) -> None:
        return None


class FakeSerial:
    """Scripted modem. `responses` maps a command to its reply (or callable)."""

    def __init__(self) -> None:
        self.reader = asyncio.StreamReader()
        self.writer = FakeWriter(self)
        self.written: list[bytes] = []
        self.commands: list[str] = []
        self.responses: dict[str, object] = {}
        self.echo = False
        self.fail_writes = False
        self._buffer = b""

    def feed(self, text: str) -> None:
        self.reader.feed_data(text.encode())

    def on_write(self, data: bytes) -> None:
        self.written.append(data)
        self._buffer += data
        while True:
            if b"\x1a" in self._buffer:
                body, self._buffer = self._buffer.split(b"\x1a", 1)
                self._reply("<CTRL-Z>", body.decode())
                continue
            if b"\x1b" in self._buffer:
                _, self._buffer = self._buffer.split(b"\x1b", 1)
                self.commands.append("<ESC>")
                continue
            if b"\r" in self._buffer:
                line, self._buffer = self._buffer.split(b"\r", 1)
                command = line.decode().strip()
                if not command:
                    continue
                self.commands.append(command)
                if self.echo:
                    self.feed(f"{command}\r\n")
                self._reply(command, command)
                continue
            return

    def _reply(self, key: str, payload: str) -> None:
        reply = self.responses.get(key)
        if reply is None and key != "<CTRL-Z>":
            reply = self.responses.get("*", "\r\nOK\r\n")
        if callable(reply):
            reply = reply(payload)
        if reply:
            self.feed(str(reply))


@pytest.fixture
async def fake() -> FakeSerial:
    """A fresh fake modem (async so the StreamReader binds to the test loop)."""
    return FakeSerial()


@pytest.fixture
async def modem(fake: FakeSerial):
    """Modem connected to the fake port, with short protocol timeouts."""

    async def _open(**_kwargs: object):
        return fake.reader, fake.writer

    with (
        patch.object(modem_module.serial_asyncio_fast, "open_serial_connection", _open),
        patch.object(modem_module, "SETTLE_QUIET", 0.02),
        patch.object(modem_module, "SETTLE_MAX", 0.1),
    ):
        instance = SIM7600Modem("/dev/ttyUSB2", 115200)
        instance.prompt_timeout = 0.2
        instance.send_timeout = 0.5
        instance.ussd_timeout = 0.3
        yield instance
        await instance.close()


# --- Connect / echo ---


async def test_connect_disables_echo(modem: SIM7600Modem, fake: FakeSerial) -> None:
    """ATE0 is the first command after opening the port."""
    await modem.connect()
    assert fake.commands[0] == "ATE0"
    assert "AT+CMGF=0" in fake.commands
    assert "AT+CNMI=2,1,0,0,0" in fake.commands


async def test_echo_lines_are_skipped(modem: SIM7600Modem, fake: FakeSerial) -> None:
    """Echoed command lines never show up in the response."""
    fake.echo = True
    fake.responses["AT+CSQ"] = "\r\n+CSQ: 20,99\r\n\r\nOK\r\n"
    assert await modem.send_command("AT+CSQ") == ["+CSQ: 20,99", "OK"]


# --- URC routing ---


async def test_urcs_are_routed_to_callback(
    modem: SIM7600Modem, fake: FakeSerial
) -> None:
    """RING, +CLIP and +CMTI inside a response go to the URC callback."""
    urcs: list[str] = []
    modem.urc_callback = urcs.append
    fake.responses["AT+CSQ"] = (
        '\r\nRING\r\n\r\n+CLIP: "+79991234567",145,,,,0\r\n'
        '+CSQ: 18,99\r\n+CMTI: "SM",3\r\n\r\nOK\r\n'
    )
    assert await modem.send_command("AT+CSQ") == ["+CSQ: 18,99", "OK"]
    assert urcs == ["RING", '+CLIP: "+79991234567",145,,,,0', '+CMTI: "SM",3']


async def test_urc_between_commands(modem: SIM7600Modem, fake: FakeSerial) -> None:
    """A URC arriving while idle is dispatched and not queued as response."""
    urcs: list[str] = []
    modem.urc_callback = urcs.append
    await modem.connect()
    fake.feed('\r\n+CMTI: "SM",1\r\n')
    await asyncio.sleep(0.01)
    assert urcs == ['+CMTI: "SM",1']
    fake.responses["AT"] = "\r\nOK\r\n"
    assert await modem.send_command("AT") == ["OK"]


async def test_ambiguous_creg_is_response_for_creg_query(
    modem: SIM7600Modem, fake: FakeSerial
) -> None:
    """+CREG is a response to AT+CREG? but a URC otherwise."""
    urcs: list[str] = []
    modem.urc_callback = urcs.append
    fake.responses["AT+CREG?"] = "\r\n+CREG: 0,1\r\n\r\nOK\r\n"
    fake.responses["AT+CSQ"] = "\r\n+CREG: 5\r\n+CSQ: 10,99\r\n\r\nOK\r\n"
    assert await modem.get_registration_status() == 1
    assert await modem.get_signal_quality() == 10
    assert urcs == ["+CREG: 5"]


async def test_urc_callback_errors_do_not_break_reader(
    modem: SIM7600Modem, fake: FakeSerial
) -> None:
    """An exception in the callback is logged, the reader keeps running."""

    def _boom(_line: str) -> None:
        raise RuntimeError("boom")

    modem.urc_callback = _boom
    fake.responses["AT+CSQ"] = "\r\nRING\r\n+CSQ: 9,99\r\n\r\nOK\r\n"
    assert await modem.get_signal_quality() == 9


# --- Desync after timeout ---


async def test_late_reply_is_not_used_for_next_command(
    modem: SIM7600Modem, fake: FakeSerial
) -> None:
    """A reply arriving after a timeout is discarded, not given to the next command."""
    await modem.connect()
    fake.responses["AT+COPS?"] = ""  # no answer -> timeout
    fake.responses["AT+CSQ"] = "\r\n+CSQ: 25,99\r\n\r\nOK\r\n"

    assert await modem.send_command("AT+COPS?", timeout=0.05) == []
    # The late COPS answer arrives now.
    fake.feed('\r\n+COPS: 0,0,"Late Operator",7\r\n\r\nOK\r\n')
    assert await modem.send_command("AT+CSQ") == ["+CSQ: 25,99", "OK"]


async def test_stale_lines_flushed_before_write(
    modem: SIM7600Modem, fake: FakeSerial
) -> None:
    """Garbage queued between commands is dropped before the next write."""
    await modem.connect()
    fake.feed("\r\nOK\r\n")
    await asyncio.sleep(0.01)
    fake.responses["AT+CSQ"] = "\r\n+CSQ: 7,99\r\n\r\nOK\r\n"
    assert await modem.send_command("AT+CSQ") == ["+CSQ: 7,99", "OK"]


async def test_consecutive_timeouts_raise(
    modem: SIM7600Modem, fake: FakeSerial
) -> None:
    """After 3 timeouts in a row ModemTimeoutError is raised (no silent [])."""
    await modem.connect()
    fake.responses["*"] = ""
    assert await modem.send_command("AT+CSQ", timeout=0.02) == []
    assert await modem.send_command("AT+CSQ", timeout=0.02) == []
    with pytest.raises(ModemTimeoutError):
        await modem.send_command("AT+CSQ", timeout=0.02)
    assert not modem.connected


async def test_successful_reply_resets_timeout_counter(
    modem: SIM7600Modem, fake: FakeSerial
) -> None:
    """A good answer between timeouts resets the counter."""
    await modem.connect()
    fake.responses["AT+X"] = ""
    for _ in range(4):
        assert await modem.send_command("AT+X", timeout=0.02) == []
        assert await modem.send_command("AT") == ["OK"]


# --- Reconnect ---


async def test_reconnect_after_serial_exception(fake: FakeSerial) -> None:
    """A write error resets reader/writer and the next command reopens the port."""
    ports = [fake, FakeSerial()]
    opened: list[FakeSerial] = []

    async def _open(**_kwargs: object):
        port = ports[len(opened)]
        opened.append(port)
        return port.reader, port.writer

    with patch.object(
        modem_module.serial_asyncio_fast, "open_serial_connection", _open
    ):
        instance = SIM7600Modem("/dev/ttyUSB2", 115200)
        await instance.connect()
        fake.fail_writes = True
        with pytest.raises(ModemConnectionError):
            await instance.send_command("AT+CSQ")
        assert instance._reader is None
        assert instance._writer is None
        assert fake.writer.closed

        ports[1].responses["AT+CSQ"] = "\r\n+CSQ: 15,99\r\n\r\nOK\r\n"
        assert await instance.get_signal_quality() == 15
        assert len(opened) == 2
        await instance.close()


async def test_reconnect_after_eof(modem: SIM7600Modem, fake: FakeSerial) -> None:
    """EOF on the port (USB unplugged) marks the modem disconnected."""
    await modem.connect()
    fake.reader.feed_eof()
    await asyncio.sleep(0.01)
    assert not modem.connected


async def test_open_failure_raises_connection_error() -> None:
    """SerialException on open becomes ModemConnectionError."""

    async def _open(**_kwargs: object):
        raise serial.SerialException("no such port")

    with patch.object(
        modem_module.serial_asyncio_fast, "open_serial_connection", _open
    ):
        instance = SIM7600Modem("/dev/missing", 115200)
        with pytest.raises(ModemConnectionError):
            await instance.send_command("AT")
        assert not instance.connected


async def test_closed_modem_never_reopens(modem: SIM7600Modem) -> None:
    """After close() (entry unloaded) the port is not opened again."""
    await modem.close()
    with pytest.raises(ModemConnectionError):
        await modem.send_command("AT")


# --- SMS sending ---


def _cmgs_ok(_cmd: str) -> str:
    return "\r\n> "


async def test_send_sms_pdu_flow(modem: SIM7600Modem, fake: FakeSerial) -> None:
    """PDU mode, AT+CMGS=<len>, prompt, PDU + Ctrl+Z, +CMGS/OK."""
    fake.responses["AT+CMGS=15"] = _cmgs_ok
    fake.responses["<CTRL-Z>"] = "\r\n+CMGS: 12\r\n\r\nOK\r\n"
    await modem.send_sms("+79991234567", "hi")
    assert "AT+CMGS=15" in fake.commands
    assert fake.written[-1] == b"0001000B919799214365F7000002E834\x1a"


async def test_send_sms_cyrillic_multipart(
    modem: SIM7600Modem, fake: FakeSerial
) -> None:
    """Long Cyrillic text is sent as several UCS2 PDUs."""
    fake.responses["*"] = lambda cmd: (
        "\r\n> " if cmd.startswith("AT+CMGS=") else "\r\nOK\r\n"
    )
    fake.responses["<CTRL-Z>"] = "\r\n+CMGS: 1\r\n\r\nOK\r\n"
    await modem.send_sms("+79991234567", "Привет, мир! " * 10)
    cmgs = [c for c in fake.commands if c.startswith("AT+CMGS=")]
    assert len(cmgs) == 2


async def test_send_sms_strips_ctrl_z_and_esc(
    modem: SIM7600Modem, fake: FakeSerial
) -> None:
    """Ctrl+Z / ESC in the text never reach the modem as control codes."""
    fake.responses["*"] = lambda cmd: (
        "\r\n> " if cmd.startswith("AT+CMGS=") else "\r\nOK\r\n"
    )
    fake.responses["<CTRL-Z>"] = "\r\n+CMGS: 1\r\n\r\nOK\r\n"
    await modem.send_sms("+79991234567", "a\x1ab\x1bc")
    body = fake.written[-1]
    assert body.count(b"\x1a") == 1 and body.endswith(b"\x1a")
    assert b"\x1b" not in body


@pytest.mark.parametrize("number", ['+7999"\r\nAT+CFUN=0', "12", "+7 999", ""])
async def test_send_sms_rejects_invalid_number(
    modem: SIM7600Modem, fake: FakeSerial, number: str
) -> None:
    """Invalid numbers are rejected before anything is written."""
    with pytest.raises(InvalidInputError):
        await modem.send_sms(number, "test")
    assert fake.written == []


async def test_send_sms_rejects_empty_text(modem: SIM7600Modem) -> None:
    """Text that is empty after sanitising is rejected."""
    with pytest.raises(InvalidInputError):
        await modem.send_sms("+79991234567", "\x1a")


async def test_send_sms_esc_after_prompt_timeout(
    modem: SIM7600Modem, fake: FakeSerial
) -> None:
    """No '> ' prompt: ESC is sent so the modem leaves the input state."""
    fake.responses["AT+CMGS=15"] = ""
    with pytest.raises(ModemTimeoutError):
        await modem.send_sms("+79991234567", "hi")
    assert fake.written[-1] == b"\x1b"
    assert fake.commands[-1] == "<ESC>"
    # The modem works again afterwards.
    fake.responses["AT+CSQ"] = "\r\n+CSQ: 11,99\r\n\r\nOK\r\n"
    assert await modem.get_signal_quality() == 11


async def test_send_sms_error_after_body(modem: SIM7600Modem, fake: FakeSerial) -> None:
    """+CMS ERROR after the body raises ModemCommandError."""
    fake.responses["AT+CMGS=15"] = _cmgs_ok
    fake.responses["<CTRL-Z>"] = "\r\n+CMS ERROR: 500\r\n"
    with pytest.raises(ModemCommandError):
        await modem.send_sms("+79991234567", "hi")


async def test_send_sms_rejected_cmgs(modem: SIM7600Modem, fake: FakeSerial) -> None:
    """ERROR instead of the prompt raises ModemCommandError."""
    fake.responses["AT+CMGS=15"] = "\r\nERROR\r\n"
    with pytest.raises(ModemCommandError):
        await modem.send_sms("+79991234567", "hi")


async def test_send_sms_confirmation_timeout(
    modem: SIM7600Modem, fake: FakeSerial
) -> None:
    """No final result after the body raises ModemTimeoutError."""
    fake.responses["AT+CMGS=15"] = _cmgs_ok
    with pytest.raises(ModemTimeoutError):
        await modem.send_sms("+79991234567", "hi")


async def test_send_sms_default_send_timeout_is_60s() -> None:
    """The confirmation timeout defaults to 60 seconds."""
    assert SIM7600Modem("/dev/null", 115200).send_timeout == 60.0


# --- SMS reading ---


async def test_cmgl_body_ok_is_not_final(modem: SIM7600Modem, fake: FakeSerial) -> None:
    """The line after +CMGL is always the body, even if it reads 'OK'.

    In PDU mode the body is hex, so this covers modems in text mode and
    makes sure the reader keeps collecting until the real final OK.
    """
    fake.responses["AT+CMGL=4"] = (
        "\r\n+CMGL: 1,0,,20\r\n"
        f"{make_deliver_pdu('+111', 'OK')}\r\n"
        "+CMGL: 2,0,,20\r\n"
        f"{make_deliver_pdu('+222', 'second')}\r\n"
        "\r\nOK\r\n"
    )
    messages = await modem.get_unread_sms()
    assert [m.message for m in messages] == ["OK", "second"]


async def test_cmgl_raw_line_after_header_kept(
    modem: SIM7600Modem, fake: FakeSerial
) -> None:
    """A raw 'OK' body line does not end the response early."""
    fake.responses["AT+X"] = (
        '\r\n+CMGL: 1,"REC UNREAD","+111",,"25/11/07,12:30:45+12"\r\nOK\r\n\r\nOK\r\n'
    )
    lines = await modem.send_command("AT+X")
    assert lines == ['+CMGL: 1,"REC UNREAD","+111",,"25/11/07,12:30:45+12"', "OK", "OK"]


async def test_cmgl_ucs2_and_multipart(modem: SIM7600Modem, fake: FakeSerial) -> None:
    """UCS2 parts are decoded and joined; all storage indices are returned."""
    fake.responses["AT+CMGL=4"] = (
        "\r\n+CMGL: 4,0,,30\r\n"
        f"{make_deliver_pdu('+79991234567', 'мир', ref=9, total=2, seq=2)}\r\n"
        "+CMGL: 3,0,,30\r\n"
        f"{make_deliver_pdu('+79991234567', 'Привет, ', ref=9, total=2, seq=1)}\r\n"
        "\r\nOK\r\n"
    )
    messages = await modem.get_unread_sms()
    assert len(messages) == 1
    assert messages[0].message == "Привет, мир"
    assert messages[0].indices == (3, 4)
    assert messages[0].sender == "+79991234567"


async def test_cmgl_empty_body(modem: SIM7600Modem, fake: FakeSerial) -> None:
    """An SMS with an empty text is reported with an empty message."""
    fake.responses["AT+CMGL=4"] = (
        f"\r\n+CMGL: 1,0,,13\r\n{make_deliver_pdu('+111', '')}\r\n\r\nOK\r\n"
    )
    messages = await modem.get_unread_sms()
    assert len(messages) == 1
    assert messages[0].message == ""


async def test_cmgl_error_returns_empty(modem: SIM7600Modem, fake: FakeSerial) -> None:
    """A failing listing returns no messages."""
    fake.responses["AT+CMGL=4"] = "\r\n+CMS ERROR: 302\r\n"
    assert await modem.get_unread_sms() == []


async def test_delete_failure_is_logged(
    modem: SIM7600Modem, fake: FakeSerial, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing AT+CMGD is logged as warning."""
    fake.responses["AT+CMGD=5,0"] = "\r\n+CMS ERROR: 321\r\n"
    assert await modem.delete_sms(5) is False
    assert "Failed to delete SMS 5" in caplog.text


async def test_delete_read_sms(modem: SIM7600Modem, fake: FakeSerial) -> None:
    """AT+CMGD=1,1 removes all read messages."""
    assert await modem.delete_read_sms() is True
    assert "AT+CMGD=1,1" in fake.commands


# --- USSD ---


async def test_ussd_ucs2_reply(modem: SIM7600Modem, fake: FakeSerial) -> None:
    """The +CUSD URC answers the request and UCS2 is decoded."""
    text = ucs2_hex_encode("Баланс: 150,00 р.")
    fake.responses['AT+CUSD=1,"*100#",15'] = f'\r\nOK\r\n\r\n+CUSD: 0,"{text}",72\r\n'
    reply = await modem.send_ussd("*100#")
    assert reply.status == 0
    assert reply.message == "Баланс: 150,00 р."


async def test_ussd_multiline_reply(modem: SIM7600Modem, fake: FakeSerial) -> None:
    """A reply with line breaks inside the quotes is kept together."""
    fake.responses['AT+CUSD=1,"*102#",15'] = (
        '\r\nOK\r\n\r\n+CUSD: 0,"Line 1\r\nLine 2",15\r\n'
    )
    reply = await modem.send_ussd("*102#")
    assert reply.message == "Line 1\nLine 2"


async def test_ussd_menu_session_closed(modem: SIM7600Modem, fake: FakeSerial) -> None:
    """Status 1 (further action) closes the session with AT+CUSD=2."""
    fake.responses['AT+CUSD=1,"*111#",15'] = '\r\nOK\r\n\r\n+CUSD: 1,"Menu",15\r\n'
    reply = await modem.send_ussd("*111#")
    assert reply.message == "Menu"
    assert "AT+CUSD=2" in fake.commands


async def test_ussd_timeout(modem: SIM7600Modem, fake: FakeSerial) -> None:
    """No +CUSD within the timeout raises UssdError and cancels the session."""
    with pytest.raises(UssdError):
        await modem.send_ussd("*100#")
    assert fake.commands[-1] == "AT+CUSD=2"


async def test_ussd_rejected(modem: SIM7600Modem, fake: FakeSerial) -> None:
    """ERROR from the modem raises UssdError."""
    fake.responses['AT+CUSD=1,"*100#",15'] = "\r\n+CME ERROR: unknown\r\n"
    with pytest.raises(UssdError):
        await modem.send_ussd("*100#")


@pytest.mark.parametrize("code", ['*100#"\r\nAT', "", "abc"])
async def test_ussd_rejects_invalid_code(
    modem: SIM7600Modem, fake: FakeSerial, code: str
) -> None:
    """Codes with other characters are rejected before writing."""
    with pytest.raises(InvalidInputError):
        await modem.send_ussd(code)
    assert fake.written == []


# --- Misc parsing through the real transport ---


async def test_firmware_strips_prefix(modem: SIM7600Modem, fake: FakeSerial) -> None:
    """'+CGMR: ' and 'Revision:' prefixes are removed."""
    fake.responses["AT+CGMR"] = "\r\n+CGMR: LE20B04SIM7600M22\r\n\r\nOK\r\n"
    assert await modem.get_firmware() == "LE20B04SIM7600M22"


async def test_gps_fix_has_timestamp(modem: SIM7600Modem, fake: FakeSerial) -> None:
    """A GPS fix records when it was received."""
    fake.responses["AT+CGPSINFO"] = (
        "\r\n+CGPSINFO: 5231.450000,N,01324.550000,E,071125,123045.0,10.0,5.4,0.0\r\n"
        "\r\nOK\r\n"
    )
    gps = await modem.get_gps_info()
    assert gps is not None
    assert gps.speed == 5.4
    assert gps.fix_time is not None
