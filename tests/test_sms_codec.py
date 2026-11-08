"""Pure tests for the SMS/USSD codec (no Home Assistant needed)."""

from __future__ import annotations

import pytest
from pdu_helpers import DEFAULT_TS_ISO, make_deliver_pdu

from custom_components.sim7600.sms_codec import (
    MAX_SMS_PARTS,
    SmsAssembler,
    SmsFragment,
    build_submit_pdus,
    decode_deliver_pdu,
    decode_ussd_text,
    gsm7_decode,
    gsm7_encode,
    gsm7_pack,
    gsm7_unpack,
    is_valid_phone_number,
    looks_like_ucs2_hex,
    maybe_decode_ucs2,
    parse_cmgl_pdu,
    parse_cmgl_text,
    parse_cusd,
    parse_text_timestamp,
    sanitize_sms_text,
    split_sms,
    ucs2_hex_decode,
    ucs2_hex_encode,
)

# --- Number validation ---


@pytest.mark.parametrize("number", ["+79991234567", "112", "4915112345678", "+123"])
def test_valid_numbers(number: str) -> None:
    """Digits with optional leading + are accepted."""
    assert is_valid_phone_number(number)


@pytest.mark.parametrize(
    "number",
    [
        "12",
        "+",
        "",
        "+7999 123",
        '+7999"\r\nAT+CFUN=0',
        "+79991234567;",
        "1" * 21,
        "++7999",
        "abc123",
    ],
)
def test_invalid_numbers(number: str) -> None:
    """Anything that could inject AT commands is rejected."""
    assert not is_valid_phone_number(number)


def test_sanitize_removes_ctrl_z_and_esc() -> None:
    """Ctrl+Z and ESC would end or abort AT+CMGS."""
    assert sanitize_sms_text("a\x1ab\x1bc") == "abc"


# --- UCS2 ---


def test_ucs2_roundtrip_cyrillic() -> None:
    """Cyrillic text encodes to UTF-16BE hex and back."""
    encoded = ucs2_hex_encode("Привет")
    assert encoded == "041F04400438043204350442"
    assert ucs2_hex_decode(encoded) == "Привет"


def test_ucs2_roundtrip_emoji_surrogates() -> None:
    """Characters outside the BMP survive as surrogate pairs."""
    assert ucs2_hex_decode(ucs2_hex_encode("ok \U0001f600")) == "ok \U0001f600"


def test_ucs2_heuristic_does_not_eat_numbers() -> None:
    """Plain digits are not mistaken for UCS2 hex."""
    assert not looks_like_ucs2_hex("1234")
    assert maybe_decode_ucs2("1234") == "1234"
    assert maybe_decode_ucs2("00480069") == "Hi"


# --- GSM 7-bit ---


def test_gsm7_pack_known_vector() -> None:
    """'hellohello' packs to the well-known E8329BFD4697D9EC37."""
    assert gsm7_pack(gsm7_encode("hellohello")).hex().upper() == "E8329BFD4697D9EC37"


def test_gsm7_extension_roundtrip() -> None:
    """Extension characters use the escape code and decode back."""
    septets = gsm7_encode("€[x]")
    assert len(septets) == 7
    assert gsm7_decode(gsm7_unpack(gsm7_pack(septets), len(septets))) == "€[x]"


# --- Splitting ---


def test_split_gsm7_limits() -> None:
    """160 septets fit one SMS; longer text uses 153 per part."""
    assert split_sms("a" * 160) == ["a" * 160]
    parts = split_sms("a" * 161)
    assert [len(p) for p in parts] == [153, 8]


def test_split_ucs2_limits() -> None:
    """70 UCS2 characters fit one SMS; longer text uses 67 per part."""
    assert split_sms("я" * 70) == ["я" * 70]
    assert [len(p) for p in split_sms("я" * 71)] == [67, 4]
    assert [len(p) for p in split_sms("я" * 134)] == [67, 67]


def test_split_does_not_break_extension_char() -> None:
    """A 2-septet character is moved to the next part as a whole."""
    parts = split_sms("a" * 152 + "€" + "b" * 10)
    assert parts[0] == "a" * 152
    assert parts[1].startswith("€")


def test_split_does_not_break_surrogate_pair() -> None:
    """Emoji (2 UCS2 units) are never split."""
    parts = split_sms("я" * 66 + "\U0001f600" + "я" * 10)
    assert parts[0] == "я" * 66
    assert parts[1].startswith("\U0001f600")


# --- SUBMIT PDUs ---


def test_submit_pdu_single_gsm7() -> None:
    """Short GSM text: one PDU, DCS 00, international address."""
    pdus = build_submit_pdus("+79991234567", "hi", 0)
    assert len(pdus) == 1
    assert pdus[0].hex == "0001000B919799214365F7000002E834"
    assert pdus[0].tpdu_length == 15


