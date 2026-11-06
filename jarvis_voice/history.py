"""Persistent calls and received SMS on the add-on's private data volume."""

import asyncio
import hashlib
import logging
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from gsmmodem.exceptions import EncodingError
from gsmmodem.pdu import Concatenation, decodeSmsPdu

LOG = logging.getLogger(__name__)
ACTIVE_CALLS = ("ringing", "dialing", "connected")
PAGE_SIZE = 50


def decode_messages(lines):
    """Yield received messages from a SIM7600 PDU-mode CMGL response."""
    for header, pdu in zip(lines, lines[1:], strict=False):
        if not re.match(r"\+CMGL:\s*\d+,\s*[01],", header):
            continue
        if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}){1,256}", pdu):
            continue
        try:
            message = decodeSmsPdu(pdu)
            if message["type"] != "SMS-DELIVER":
                continue
            part = next(
                (
                    item
                    for item in message.get("udh", [])
                    if isinstance(item, Concatenation)
                ),
                None,
            )
            multipart = part and 1 <= part.number <= part.parts <= 255
            yield (
                hashlib.sha256(bytes.fromhex(pdu)).hexdigest(),
                message["time"].timestamp(),
                message["number"],
                message.get("text", ""),
                part.reference if multipart else None,
                part.number if multipart else 1,
                part.parts if multipart else 1,
            )
        except (
            EncodingError,
            ValueError,
            TypeError,
            IndexError,
            KeyError,
            StopIteration,
        ):
            LOG.warning("Could not decode one received SMS PDU")


class History:
    def __init__(self, path):
        self.path = Path(path)
        self.session = uuid.uuid4().hex

    def _run(self, operation, *args):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            return operation(conn, *args)

    async def start(self):
        await asyncio.to_thread(self._run, self._init)

    def _init(self, conn):
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY,
                call_key TEXT UNIQUE,
                kind TEXT NOT NULL,
                created_at REAL NOT NULL,
                number TEXT NOT NULL,
                direction TEXT NOT NULL,
                state TEXT NOT NULL,
                text TEXT NOT NULL DEFAULT '',
                connected_at REAL,
                ended_at REAL,
                reference INTEGER,
                total INTEGER
            );
            CREATE INDEX IF NOT EXISTS history_date ON history(created_at DESC, id DESC);
            CREATE TABLE IF NOT EXISTS sms_parts (
                digest TEXT PRIMARY KEY,
                history_id INTEGER NOT NULL REFERENCES history(id),
                part INTEGER NOT NULL,
                body TEXT NOT NULL,
                UNIQUE(history_id, part)
            );
        """)
        self.path.chmod(0o600)
        self._interrupt(conn)

    def _interrupt(self, conn):
        conn.execute(
            "UPDATE history SET state='interrupted', ended_at=? WHERE kind='call' "
            "AND state IN (?,?,?)",
            (time.time(), *ACTIVE_CALLS),
        )

    async def call(self, event, data):
        await asyncio.to_thread(self._run, self._call, event, data)

    def _call(self, conn, event, data):
        if event == "modem_offline":
            self._interrupt(conn)
            return
        key = f"{self.session}:{data['call_id']}"
        now = time.time()
        if event in {"incoming_call", "outgoing_call"}:
            incoming = event == "incoming_call"
            conn.execute(
                "INSERT OR IGNORE INTO history "
                "(call_key,kind,created_at,number,direction,state) VALUES (?,'call',?,?,?,?)",
                (
                    key,
                    now,
                    data.get("number", ""),
                    "incoming" if incoming else "outgoing",
                    "ringing" if incoming else "dialing",
                ),
            )
        elif event == "caller_id":
            conn.execute(
                "UPDATE history SET number=? WHERE call_key=?", (data["number"], key)
            )
        elif event == "call_connected":
            conn.execute(
                "UPDATE history SET state='connected', connected_at=COALESCE(connected_at,?) "
                "WHERE call_key=?",
                (now, key),
            )
        elif event in {"call_ended", "call_failed"}:
            conn.execute(
                "UPDATE history SET state=CASE WHEN connected_at IS NOT NULL THEN 'completed' "
                "WHEN direction='incoming' THEN 'missed' ELSE 'failed' END, "
                "text=?, ended_at=? WHERE call_key=? AND state IN (?,?,?)",
                (data.get("reason", "")[:160], now, key, *ACTIVE_CALLS),
            )

    async def import_sms(self, lines):
        await asyncio.to_thread(self._run, self._import_sms, lines)

    def _import_sms(self, conn, lines):
        for digest, date, sender, text, ref, part, total in decode_messages(lines):
            if conn.execute(
                "SELECT 1 FROM sms_parts WHERE digest=?", (digest,)
            ).fetchone():
                continue
            row = None
            if ref is not None:
                row = conn.execute(
                    "SELECT id FROM history WHERE kind='sms' AND number=? AND reference=? "
                    "AND total=? AND created_at BETWEEN ? AND ? AND NOT EXISTS "
                    "(SELECT 1 FROM sms_parts WHERE history_id=history.id AND part=?) "
                    "ORDER BY created_at DESC LIMIT 1",
                    (sender, ref, total, date - 600, date + 600, part),
                ).fetchone()
            if row is None:
                event_id = conn.execute(
                    "INSERT INTO history (kind,created_at,number,direction,state,reference,total) "
                    "VALUES ('sms',?,?,'incoming','partial',?,?)",
                    (date, sender, ref, total),
                ).lastrowid
            else:
                event_id = row["id"]
            conn.execute(
                "INSERT INTO sms_parts(digest,history_id,part,body) VALUES (?,?,?,?)",
                (digest, event_id, part, text),
            )
            parts = conn.execute(
                "SELECT body FROM sms_parts WHERE history_id=? ORDER BY part",
                (event_id,),
            ).fetchall()
            conn.execute(
                "UPDATE history SET state=?,text=?,created_at=MIN(created_at,?) WHERE id=?",
                (
                    "received" if len(parts) == total else f"{len(parts)}/{total}",
                    "".join(item["body"] for item in parts),
                    date,
                    event_id,
                ),
            )

    async def page(self, kind, offset):
        return await asyncio.to_thread(self._run, self._page, kind, offset)

    def _page(self, conn, kind, offset):
        rows = conn.execute(
            "SELECT kind,created_at,number,direction,state,text,connected_at,ended_at "
            "FROM history WHERE (? IS NULL OR kind=?) ORDER BY created_at DESC,id DESC "
            "LIMIT ? OFFSET ?",
            (kind, kind, PAGE_SIZE + 1, offset),
        ).fetchall()
        return {
            "items": [dict(row) for row in rows[:PAGE_SIZE]],
            "more": len(rows) > PAGE_SIZE,
        }
