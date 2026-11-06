"""Local durable preferences and reminders, keyed by profile."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import json
from math import asin, cos, radians, sin, sqrt
from uuid import uuid4


class Personal:
    def __init__(self, path):
        self.path = Path(path)

    def query(self, sql, values=()):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("PRAGMA secure_delete=ON")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS notes (
                    profile TEXT, note TEXT, PRIMARY KEY (profile, note));
                CREATE TABLE IF NOT EXISTS preferences (
                    profile TEXT, phrase TEXT, entity TEXT, count INTEGER,
                    PRIMARY KEY (profile, phrase, entity));
                CREATE TABLE IF NOT EXISTS reminders (
                    id TEXT PRIMARY KEY, profile TEXT, name TEXT, text TEXT,
                    due REAL, delivered INTEGER DEFAULT 0, retry_at REAL DEFAULT 0, condition TEXT, armed INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS action_runs (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL);
            """)
            self.path.chmod(0o600)
            return db.execute(sql, values).fetchall()

    def notes(self, profile):
        return [row[0] for row in self.query(
            "SELECT note FROM notes WHERE profile=? ORDER BY rowid DESC LIMIT 24", (profile,))]

    def remember(self, profile, note):
        self.query("INSERT OR IGNORE INTO notes VALUES (?, ?)", (profile, note))

    def forget(self, profile, note):
        return bool(self.query(
            "DELETE FROM notes WHERE profile=? AND note=? RETURNING note", (profile, note)))

    def learned(self, profile, phrase):
        rows = self.query(
            "SELECT entity FROM preferences WHERE profile=? AND phrase=? AND count>=2",
            (profile, phrase))
        return rows[0][0] if len(rows) == 1 else ""

    def record(self, profile, phrase, entity):
        self.query("""INSERT INTO preferences VALUES (?, ?, ?, 1)
            ON CONFLICT(profile, phrase, entity) DO UPDATE SET count=count+1""",
            (profile, phrase, entity))

    def reminder(self, profile, name, text, due=None, condition=None):
        identifier = uuid4().hex
        self.query("INSERT INTO reminders (id,profile,name,text,due,condition) VALUES (?, ?, ?, ?, ?, ?)",
                   (identifier, profile, name, text, due.timestamp() if due else None,
                    json.dumps(condition, ensure_ascii=False) if condition else None))
        return identifier

    def pending(self, profile):
        return [{"id": i, "text": t, "when": datetime.fromtimestamp(d, timezone.utc).isoformat() if d else None,
                 "condition": json.loads(c) if c else None}
                for i,t,d,c in self.query(
                    "SELECT id,text,due,condition FROM reminders WHERE profile=? AND delivered=0 ORDER BY due LIMIT 20",
                    (profile,))]

    def cancel(self, profile, identifier):
        return bool(self.query("DELETE FROM reminders WHERE profile=? AND id=? AND delivered=0 RETURNING id",
                               (profile, identifier)))

    def due(self, now, profiles):
        if not profiles:
            return []
        placeholders = ",".join("?" for _ in profiles)
        return self.query(f"""SELECT id,profile,text FROM reminders
            WHERE delivered=0 AND due<=? AND retry_at<=? AND profile IN ({placeholders})
            AND json_type(condition, '$.action') IS NULL
            ORDER BY due LIMIT 20""", (now.timestamp(), now.timestamp(), *profiles))

    def due_actions(self, now):
        return [(i, p, json.loads(c)) for i,p,c in self.query(
            """SELECT id,profile,condition FROM reminders WHERE delivered=0 AND due<=?
            AND json_type(condition, '$.action') IS NOT NULL ORDER BY due LIMIT 20""",
            (now.timestamp(),))]

    def claim_action(self, identifier):
        return bool(self.query(
            "UPDATE reminders SET delivered=1 WHERE id=? AND delivered=0 RETURNING id", (identifier,)))

    def action_status(self, identifier, status):
        self.query("INSERT OR REPLACE INTO action_runs VALUES (?, ?)", (identifier, status))

    def action_history(self, profile):
        return [{"id": i, "text": text, "status": status} for i,text,status in self.query(
            """SELECT reminders.id,text,status FROM action_runs JOIN reminders USING(id)
            WHERE profile=? ORDER BY reminders.due DESC LIMIT 10""", (profile,))]

    def watches(self):
        return [(i, p, json.loads(c), armed) for i, p, c, armed in self.query(
            "SELECT id,profile,condition,armed FROM reminders WHERE delivered=0 AND due IS NULL AND condition IS NOT NULL")]

    def sample(self, identifier, matched, armed, now):
        if matched is False and not armed:
            self.query("UPDATE reminders SET armed=1 WHERE id=? AND due IS NULL", (identifier,))
        elif matched is True and armed:
            self.query("UPDATE reminders SET due=? WHERE id=? AND due IS NULL", (now.timestamp(), identifier))

    def retry_after(self, identifier, time):
        self.query("UPDATE reminders SET retry_at=? WHERE id=?", (time.timestamp(), identifier))

    def delivered(self, identifier):
        self.query("UPDATE reminders SET delivered=1 WHERE id=?", (identifier,))

def condition_matches(condition, states):
    row = states.get(condition["entity"], {})
    if row.get("state") in (None, "unknown", "unavailable"):
        return None
    if condition["kind"] == "zone":
        zone = states.get(condition["zone"], {})
        if zone.get("state") in (None, "unknown", "unavailable"):
            return None
        try:
            a, b = row["attributes"], zone["attributes"]
            lat_a, lat_b = radians(a["latitude"]), radians(b["latitude"])
            delta_lon = radians(b["longitude"] - a["longitude"])
            h = sin((lat_b - lat_a) / 2) ** 2 + cos(lat_a) * cos(lat_b) * sin(delta_lon / 2) ** 2
            return 12_742_000 * asin(sqrt(min(1, h))) <= b["radius"]
        except (KeyError, TypeError, ValueError):
            return None
    value = condition["value"]
    if condition["operator"] == "equals":
        return row["state"] == value
    try:
        actual, threshold = float(row["state"]), float(value)
        return actual > threshold if condition["operator"] == "above" else actual < threshold
    except (ValueError, TypeError):
        return None
