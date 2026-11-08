"""SMS and USSD encoding helpers for the SIM7600 integration.

This module is pure Python. It has no Home Assistant or serial dependencies so
it can be unit tested in isolation.

Sending uses PDU mode (AT+CMGF=0). PDU mode is the only mode that supports
both UCS2 (Cyrillic etc.) and concatenated (multipart) SMS reliably on SIMCom
modems. Reading uses PDU mode as well; a text-mode parser exists as fallback.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .types import SmsData

PHONE_NUMBER_PATTERN = r"^\+?\d{3,20}$"
PHONE_NUMBER_RE = re.compile(PHONE_NUMBER_PATTERN)
USSD_CODE_PATTERN = r"^[0-9*#+]{1,40}$"
USSD_CODE_RE = re.compile(USSD_CODE_PATTERN)

ALPHABET_GSM7 = 0
ALPHABET_8BIT = 1
ALPHABET_UCS2 = 2

GSM7_SINGLE_LIMIT = 160
GSM7_PART_LIMIT = 153
UCS2_SINGLE_LIMIT = 70
UCS2_PART_LIMIT = 67
# Hard cap on concatenated parts to avoid sending dozens of billable SMS by
# accident. The protocol allows 255.
MAX_SMS_PARTS = 10

# 3GPP TS 23.038 GSM 7-bit default alphabet. Index 0x1B is the escape code.
GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅå"
    "Δ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ"
    " !\"#¤%&'()*+,-./"
    "0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_ESCAPE = 0x1B
GSM7_EXT = {
    "\f": 0x0A,
    "^": 0x14,
    "{": 0x28,
    "}": 0x29,
    "\\": 0x2F,
    "[": 0x3C,
    "~": 0x3D,
    "]": 0x3E,
    "|": 0x40,
    "€": 0x65,
}
_BASIC_INDEX = {ch: i for i, ch in enumerate(GSM7_BASIC) if i != GSM7_ESCAPE}
_EXT_REVERSE = {code: ch for ch, code in GSM7_EXT.items()}

_FINAL_ERRORS = ("ERROR", "+CME ERROR:", "+CMS ERROR:")
_HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")
_UCS2_HEX_RE = re.compile(r"^(?:[0-9A-Fa-f]{4})+$")
_CMGL_PDU_HEADER_RE = re.compile(r"^\+CMGL:\s*(\d+)\s*,\s*(\d+)")
_CMGL_TEXT_HEADER_RE = re.compile(
    r'^\+CMGL:\s*(\d+),"([^"]*)","([^"]*)"(?:,(?:"[^"]*"|[^,]*))?(?:,"([^"]*)")?'
)
_TEXT_STATUS = {"REC UNREAD": 0, "REC READ": 1, "STO UNSENT": 2, "STO SENT": 3}
_CUSD_RE = re.compile(r'^\+CUSD:\s*(\d+)(?:\s*,\s*"(.*)"(?:\s*,\s*(\d+))?)?', re.S)
_TEXT_TS_RE = re.compile(
    r"^(\d{2})/(\d{2})/(\d{2}),(\d{2}):(\d{2}):(\d{2})([+-])(\d{1,2})$"
)


def is_final_result(line: str) -> bool:
    """Return True if the line is a final AT result code."""
    return line == "OK" or line.startswith(_FINAL_ERRORS)


def is_error_result(line: str) -> bool:
    """Return True if the line is a final AT error result."""
    return line.startswith(_FINAL_ERRORS)


def sanitize_sms_text(text: str) -> str:
    """Remove Ctrl+Z and ESC, which would terminate or abort AT+CMGS."""
    return text.replace("\x1a", "").replace("\x1b", "")


def is_valid_phone_number(number: str) -> bool:
    """Return True if the number is safe to put into an AT command."""
    return bool(PHONE_NUMBER_RE.fullmatch(number))


# ---------------------------------------------------------------------------
# GSM 7-bit
# ---------------------------------------------------------------------------


def is_gsm7(text: str) -> bool:
    """Return True if every character exists in the GSM 7-bit alphabet."""
    return all(ch in _BASIC_INDEX or ch in GSM7_EXT for ch in text)


def gsm7_char_septets(ch: str) -> int:
    """Return the number of septets a character needs."""
    return 2 if ch in GSM7_EXT else 1


def gsm7_encode(text: str) -> list[int]:
    """Encode text into GSM 7-bit septets (unpacked)."""
    septets: list[int] = []
    for ch in text:
        if ch in _BASIC_INDEX:
            septets.append(_BASIC_INDEX[ch])
        elif ch in GSM7_EXT:
            septets.extend((GSM7_ESCAPE, GSM7_EXT[ch]))
        else:
            raise ValueError(f"Character {ch!r} is not in the GSM 7-bit alphabet")
    return septets


def gsm7_decode(septets: Iterable[int]) -> str:
    """Decode unpacked GSM 7-bit septets into text."""
    out: list[str] = []
    escape = False
    for septet in septets:
        if escape:
            escape = False
            if septet in _EXT_REVERSE:
                out.append(_EXT_REVERSE[septet])
            elif septet != GSM7_ESCAPE:
                out.append(GSM7_BASIC[septet])
            else:
                out.append(" ")
        elif septet == GSM7_ESCAPE:
            escape = True
        else:
            out.append(GSM7_BASIC[septet & 0x7F])
    return "".join(out)


def gsm7_pack(septets: list[int], fill_bits: int = 0) -> bytes:
    """Pack septets into octets, starting after fill_bits padding bits."""
    acc = 0
    nbits = fill_bits
    out = bytearray()
    for septet in septets:
        acc |= (septet & 0x7F) << nbits
        nbits += 7
        while nbits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            nbits -= 8
    if nbits > 0:
        out.append(acc & 0xFF)
    return bytes(out)


def gsm7_unpack(data: bytes, count: int, fill_bits: int = 0) -> list[int]:
    """Unpack count septets from packed octets, skipping fill_bits."""
    available = max(0, (len(data) * 8 - fill_bits) // 7)
    count = min(count, available)
    value = int.from_bytes(data, "little")
    return [(value >> (fill_bits + 7 * i)) & 0x7F for i in range(count)]


# ---------------------------------------------------------------------------
# UCS2
# ---------------------------------------------------------------------------


def ucs2_hex_encode(text: str) -> str:
    """Encode text as uppercase hex UTF-16BE (the modem's UCS2 format)."""
    return text.encode("utf-16-be").hex().upper()


def ucs2_hex_decode(hex_text: str) -> str:
    """Decode hex UTF-16BE into text."""
    return bytes.fromhex(hex_text).decode("utf-16-be", errors="replace")


def looks_like_ucs2_hex(text: str) -> bool:
    """Heuristic for text-mode bodies without a known coding scheme.

    Only groups starting with 00 (Latin-1) or 04 (Cyrillic) are accepted, so
    plain numeric bodies such as "1234" are not mistaken for UCS2.
    """
    if len(text) < 4 or not _UCS2_HEX_RE.fullmatch(text):
        return False
    return all(text[i : i + 2] in ("00", "04") for i in range(0, len(text), 4))


def maybe_decode_ucs2(text: str) -> str:
    """Decode text if it looks like UCS2 hex, otherwise return it unchanged."""
    if looks_like_ucs2_hex(text):
        return ucs2_hex_decode(text)
    return text


# ---------------------------------------------------------------------------
# Splitting and PDU encoding
# ---------------------------------------------------------------------------


def split_sms(text: str) -> list[str]:
    """Split text into SMS parts.

    GSM 7-bit: 160 septets single, 153 per concatenated part.
    UCS2: 70 code units single, 67 per concatenated part.
    Extension characters (2 septets) and surrogate pairs are never split.
    """
    if is_gsm7(text):
        weight = gsm7_char_septets
        single, part_limit = GSM7_SINGLE_LIMIT, GSM7_PART_LIMIT
    else:

        def weight(ch: str) -> int:
            return 2 if ord(ch) > 0xFFFF else 1

        single, part_limit = UCS2_SINGLE_LIMIT, UCS2_PART_LIMIT

    if sum(weight(ch) for ch in text) <= single:
        return [text]

    parts: list[str] = []
    current: list[str] = []
    used = 0
    for ch in text:
        cost = weight(ch)
        if used + cost > part_limit:
            parts.append("".join(current))
            current, used = [], 0
        current.append(ch)
        used += cost
    if current:
        parts.append("".join(current))
    return parts


def encode_address(number: str) -> bytes:
    """Encode a destination address (length, type, swapped BCD digits)."""
    if not is_valid_phone_number(number):
        raise ValueError(f"Invalid phone number: {number!r}")
    digits = number.lstrip("+")
    toa = 0x91 if number.startswith("+") else 0x81
    semi = digits + ("F" if len(digits) % 2 else "")
    swapped = "".join(semi[i + 1] + semi[i] for i in range(0, len(semi), 2))
    return bytes([len(digits), toa]) + bytes.fromhex(swapped)


@dataclass(frozen=True)
class SubmitPdu:
    """One SMS-SUBMIT PDU ready for AT+CMGS=<tpdu_length>."""

    tpdu_length: int
    hex: str


def build_submit_pdus(number: str, text: str, reference: int = 0) -> list[SubmitPdu]:
    """Build SMS-SUBMIT PDUs for a (possibly multipart) message."""
    text = sanitize_sms_text(text)
    if not text:
        raise ValueError("SMS text is empty")
    address = encode_address(number)
    gsm7 = is_gsm7(text)
    dcs = 0x00 if gsm7 else 0x08
    parts = split_sms(text)
    total = len(parts)
    if total > MAX_SMS_PARTS:
        raise ValueError(f"SMS needs {total} parts, the maximum is {MAX_SMS_PARTS}")
    multipart = total > 1
    ref = reference & 0xFF

    pdus: list[SubmitPdu] = []
    for seq, part in enumerate(parts, start=1):
        first_octet = 0x01 | (0x40 if multipart else 0x00)
        udh = bytes([0x05, 0x00, 0x03, ref, total, seq]) if multipart else b""
        if gsm7:
            septets = gsm7_encode(part)
            fill = (7 - (len(udh) * 8) % 7) % 7 if udh else 0
            udl = (len(udh) * 8 + fill) // 7 + len(septets)
            user_data = udh + gsm7_pack(septets, fill)
        else:
            body = part.encode("utf-16-be")
            udl = len(udh) + len(body)
            user_data = udh + body
        tpdu = (
            bytes([first_octet, 0x00]) + address + bytes([0x00, dcs, udl]) + user_data
        )
        pdus.append(SubmitPdu(len(tpdu), "00" + tpdu.hex().upper()))
    return pdus


# ---------------------------------------------------------------------------
# PDU decoding
# ---------------------------------------------------------------------------


def sms_dcs_alphabet(dcs: int) -> int:
    """Return the alphabet of an SMS data coding scheme (TS 23.038 ch. 4)."""
    group = dcs >> 4
    if group <= 0x7:
        alphabet = (dcs >> 2) & 0x03
        return ALPHABET_GSM7 if alphabet == 3 else alphabet
    if group in (0xC, 0xD):
        return ALPHABET_GSM7
    if group == 0xE:
        return ALPHABET_UCS2
    if group == 0xF:
        return ALPHABET_8BIT if dcs & 0x04 else ALPHABET_GSM7
    return ALPHABET_GSM7


def cbs_dcs_alphabet(dcs: int) -> int:
    """Return the alphabet of a CBS/USSD data coding scheme (TS 23.038 ch. 5)."""
    group = dcs >> 4
    if group == 0x1:
        return ALPHABET_UCS2 if (dcs & 0x0F) == 0x01 else ALPHABET_GSM7
    if 0x4 <= group <= 0x7 or group == 0x9:
        alphabet = (dcs >> 2) & 0x03
        return ALPHABET_GSM7 if alphabet == 3 else alphabet
    if group == 0xF:
        return ALPHABET_8BIT if dcs & 0x04 else ALPHABET_GSM7
    return ALPHABET_GSM7


def _decode_semi_octets(raw: bytes, digits: int) -> str:
    table = "0123456789*#abc"
    out: list[str] = []
    for octet in raw:
        for nibble in (octet & 0x0F, octet >> 4):
            if nibble == 0x0F:
                continue
            out.append(table[nibble])
    return "".join(out)[:digits]


def _swapped_bcd(octet: int) -> int:
    return (octet & 0x0F) * 10 + (octet >> 4)


def _decode_scts(raw: bytes) -> str:
    if len(raw) < 7:
        return ""
    try:
        year, month, day, hour, minute, second = (_swapped_bcd(b) for b in raw[:6])
        tz_octet = raw[6]
        quarters = (tz_octet & 0x07) * 10 + (tz_octet >> 4)
        offset = timedelta(minutes=15 * quarters)
        if tz_octet & 0x08:
            offset = -offset
        stamp = datetime(
            2000 + year, month, day, hour, minute, second, tzinfo=timezone(offset)
        )
    except ValueError:
        return ""
    return stamp.isoformat()


def parse_text_timestamp(value: str) -> str:
    """Convert a text-mode SCTS ("yy/MM/dd,hh:mm:ss+zz") to ISO 8601.

    The zone is given in quarter hours. Unparseable values are returned as is.
    """
    match = _TEXT_TS_RE.match(value.strip())
    if not match:
        return value
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    offset = timedelta(minutes=15 * int(match.group(8)))
    if match.group(7) == "-":
        offset = -offset
    try:
        stamp = datetime(
            2000 + year, month, day, hour, minute, second, tzinfo=timezone(offset)
        )
    except ValueError:
        return value
    return stamp.isoformat()


@dataclass(frozen=True)
class SmsFragment:
    """One stored SMS (a single message or one part of a multipart one)."""

    index: int
    status: int | None
    sender: str
    timestamp: str
    text: str
    ref: int | None = None
    total: int = 1
    seq: int = 1


@dataclass(frozen=True)
class DeliverPdu:
    """Decoded SMS-DELIVER PDU."""

    sender: str
    timestamp: str
    text: str
    ref: int | None = None
    total: int = 1
    seq: int = 1


def decode_deliver_pdu(pdu_hex: str) -> DeliverPdu | None:
    """Decode an SMS-DELIVER PDU (with SMSC prefix).

    Returns None for other PDU types (stored SUBMIT, status reports).
    Raises ValueError for malformed PDUs.
    """
    try:
        data = bytes.fromhex(pdu_hex.strip())
        pos = 1 + data[0]
        first_octet = data[pos]
        pos += 1
        if first_octet & 0x03 != 0x00:
            return None
        udhi = bool(first_octet & 0x40)

        oa_digits = data[pos]
        toa = data[pos + 1]
        pos += 2
        oa_octets = (oa_digits + 1) // 2
        oa_raw = data[pos : pos + oa_octets]
        pos += oa_octets
        if (toa & 0x70) == 0x50:
            sender = gsm7_decode(gsm7_unpack(oa_raw, (oa_digits * 4) // 7))
        else:
            sender = _decode_semi_octets(oa_raw, oa_digits)
            if (toa & 0x70) == 0x10:
                sender = "+" + sender

        dcs = data[pos + 1]
        pos += 2
        timestamp = _decode_scts(data[pos : pos + 7])
        pos += 7
        udl = data[pos]
        pos += 1
        user_data = data[pos:]
    except IndexError as err:
        raise ValueError("Truncated PDU") from err

    ref: int | None = None
    total = 1
    seq = 1
    header_len = 0
    if udhi and user_data:
        udhl = user_data[0]
        header_len = udhl + 1
        udh = user_data[1:header_len]
        i = 0
        while i + 1 < len(udh):
            iei, length = udh[i], udh[i + 1]
            value = udh[i + 2 : i + 2 + length]
            if iei == 0x00 and length == 3 and len(value) == 3:
                ref, total, seq = value[0], value[1], value[2]
            elif iei == 0x08 and length == 4 and len(value) == 4:
                ref, total, seq = (value[0] << 8) | value[1], value[2], value[3]
            i += 2 + length

    alphabet = sms_dcs_alphabet(dcs)
    if alphabet == ALPHABET_GSM7:
        fill = (7 - (header_len * 8) % 7) % 7 if header_len else 0
        header_septets = (header_len * 8 + fill) // 7
        septets = gsm7_unpack(user_data[header_len:], udl - header_septets, fill)
        text = gsm7_decode(septets)
    elif alphabet == ALPHABET_UCS2:
        text = user_data[header_len:udl].decode("utf-16-be", errors="replace")
    else:
        text = user_data[header_len:udl].decode("latin-1")

    if total < 1 or seq < 1:
        ref, total, seq = None, 1, 1
    return DeliverPdu(sender, timestamp, text, ref, total, seq)


def parse_cmgl_pdu(lines: list[str]) -> list[SmsFragment]:
    """Parse an AT+CMGL=4 (PDU mode) response.

    The modem layer guarantees that the line after each +CMGL header is the
    PDU, even if it looks like a result code.
    """
    fragments: list[SmsFragment] = []
    i = 0
    while i < len(lines):
        match = _CMGL_PDU_HEADER_RE.match(lines[i])
        if not match or i + 1 >= len(lines):
            i += 1
            continue
        index, status = int(match.group(1)), int(match.group(2))
        pdu = lines[i + 1].strip()
        i += 2
        if not pdu or not _HEX_RE.fullmatch(pdu):
            continue
        try:
            decoded = decode_deliver_pdu(pdu)
        except ValueError:
            continue
        if decoded is None:
            continue
        fragments.append(
            SmsFragment(
                index=index,
                status=status,
                sender=decoded.sender,
                timestamp=decoded.timestamp,
                text=decoded.text,
                ref=decoded.ref,
                total=decoded.total,
                seq=decoded.seq,
            )
        )
    return fragments


def parse_cmgl_text(lines: list[str]) -> list[SmsFragment]:
    """Parse an AT+CMGL (text mode) response.

    Bodies may be empty, multi-line, or equal to "OK"/"ERROR". Only the last
    line of the response is treated as the final result code. UCS2 hex bodies
    are decoded heuristically (see looks_like_ucs2_hex).
    """
    content = list(lines)
    if content and is_final_result(content[-1].strip()):
        content = content[:-1]

    fragments: list[SmsFragment] = []
    header: re.Match[str] | None = None
    body: list[str] = []

    def flush() -> None:
        if header is None:
            return
        while body and not body[-1].strip():
            body.pop()
        text = "\n".join(body)
        if "\n" not in text:
            text = maybe_decode_ucs2(text.strip())
        fragments.append(
            SmsFragment(
                index=int(header.group(1)),
                status=_TEXT_STATUS.get(header.group(2)),
                sender=maybe_decode_ucs2(header.group(3)),
                timestamp=parse_text_timestamp(header.group(4) or ""),
                text=text,
            )
        )

    for line in content:
        match = _CMGL_TEXT_HEADER_RE.match(line.strip())
        if match:
            flush()
            header = match
            body = []
        elif header is not None:
            body.append(line.rstrip("\r\n"))
    flush()
    return fragments


class SmsAssembler:
    """Join concatenated SMS parts listed from modem storage.

    The modem is listed with AT+CMGL=4 (all messages) on every cycle. Single
    messages are reported when unread. Multipart groups are reported once all
    parts are present, or with the available parts after `timeout` seconds.
    Read groups that were never seen as unread are ignored; they are removed
    by the periodic AT+CMGD=1,1 cleanup.
    """

    def __init__(self, timeout: float = 3600.0) -> None:
        """Initialize the assembler."""
        self.timeout = timeout
        self._first_seen: dict[tuple[str, int, int], float] = {}

    @property
    def has_pending(self) -> bool:
        """Return True while incomplete multipart messages are waiting."""
        return bool(self._first_seen)

    def assemble(self, fragments: list[SmsFragment], now: float) -> list[SmsData]:
        """Return complete messages, in storage order."""
        messages: list[tuple[int, SmsData]] = []
        groups: dict[tuple[str, int, int], list[SmsFragment]] = {}

        for frag in fragments:
            if frag.ref is None or frag.total <= 1:
                if frag.status in (None, 0):
                    messages.append(
                        (
                            frag.index,
                            SmsData(
                                index=frag.index,
                                sender=frag.sender,
                                timestamp=frag.timestamp,
                                message=frag.text,
                                indices=(frag.index,),
                            ),
                        )
                    )
                continue
            groups.setdefault((frag.sender, frag.ref, frag.total), []).append(frag)

        for key in list(self._first_seen):
            if key not in groups:
                del self._first_seen[key]

        for key, parts in groups.items():
            ours = key in self._first_seen or any(p.status in (None, 0) for p in parts)
            if not ours:
                continue
            first_seen = self._first_seen.setdefault(key, now)
            by_seq: dict[int, SmsFragment] = {}
            for part in sorted(parts, key=lambda p: p.index):
                by_seq.setdefault(part.seq, part)
            total = key[2]
            complete = all(seq in by_seq for seq in range(1, total + 1))
            if not complete and now - first_seen < self.timeout:
                continue
            del self._first_seen[key]
            ordered = [by_seq[seq] for seq in sorted(by_seq)]
            first = ordered[0]
            messages.append(
                (
                    min(p.index for p in parts),
                    SmsData(
                        index=first.index,
                        sender=first.sender,
                        timestamp=first.timestamp,
                        message="".join(p.text for p in ordered),
                        indices=tuple(sorted(p.index for p in parts)),
                        parts=total,
                        complete=complete,
                    ),
                )
            )

        messages.sort(key=lambda item: item[0])
        return [sms for _, sms in messages]


# ---------------------------------------------------------------------------
# USSD
# ---------------------------------------------------------------------------


def decode_ussd_text(text: str, dcs: int | None) -> str:
    """Decode the text of a +CUSD reply according to its CBS DCS."""
    if dcs is None:
        return maybe_decode_ucs2(text)
    alphabet = cbs_dcs_alphabet(dcs)
    if alphabet == ALPHABET_UCS2 and _UCS2_HEX_RE.fullmatch(text):
        decoded = ucs2_hex_decode(text)
        # DCS 0x11: UCS2 preceded by a language indication (one UCS2 unit).
        if dcs == 0x11 and decoded:
            decoded = decoded[1:]
        return decoded
    if alphabet == ALPHABET_8BIT and len(text) % 2 == 0 and _HEX_RE.fullmatch(text):
        return bytes.fromhex(text).decode("latin-1")
    return maybe_decode_ucs2(text)


@dataclass(frozen=True)
class CusdReply:
    """Parsed +CUSD unsolicited result."""

    status: int
    message: str | None
    dcs: int | None


def parse_cusd(line: str) -> CusdReply | None:
    """Parse a (possibly multi-line) +CUSD line."""
    match = _CUSD_RE.match(line.strip())
    if not match:
        return None
    status = int(match.group(1))
    dcs = int(match.group(3)) if match.group(3) is not None else None
    raw = match.group(2)
    message = decode_ussd_text(raw, dcs) if raw is not None else None
    return CusdReply(status=status, message=message, dcs=dcs)
