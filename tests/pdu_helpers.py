"""Helpers to build SMS-DELIVER PDUs for tests."""

from __future__ import annotations

from custom_components.sim7600.sms_codec import gsm7_encode, gsm7_pack, is_gsm7


def _bcd(value: int) -> int:
    return ((value % 10) << 4) | (value // 10)


# 2025-11-07 12:30:45 +03:00 (12 quarter hours)
DEFAULT_SCTS = bytes(_bcd(v) for v in (25, 11, 7, 12, 30, 45)) + bytes([_bcd(12)])
DEFAULT_TS_ISO = "2025-11-07T12:30:45+03:00"


def make_deliver_pdu(
    sender: str,
    text: str,
    *,
    ref: int | None = None,
    total: int = 1,
    seq: int = 1,
    ucs2: bool | None = None,
) -> str:
    """Return the hex of an SMS-DELIVER PDU (empty SMSC)."""
    digits = sender.lstrip("+")
    toa = 0x91 if sender.startswith("+") else 0x81
    semi = digits + ("F" if len(digits) % 2 else "")
    address = bytes([len(digits), toa]) + bytes.fromhex(
        "".join(semi[i + 1] + semi[i] for i in range(0, len(semi), 2))
    )
    udh = bytes([0x05, 0x00, 0x03, ref, total, seq]) if ref is not None else b""
    use_ucs2 = (not is_gsm7(text)) if ucs2 is None else ucs2
    if use_ucs2:
        body = text.encode("utf-16-be")
        udl = len(udh) + len(body)
        user_data = udh + body
        dcs = 0x08
    else:
        septets = gsm7_encode(text)
        fill = (7 - (len(udh) * 8) % 7) % 7 if udh else 0
        udl = (len(udh) * 8 + fill) // 7 + len(septets)
        user_data = udh + gsm7_pack(septets, fill)
        dcs = 0x00
    first = 0x04 | (0x40 if udh else 0x00)
    tpdu = (
        bytes([first])
        + address
        + bytes([0x00, dcs])
        + DEFAULT_SCTS
        + bytes([udl])
        + user_data
    )
    return ("00" + tpdu.hex()).upper()
