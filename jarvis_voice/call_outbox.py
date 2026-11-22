"""Persist call events until Home Assistant acknowledges delivery."""

import json
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path


class CallOutbox:
    def __init__(self, path):
        self.path = Path(path)
        with self._connect() as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS stream (id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_created ON events(created_at);
            """)
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM stream").fetchone()
            self.stream = row[0] if row else secrets.token_hex(16)
            if not row:
                db.execute("INSERT INTO stream VALUES (?)", (self.stream,))
            # An interrupted direct send has unknown delivery. Retry with the same ID.
            db.execute("UPDATE events SET state='companion' WHERE state='pending'")
        self.path.chmod(0o600)

    def _connect(self):
        return closing(sqlite3.connect(self.path, timeout=1))

    def record(self, event):
        created_at = time.time()
        with self._connect() as db, db:
            cursor = db.execute(
                "INSERT INTO events(name,payload,state,created_at) VALUES (?,?,?,?)",
                (
                    event["name"],
                    json.dumps(event["data"]),
                    event["delivery"],
                    created_at,
                ),
            )
            db.execute(
                "DELETE FROM events WHERE created_at < ?", (created_at - 30 * 86400,)
            )
            return cursor.lastrowid

    def update(self, event):
        with self._connect() as db, db:
            if event["delivery"] == "direct":
                db.execute("DELETE FROM events WHERE sequence=?", (event["sequence"],))
            else:
                db.execute(
                    "UPDATE events SET state=?, payload=? WHERE sequence=?",
                    (event["delivery"], json.dumps(event["data"]), event["sequence"]),
                )

    def batch(self):
        with self._connect() as db:
            return [
                {
                    "sequence": sequence,
                    "name": name,
                    "data": json.loads(payload),
                    "delivery": state,
                }
                for sequence, name, payload, state in db.execute(
                    "SELECT sequence,name,payload,state FROM events ORDER BY sequence LIMIT 128"
                )
            ]

    def acknowledge(self, stream, sequence):
        if stream != self.stream:
            return False
        with self._connect() as db, db:
            db.execute(
                "DELETE FROM events WHERE sequence <= ? AND state='companion'",
                (sequence,),
            )
        return True