def test_submit_pdu_ucs2_multipart() -> None:
    """Long Cyrillic text: UCS2 parts with a concatenation header."""
    text = "Привет, мир! " * 10  # 130 chars
    pdus = build_submit_pdus("+79991234567", text, 0x2A)
    assert len(pdus) == 2
    # Layout: SMSC(0) FO(1) MR(2) DA-len(3) TOA(4) DA(5-10) PID(11) DCS(12)
    # UDL(13) UDH(14-19)
    for seq, pdu in enumerate(pdus, start=1):
        raw = bytes.fromhex(pdu.hex)
        assert raw[1] & 0x40  # UDHI
        assert len(raw) - 1 == pdu.tpdu_length
        assert raw[12] == 0x08  # DCS UCS2
        assert raw[14:20] == bytes([0x05, 0x00, 0x03, 0x2A, 2, seq])
    # 67 chars * 2 bytes + 6 byte header
    assert bytes.fromhex(pdus[0].hex)[13] == 140


def test_submit_pdu_gsm7_multipart_udl() -> None:
    """GSM 7-bit parts count the header as 7 septets."""
    pdus = build_submit_pdus("+4915112345678", "A" * 200, 9)
    assert len(pdus) == 2
    # 13 digit address -> 7 octets, so UDL is at offset 14.
    assert bytes.fromhex(pdus[0].hex)[14] == 160  # 7 + 153 septets


def test_submit_rejects_bad_input() -> None:
    """Invalid number, empty text and too many parts raise ValueError."""
    with pytest.raises(ValueError):
        build_submit_pdus('+7"AT', "x")
    with pytest.raises(ValueError):
        build_submit_pdus("+79991234567", "\x1a")
    with pytest.raises(ValueError):
        build_submit_pdus("+79991234567", "я" * (67 * MAX_SMS_PARTS + 1))


# --- DELIVER PDUs ---


def test_decode_deliver_known_vector() -> None:
    """Public test vector decodes sender, time and text."""
    pdu = "07911326040000F0040B911346610089F60000208062917314080CC8F71D14969741F977FD07"
    decoded = decode_deliver_pdu(pdu)
    assert decoded is not None
    assert decoded.sender == "+31641600986"
    assert decoded.text == "How are you?"
    assert decoded.timestamp == "2002-08-26T19:37:41+00:00"


def test_decode_deliver_ucs2_multipart_header() -> None:
    """UCS2 body and concatenation info are decoded."""
    pdu = make_deliver_pdu("+79991234567", "Привет", ref=5, total=2, seq=1)
    decoded = decode_deliver_pdu(pdu)
    assert decoded is not None
    assert decoded.text == "Привет"
    assert (decoded.ref, decoded.total, decoded.seq) == (5, 2, 1)
    assert decoded.timestamp == DEFAULT_TS_ISO


def test_decode_deliver_gsm7_with_header() -> None:
    """GSM 7-bit body after a UDH honours the fill bits."""
    pdu = make_deliver_pdu("+111", "Hello world", ref=1, total=2, seq=2)
    decoded = decode_deliver_pdu(pdu)
    assert decoded is not None
    assert decoded.text == "Hello world"
    assert decoded.seq == 2


def test_decode_truncated_pdu_raises() -> None:
    """Truncated PDUs raise ValueError."""
    with pytest.raises(ValueError):
        decode_deliver_pdu("0004")


# --- CMGL (PDU mode) ---


def test_parse_cmgl_pdu_skips_garbage() -> None:
    """Non-hex and undecodable bodies are skipped, others kept."""
    good = make_deliver_pdu("+111", "First")
    lines = [
        "+CMGL: 1,0,,20",
        good,
        "+CMGL: 2,0,,5",
        "OK",
        "+CMGL: 3,1,,3",
        "0004",
        "OK",
    ]
    fragments = parse_cmgl_pdu(lines)
    assert [f.index for f in fragments] == [1]
    assert fragments[0].text == "First"
    assert fragments[0].status == 0


# --- CMGL (text mode) edge cases ---


def test_cmgl_text_body_ok_does_not_break_parsing() -> None:
    """A body that is literally 'OK' or 'ERROR' is a body, not a result code."""
    lines = [
        '+CMGL: 1,"REC UNREAD","+111",,"25/11/07,12:30:45+12"',
        "OK",
        '+CMGL: 2,"REC UNREAD","+222",,"25/11/07,12:31:00+12"',
        "ERROR",
        '+CMGL: 3,"REC UNREAD","+333",,"25/11/07,12:32:00+12"',
        "Third",
        "OK",
    ]
    fragments = parse_cmgl_text(lines)
    assert [f.text for f in fragments] == ["OK", "ERROR", "Third"]
    assert fragments[0].timestamp == "2025-11-07T12:30:45+03:00"


