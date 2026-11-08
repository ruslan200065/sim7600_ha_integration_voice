"""Private, expiring messages delivered only to their intended family profile."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from uuid import uuid4


def _profile(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 160 or value in {'room', 'local'}:
        raise ValueError('Укажите авторизованный профиль участника семьи.')
    return value


def _now(value=None):
    value = value or datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('Время должно включать часовой пояс.')
    return value


class FamilyInbox:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._query('''CREATE TABLE IF NOT EXISTS family_messages (
            id TEXT PRIMARY KEY, sender TEXT NOT NULL, recipient TEXT NOT NULL,
            text TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
            delivered REAL, cancelled INTEGER NOT NULL DEFAULT 0)''')

    def _query(self, sql, values=()):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute('PRAGMA secure_delete=ON')
            rows = db.execute(sql, values).fetchall()
        self.path.chmod(0o600)
        return rows

    def send(self, sender, recipient, text, expires_at=None, now=None):
        sender, recipient = _profile(sender), _profile(recipient)
        stamp = _now(now)
        expires = _now(expires_at) if expires_at is not None else stamp + timedelta(days=7)
        if not isinstance(text, str) or not text.strip() or len(text) > 1000 or not text.isprintable():
            raise ValueError('Сообщение должно содержать от 1 до 1000 символов.')
        if expires <= stamp or expires > stamp + timedelta(days=365):
            raise ValueError('Укажите будущий срок актуальности не более года.')
        identifier = uuid4().hex
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            count = db.execute('''SELECT COUNT(*) FROM family_messages WHERE recipient=?
                AND delivered IS NULL AND cancelled=0 AND expires>?''', (recipient, stamp.timestamp())).fetchone()[0]
            if count >= 100:
                raise ValueError('У получателя уже сто ожидающих сообщений.')
            db.execute('INSERT INTO family_messages VALUES (?,?,?,?,?,?,NULL,0)',
                       (identifier, sender, recipient, text.strip(), stamp.timestamp(), expires.timestamp()))
        return {'id': identifier, 'sender': sender, 'recipient': recipient, 'expires_at': expires.isoformat(), 'saved': True}

    def pending(self, recipient, now=None):
        recipient, stamp = _profile(recipient), _now(now)
        return [{'id': i, 'sender': sender, 'recipient': recipient, 'text': text,
                 'created_at': datetime.fromtimestamp(created, timezone.utc).isoformat(),
                 'expires_at': datetime.fromtimestamp(expires, timezone.utc).isoformat()}
                for i, sender, text, created, expires in self._query('''SELECT id,sender,text,created,expires
                    FROM family_messages WHERE recipient=? AND delivered IS NULL AND cancelled=0
                    AND expires>? ORDER BY created,id LIMIT 100''', (recipient, stamp.timestamp()))]

    def delivered(self, recipient, identifier, now=None):
        """Call only after successful playback; issuing a read tool is not delivery."""
        recipient, stamp = _profile(recipient), _now(now)
        return bool(self._query('''UPDATE family_messages SET delivered=? WHERE id=? AND recipient=?
            AND delivered IS NULL AND cancelled=0 AND expires>? RETURNING id''',
            (stamp.timestamp(), identifier, recipient, stamp.timestamp())))

    def cancel(self, sender, identifier):
        return bool(self._query('''UPDATE family_messages SET cancelled=1 WHERE id=? AND sender=?
            AND delivered IS NULL AND cancelled=0 RETURNING id''', (identifier, _profile(sender))))

    def sent(self, sender, now=None):
        sender, stamp = _profile(sender), _now(now)
        return [{'id': i, 'recipient': recipient, 'text': text,
                 'status': 'cancelled' if cancelled else 'delivered' if delivered is not None else 'expired' if expires <= stamp.timestamp() else 'pending'}
                for i, recipient, text, expires, delivered, cancelled in self._query('''SELECT id,recipient,text,expires,delivered,cancelled
                    FROM family_messages WHERE sender=? ORDER BY created DESC,id LIMIT 100''', (sender,))]
