"""Explicit opt-in escalation for shared events, never private reminders."""
from contextlib import closing
from pathlib import Path
import json
import sqlite3


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 160:
        raise ValueError('Укажите короткий идентификатор.')
    return value


class EscalationPolicies:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('''CREATE TABLE IF NOT EXISTS event_escalation_policies (
                source TEXT PRIMARY KEY, chain TEXT NOT NULL, max_attempts INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS event_escalations (
                event_id TEXT PRIMARY KEY, source TEXT NOT NULL, chain TEXT NOT NULL,
                max_attempts INTEGER NOT NULL, position INTEGER NOT NULL DEFAULT 0);''')
        self.path.chmod(0o600)

    def set(self, source, chain, max_attempts=3):
        source = _text(source)
        if not isinstance(chain, list) or len(chain) > 10:
            raise ValueError('Укажите до десяти разных получателей.')
        chain = [_text(profile) for profile in chain]
        if len(set(chain)) != len(chain):
            raise ValueError('Получатели не должны повторяться.')
        if any(profile in {'room', 'local'} for profile in chain):
            raise ValueError('Участник должен иметь авторизованный профиль.')
        if chain and len(chain) < 2:
            raise ValueError('Для эскалации нужно минимум два получателя.')
        if type(max_attempts) is not int or not 1 <= max_attempts <= 9:
            raise ValueError('Число переходов: от 1 до 9.')
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute('INSERT OR REPLACE INTO event_escalation_policies VALUES (?,?,?)',
                       (source, json.dumps(chain), max_attempts))
        return {'source': source, 'chain': chain, 'max_attempts': max_attempts, 'enabled': bool(chain)}

    def get(self, source):
        source = _text(source)
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            row = db.execute('SELECT chain,max_attempts FROM event_escalation_policies WHERE source=?', (source,)).fetchone()
        return {'source': source, 'chain': json.loads(row[0]) if row else [], 'max_attempts': row[1] if row else 0, 'enabled': bool(row and json.loads(row[0]))}

    def policies(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            rows = db.execute('SELECT source,chain,max_attempts FROM event_escalation_policies ORDER BY source').fetchall()
        return [{'source': source, 'chain': json.loads(chain), 'max_attempts': maximum, 'enabled': bool(json.loads(chain))}
                for source, chain, maximum in rows]

    def advance(self, event_id, source, current_profile, is_private=True, attempts=0):
        """Caller invokes only after delivery failure; retries cannot restart a chain."""
        event_id, source, current_profile = _text(event_id), _text(source), _text(current_profile)
        if is_private is not False:
            return {'escalate': False, 'reason': 'private_reminder'}
        if type(attempts) is not int or attempts < 0:
            raise ValueError('Неверное число попыток.')
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT source,chain,max_attempts,position FROM event_escalations WHERE event_id=?', (event_id,)).fetchone()
            if row:
                saved_source, raw_chain, maximum, position = row
                if source != saved_source:
                    return {'escalate': False, 'reason': 'event_source_mismatch'}
                chain = json.loads(raw_chain)
            else:
                policy = db.execute('SELECT chain,max_attempts FROM event_escalation_policies WHERE source=?', (source,)).fetchone()
                if not policy:
                    return {'escalate': False, 'reason': 'not_configured'}
                raw_chain, maximum = policy
                chain, position = json.loads(raw_chain), 0
                if not chain:
                    return {'escalate': False, 'reason': 'disabled'}
                db.execute('INSERT INTO event_escalations VALUES (?,?,?,?,0)', (event_id, source, raw_chain, maximum))
            if position >= len(chain) or current_profile != chain[position]:
                return {'escalate': False, 'reason': 'recipient_mismatch'}
            if position >= maximum or position+1 >= len(chain):
                return {'escalate': False, 'reason': 'exhausted'}
            position += 1
            db.execute('UPDATE event_escalations SET position=? WHERE event_id=?', (position, event_id))
        return {'escalate': True, 'recipient': chain[position], 'attempt': position, 'reason': 'explicit_policy'}
