"""Administrator-maintained home vocabulary and scoped memory provenance."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import json
import re
import sqlite3
import threading

ENTITY = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")


class Knowledge:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._ready = False

    def query(self, sql, values=()):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, closing(sqlite3.connect(self.path)) as db, db:
            db.execute("PRAGMA secure_delete=ON")
            if not self._ready:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS home_vocabulary (
                        entity TEXT PRIMARY KEY, aliases TEXT NOT NULL, room TEXT NOT NULL,
                        purpose TEXT NOT NULL, updated_at TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS memory_provenance (
                        profile TEXT, text TEXT, source TEXT, kind TEXT,
                        created_at TEXT, updated_at TEXT, PRIMARY KEY(profile,text));
                """)
                self._ready = True
            self.path.chmod(0o600)
            return db.execute(sql, values).fetchall()

    @staticmethod
    def clean(value, length):
        if not isinstance(value, str) or len(value) > length or not value.isprintable() and value:
            raise ValueError("Используйте обычный текст без управляющих символов.")
        return value.strip()

    def set_entity(self, entity, aliases, room="", purpose=""):
        if not isinstance(entity, str) or not ENTITY.fullmatch(entity):
            raise ValueError("Выберите сущность Home Assistant.")
        if not isinstance(aliases, list) or len(aliases) > 20:
            raise ValueError("Допускается до 20 разговорных названий.")
        aliases = list(dict.fromkeys(self.clean(item, 100) for item in aliases))
        aliases = [item for item in aliases if item]
        now = datetime.now(timezone.utc).isoformat()
        self.query("INSERT OR REPLACE INTO home_vocabulary VALUES (?,?,?,?,?)",
                   (entity, json.dumps(aliases, ensure_ascii=False), self.clean(room, 100), self.clean(purpose, 300), now))
        return self.entities()[entity]

    def entities(self):
        return {entity: {"aliases": json.loads(aliases), "room": room, "purpose": purpose, "updated_at": updated}
                for entity, aliases, room, purpose, updated in self.query("SELECT * FROM home_vocabulary ORDER BY entity")}

    def remove_entity(self, entity):
        return bool(self.query("DELETE FROM home_vocabulary WHERE entity=? RETURNING entity", (entity,)))

    def remember(self, profile, text, source="voice", kind="fact"):
        text = self.clean(text, 500)
        if not text or source not in {"voice", "panel", "import"} or kind not in {"fact", "preference", "name"}:
            raise ValueError("Неверный тип записи памяти.")
        now = datetime.now(timezone.utc).isoformat()
        self.query("""INSERT INTO memory_provenance VALUES (?,?,?,?,?,?)
            ON CONFLICT(profile,text) DO UPDATE SET source=excluded.source,kind=excluded.kind,updated_at=excluded.updated_at""",
                   (profile, text, source, kind, now, now))

    def memories(self, profile):
        return [{"owner": profile, "text": text, "source": source, "kind": kind, "created_at": created, "updated_at": updated}
                for text, source, kind, created, updated in self.query(
                    "SELECT text,source,kind,created_at,updated_at FROM memory_provenance WHERE profile=? ORDER BY created_at DESC", (profile,))]

    def forget(self, profile, text):
        return bool(self.query("DELETE FROM memory_provenance WHERE profile=? AND text=? RETURNING text", (profile, text)))

    def rename_memory(self, profile, old_text, text):
        """Keep the original provenance date when an administrator edits a note."""
        text = self.clean(text, 500)
        if not text:
            raise ValueError("Запись памяти не может быть пустой.")
        rows = self.query("SELECT created_at,kind FROM memory_provenance WHERE profile=? AND text=?", (profile, old_text))
        if not rows:
            self.remember(profile, text, "panel")
            return
        created, kind = rows[0]
        now = datetime.now(timezone.utc).isoformat()
        self.query("""UPDATE memory_provenance SET text=?,source='panel',updated_at=?
            WHERE profile=? AND text=?""", (text, now, profile, old_text))
        return {"created_at": created, "kind": kind}