def test_cmgl_text_multiline_and_empty_bodies() -> None:
    """Multi-line bodies keep their lines; empty bodies stay empty."""
    lines = [
        '+CMGL: 1,"REC UNREAD","+111",,"25/11/07,12:30:45+12"',
        "line one",
        "line two",
        '+CMGL: 2,"REC UNREAD","+222",,"25/11/07,12:30:45+12"',
        "",
        "OK",
    ]
    fragments = parse_cmgl_text(lines)
    assert fragments[0].text == "line one\nline two"
    assert fragments[1].text == ""
    assert len(fragments) == 2


def test_cmgl_text_ucs2_body_and_sender() -> None:
    """UCS2 hex bodies (AT+CSCS="UCS2") are decoded."""
    lines = [
        f'+CMGL: 4,"REC UNREAD","{ucs2_hex_encode("+79991234567")}",,'
        '"25/11/07,12:30:45+12"',
        ucs2_hex_encode("Привет"),
        "OK",
    ]
    fragments = parse_cmgl_text(lines)
    assert fragments[0].sender == "+79991234567"
    assert fragments[0].text == "Привет"


def test_text_timestamp_negative_zone_and_garbage() -> None:
    """Negative quarter-hour zones work; unknown formats are passed through."""
    assert parse_text_timestamp("25/01/02,03:04:05-08") == "2025-01-02T03:04:05-02:00"
    assert parse_text_timestamp("garbage") == "garbage"


# --- Multipart assembly ---


def _frag(
    index: int, seq: int, text: str, status: int = 0, total: int = 2
) -> SmsFragment:
    return SmsFragment(
        index=index,
        status=status,
        sender="+111",
        timestamp="ts",
        text=text,
        ref=7,
        total=total,
        seq=seq,
    )


def test_assemble_out_of_order_parts() -> None:
    """Parts are joined by sequence number, not storage order."""
    assembler = SmsAssembler()
    messages = assembler.assemble([_frag(5, 2, "world"), _frag(4, 1, "Hello ")], 0)
    assert len(messages) == 1
    assert messages[0].message == "Hello world"
    assert messages[0].indices == (4, 5)
    assert messages[0].parts == 2
    assert messages[0].complete


def test_assemble_waits_for_missing_part_across_calls() -> None:
    """An incomplete group waits; the next listing (parts now read) completes it."""
    assembler = SmsAssembler(timeout=60)
    assert assembler.assemble([_frag(1, 1, "Hello ")], 0) == []
    assert assembler.has_pending
    messages = assembler.assemble(
        [_frag(1, 1, "Hello ", status=1), _frag(2, 2, "world")], 10
    )
    assert [m.message for m in messages] == ["Hello world"]
    assert not assembler.has_pending


def test_assemble_timeout_delivers_partial() -> None:
    """After the timeout the available parts are delivered, marked incomplete."""
    assembler = SmsAssembler(timeout=60)
    assembler.assemble([_frag(1, 1, "Hello ")], 0)
    messages = assembler.assemble([_frag(1, 1, "Hello ", status=1)], 61)
    assert messages[0].message == "Hello "
    assert not messages[0].complete


def test_assemble_ignores_old_read_messages() -> None:
    """Read messages from before are not reported again."""
    assembler = SmsAssembler()
    single = SmsFragment(1, 1, "+1", "ts", "old")
    assert (
        assembler.assemble([single, _frag(2, 1, "a", 1), _frag(3, 2, "b", 1)], 0) == []
    )


# --- USSD ---


def test_ussd_ucs2_reply() -> None:
    """DCS 72 (UCS2) hex replies are decoded."""
    reply = parse_cusd(f'+CUSD: 0,"{ucs2_hex_encode("Баланс: 100 р.")}",72')
    assert reply is not None
    assert reply.status == 0
    assert reply.message == "Баланс: 100 р."
    assert reply.dcs == 72


def test_ussd_gsm_reply() -> None:
    """DCS 15 text replies are returned as is."""
    reply = parse_cusd('+CUSD: 0,"Balance: 10.00 EUR",15')
    assert reply is not None
    assert reply.message == "Balance: 10.00 EUR"


def test_ussd_language_prefixed_ucs2() -> None:
    """DCS 0x11 drops the leading language indication."""
    assert decode_ussd_text(ucs2_hex_encode("rДа"), 0x11) == "Да"


def test_ussd_status_without_text() -> None:
    """'+CUSD: 4' (not supported) has no message."""
    reply = parse_cusd("+CUSD: 4")
    assert reply is not None
    assert reply.status == 4
    assert reply.message is None
