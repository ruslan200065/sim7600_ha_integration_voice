"""Pure helpers for the JARVIS Q1 companion.

This module must not import Home Assistant or use relative imports, so it can be
unit tested on a machine without Home Assistant installed.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import ipaddress
import re
from typing import Any

MAX_STATE_LENGTH = 255
CSQ_UNKNOWN = 99
ACTIVE_CALL_STATES = frozenset({"ringing", "answering", "dialing", "connected"})
IDLE_CALL_STATES = frozenset({"idle", "offline"})
SOURCE_HASSIO = "hassio"
REDACTED = "**REDACTED**"

_HOSTNAME_LABEL = re.compile(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)\Z")
_PHONE = re.compile(r"\+\d{6,15}\b|\b\d{10,15}\b")
_MIN_SECRET_LENGTH = 6


def truncate_state(value: Any, limit: int = MAX_STATE_LENGTH) -> str | None:
    """Return a state string no longer than ``limit`` characters (None when empty)."""
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"


def parse_csq(value: Any) -> int | None:
    """Parse a CSQ RSSI value ("17,99", "+CSQ: 17,99" or 17). 99/out of range -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        csq = int(value)
    else:
        text = str(value).strip()
        if text.upper().startswith("+CSQ:"):
            text = text[5:]
        try:
            csq = int(text.split(",")[0].strip())
        except ValueError:
            return None
    if csq == CSQ_UNKNOWN or not 0 <= csq <= 31:
        return None
    return csq


def csq_to_dbm(csq: int | None) -> int | None:
    """Convert a valid CSQ RSSI index (0..31) to dBm."""
    if csq is None:
        return None
    return -113 + 2 * csq


def normalize_host(value: Any) -> str | None:
    """Return a cleaned host (IPv4, IPv6 without brackets, or hostname) or None if invalid."""
    if not isinstance(value, str):
        return None
    host = value.strip()
    bracketed = host.startswith("[") and host.endswith("]")
    if bracketed:
        host = host[1:-1]
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if bracketed and address.version != 6:
            return None
        return str(address)
    if bracketed or not host or len(host) > 253:
        return None
    labels = host[:-1].split(".") if host.endswith(".") else host.split(".")
    if not all(_HOSTNAME_LABEL.match(label) for label in labels):
        return None
    if labels[-1].isdigit():
        # Looks like a malformed IPv4 address (e.g. 999.1.1.1), not a hostname.
        return None
    return host


def format_url_host(host: str) -> str:
    """Format a normalized host for use in a URL (brackets for IPv6)."""
    if ":" in host:
        return "[" + host.replace("%", "%25") + "]"
    return host


def normalize_token(value: Any) -> str:
    """Return the API token without surrounding whitespace."""
    if value is None:
        return ""
    return str(value).strip()


def call_transition(previous: str | None, current: str | None) -> str | None:
    """Return "incoming" or "ended" when call_state changes in a notable way.

    ``previous`` is None on the first poll, so no event is fired for the state the
    App was already in when Home Assistant started.
    """
    if previous is None or previous == current:
        return None
    if current == "ringing":
        return "incoming"
    if previous in ACTIVE_CALL_STATES and current in IDLE_CALL_STATES:
        return "ended"
    return None


def is_supervisor_entry(data: Mapping[str, Any], entry_source: str | None = None) -> bool:
    """True for entries created from Supervisor (hassio) discovery."""
    return data.get("source") == SOURCE_HASSIO or entry_source == SOURCE_HASSIO


class EntryLookupError(LookupError):
    """Service call could not be mapped to a loaded config entry."""

    MESSAGES = {
        "no_entries": "No JARVIS Q1 gateway is loaded",
        "multiple_entries": "Specify entry_id: several JARVIS Q1 gateways are loaded",
        "unknown_entry": "JARVIS Q1 config entry {entry_id} is not loaded or does not exist",
    }

    def __init__(self, code: str, entry_id: str | None = None) -> None:
        self.code = code
        self.entry_id = entry_id or ""
        super().__init__(self.MESSAGES[code].format(entry_id=self.entry_id))


def resolve_entry_id(loaded: Mapping[str, Any], entry_id: str | None) -> str:
    """Pick the target entry: explicit id, or the only loaded one."""
    if entry_id:
        if entry_id not in loaded:
            raise EntryLookupError("unknown_entry", entry_id)
        return entry_id
    if not loaded:
        raise EntryLookupError("no_entries")
    if len(loaded) != 1:
        raise EntryLookupError("multiple_entries")
    return next(iter(loaded))


def redact(data: Any, keys: Iterable[str], secrets: Iterable[Any] = ()) -> Any:
    """Recursively redact values of ``keys``, literal ``secrets`` and phone numbers."""
    key_set = {key.lower() for key in keys}
    literals = sorted(
        {str(s) for s in secrets if s is not None and len(str(s)) >= _MIN_SECRET_LENGTH},
        key=len, reverse=True,
    )

    def scrub(text: str) -> str:
        for literal in literals:
            text = text.replace(literal, REDACTED)
        return _PHONE.sub(REDACTED, text)

    def walk(value: Any) -> Any:
        if isinstance(value, Mapping):
            result = {}
            for key, item in value.items():
                if str(key).lower() in key_set and item not in (None, ""):
                    result[key] = REDACTED
                else:
                    result[key] = walk(item)
            return result
        if isinstance(value, (list, tuple, set, frozenset)):
            return [walk(item) for item in value]
        if isinstance(value, str):
            return scrub(value)
        return value

    return walk(data)
