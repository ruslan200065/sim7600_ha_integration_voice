"""Pure tests for jarvis_q1/helpers.py, loaded by path so Home Assistant is not needed."""
import importlib.util
import json
from pathlib import Path

import pytest

COMPANION = Path(__file__).resolve().parents[1] / "integration" / "jarvis_q1"
_spec = importlib.util.spec_from_file_location("jarvis_q1_helpers", COMPANION / "helpers.py")
h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h)


# --- truncation -------------------------------------------------------------
def test_truncate_short_and_empty():
    assert h.truncate_state("hello") == "hello"
    assert h.truncate_state("") is None
    assert h.truncate_state(None) is None


def test_truncate_long():
    text = "я" * 400
    out = h.truncate_state(text)
    assert len(out) == 255
    assert out.endswith("\u2026")
    assert h.truncate_state("a" * 255) == "a" * 255


# --- CSQ --------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("17,99", 17), ("+CSQ: 20,0", 20), (" 0,99 ", 0), (31, 31), ("31,99", 31),
    ("99,99", None), (99, None), ("", None), (None, None), ("abc", None),
    ("32,0", None), (-1, None), (True, None),
])
def test_parse_csq(raw, expected):
    assert h.parse_csq(raw) == expected


def test_csq_to_dbm():
    assert h.csq_to_dbm(0) == -113
    assert h.csq_to_dbm(17) == -79
    assert h.csq_to_dbm(31) == -51
    assert h.csq_to_dbm(None) is None
    assert h.csq_to_dbm(h.parse_csq("99,99")) is None


# --- host / token -----------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("192.168.1.10", "192.168.1.10"),
    (" 10.0.0.1 ", "10.0.0.1"),
    ("fe80::1", "fe80::1"),
    ("[2001:db8::1]", "2001:db8::1"),
    ("2001:0db8:0000::0001", "2001:db8::1"),
    ("a0d7b954-jarvis-voice", "a0d7b954-jarvis-voice"),
    ("homeassistant.local", "homeassistant.local"),
    ("host.example.com.", "host.example.com."),
    ("host\n", "host"),
])
def test_normalize_host_valid(raw, expected):
    assert h.normalize_host(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", None, 123, "http://host", "host/path", "user@host", "host:8099",
    "bad host", "-start", "end-", "a..b", "999.1.1.1", "[10.0.0.1]", "[nothost]",
    "x" * 64, "ho\nst", "ho st.local",
])
def test_normalize_host_invalid(raw):
    assert h.normalize_host(raw) is None


def test_format_url_host():
    assert h.format_url_host("10.0.0.1") == "10.0.0.1"
    assert h.format_url_host("fe80::1") == "[fe80::1]"
    assert h.format_url_host("fe80::1%eth0") == "[fe80::1%25eth0]"


def test_normalize_token():
    assert h.normalize_token("  abc \n") == "abc"
    assert h.normalize_token(None) == ""


# --- call transitions -------------------------------------------------------
@pytest.mark.parametrize("prev,cur,expected", [
    (None, "ringing", None),
    ("idle", "idle", None),
    ("idle", "ringing", "incoming"),
    ("ringing", "connected", None),
    ("connected", "idle", "ended"),
    ("ringing", "idle", "ended"),
    ("dialing", "offline", "ended"),
    ("idle", "dialing", None),
    ("offline", "idle", None),
])
def test_call_transition(prev, cur, expected):
    assert h.call_transition(prev, cur) == expected


# --- entries ----------------------------------------------------------------
def test_is_supervisor_entry():
    assert h.is_supervisor_entry({"source": "hassio"})
    assert h.is_supervisor_entry({}, "hassio")
    assert not h.is_supervisor_entry({}, "user")


def test_resolve_entry_id():
    assert h.resolve_entry_id({"a": 1}, None) == "a"
    assert h.resolve_entry_id({"a": 1, "b": 2}, "b") == "b"
    with pytest.raises(h.EntryLookupError) as err:
        h.resolve_entry_id({"a": 1}, "zzz")
    assert err.value.code == "unknown_entry" and "zzz" in str(err.value)
    with pytest.raises(h.EntryLookupError) as err:
        h.resolve_entry_id({"a": 1, "b": 2}, None)
    assert err.value.code == "multiple_entries"
    with pytest.raises(h.EntryLookupError) as err:
        h.resolve_entry_id({}, None)
    assert err.value.code == "no_entries"


# --- redaction --------------------------------------------------------------
def test_redact():
    data = {
        "token": "supersecrettoken",
        "host": "a0d7b954-jarvis-voice",
        "status": {
            "active_number": "+79991234567",
            "events": [{"name": "caller_id", "data": {"number": "+79991234567"}},
                       {"name": "note", "data": "from 89991234567 via a0d7b954-jarvis-voice"}],
            "last_error": "auth supersecrettoken failed",
            "signal": "17,99",
            "empty_number": "",
        },
    }
    out = h.redact(data, {"token", "host", "active_number", "number"},
                   ["supersecrettoken", "a0d7b954-jarvis-voice", None, "ab"])
    dumped = json.dumps(out, ensure_ascii=False)
    for secret in ("supersecrettoken", "a0d7b954-jarvis-voice", "79991234567", "89991234567"):
        assert secret not in dumped
    assert out["status"]["signal"] == "17,99"
    assert out["token"] == h.REDACTED
    assert data["token"] == "supersecrettoken"  # input not mutated


def test_helpers_has_no_homeassistant_import():
    source = (COMPANION / "helpers.py").read_text(encoding="utf-8")
    assert "homeassistant" not in source.split('"""', 2)[2]
    assert "from ." not in source
