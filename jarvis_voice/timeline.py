"""Bounded, recipient-scoped diagnostics for conversations and action timing."""
from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3

SECRET_KEY = re.compile(r"(?:api.?key|token|password|authorization|secret|pin|dtmf|image)", re.I)
SECRET_TEXT = re.compile(r"eyJ[A-Za-z0-9_-]{12,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{20,}|(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AQVN[A-Za-z0-9_-]{20,})")


def safe_details(value, depth=0):
    if depth >= 6:
        return "…"
    if isinstance(value, dict):
        return {str(key)[:80]: "[скрыто]" if SECRET_KEY.search(str(key)) else safe_details(item, depth + 1)
                for key, item in list(value.items())[:30]}
    if isinstance(value, (list,tuple)):
        return [safe_details(item, depth + 1) for item in value[:30]]
    if isinstance(value, str):
        return SECRET_TEXT.sub("[скрыто]", value)[:2000]
    if value is None or type(value) in (bool,int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return "[неподдерживаемое значение]"


class Timeline:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS conversation_timeline(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, profile TEXT NOT NULL, call_id TEXT,
                    at REAL NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL,
                    duration_ms REAL, details TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS conversation_timeline_profile ON conversation_timeline(profile,id);
            """)
        self.path.chmod(0o600)

    def record(self, profile, call_id, stage, status, duration_ms=None, details=None, at=None):
        if not isinstance(profile, str) or not profile.strip() or len(profile) > 160:
            raise ValueError("Укажите профиль разговора.")
        if call_id is not None and (type(call_id) not in (str,int) or len(str(call_id)) > 160):
            raise ValueError("Неверный идентификатор разговора.")
        for value in (stage,status):
            if not isinstance(value,str) or not value.strip() or len(value) > 80:
                raise ValueError("Укажите этап и статус.")
        if duration_ms is not None and (type(duration_ms) not in (float,int) or not math.isfinite(duration_ms) or duration_ms < 0):
            raise ValueError("Длительность должна быть неотрицательным числом.")
        stamp = at or datetime.now(timezone.utc)
        if not isinstance(stamp, datetime) or stamp.tzinfo is None:
            raise ValueError("Время должно включать часовой пояс.")
        clean = {} if re.search(r"dtmf|pin", stage, re.I) else safe_details(details or {})
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            cursor = db.execute("INSERT INTO conversation_timeline(profile,call_id,at,stage,status,duration_ms,details) VALUES (?,?,?,?,?,?,?)",
                                (profile, str(call_id) if call_id is not None else None, stamp.timestamp(), stage, status, duration_ms,
                                 json.dumps(clean, ensure_ascii=False)))
            identifier = cursor.lastrowid
            db.execute("DELETE FROM conversation_timeline WHERE at<?", (stamp.timestamp()-30*86400,))
            db.execute("DELETE FROM conversation_timeline WHERE profile=? AND id NOT IN (SELECT id FROM conversation_timeline WHERE profile=? ORDER BY id DESC LIMIT 5000)", (profile,profile))
        return identifier

    def list(self, profile, call_id=None, limit=100, offset=0):
        if not isinstance(profile, str) or not profile.strip() or len(profile) > 160:
            raise ValueError("Укажите профиль разговора.")
        if type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
            raise ValueError("Неверная страница журнала.")
        where = "profile=?"
        args = [profile]
        if call_id is not None:
            where += " AND call_id=?"
            args.append(str(call_id))
        with closing(sqlite3.connect(self.path)) as db:
            total = db.execute("SELECT COUNT(*) FROM conversation_timeline WHERE " + where, args).fetchone()[0]
            rows = db.execute("SELECT id,call_id,at,stage,status,duration_ms,details FROM conversation_timeline WHERE " + where + " ORDER BY id DESC LIMIT ? OFFSET ?", (*args,limit,offset)).fetchall()
        return {"items": [{"id": i, "call_id": call, "at": datetime.fromtimestamp(at,timezone.utc).isoformat(),
                           "stage":stage,"status":status,"duration_ms":duration,"details":json.loads(details)}
                          for i,call,at,stage,status,duration,details in rows],
                "total":total,"offset":offset,"next_offset":offset+len(rows) if offset+len(rows)<total else None}
