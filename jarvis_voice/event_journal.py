"""A factual, timestamped journal of device state changes; no inferred narrative."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import json
import re
import sqlite3

ENTITY = re.compile(r'[a-z0-9_]+\.[a-z0-9_]+')
SAFE_ATTRIBUTES = {'friendly_name', 'device_class', 'unit_of_measurement'}


class EventJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('''CREATE TABLE IF NOT EXISTS home_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT NOT NULL, state TEXT NOT NULL,
                previous TEXT, source TEXT NOT NULL, at REAL NOT NULL, attributes TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS home_events_at ON home_events(at);''')
        self.path.chmod(0o600)

    def record(self, entity, state, source='home_assistant', at=None, previous=None, attributes=None):
        if not isinstance(entity, str) or not ENTITY.fullmatch(entity):
            raise ValueError('Укажите идентификатор устройства.')
        if not isinstance(source, str) or not source.strip() or len(source) > 160:
            raise ValueError('Укажите источник события.')
        if not isinstance(state, str) or len(state) > 500 or (previous is not None and (not isinstance(previous, str) or len(previous) > 500)):
            raise ValueError('Состояние должно быть короткой строкой.')
        stamp = at or datetime.now(timezone.utc)
        if not isinstance(stamp, datetime) or stamp.tzinfo is None:
            raise ValueError('Время должно включать часовой пояс.')
        if attributes is not None and not isinstance(attributes, dict):
            raise ValueError('Атрибуты должны быть объектом.')
        clean = {key: value[:160] for key, value in (attributes or {}).items()
                 if key in SAFE_ATTRIBUTES and isinstance(value, str)}
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            cursor = db.execute('INSERT INTO home_events(entity,state,previous,source,at,attributes) VALUES (?,?,?,?,?,?)',
                                (entity, state, previous, source.strip(), stamp.timestamp(), json.dumps(clean, ensure_ascii=False)))
            identifier = cursor.lastrowid
            db.execute('DELETE FROM home_events WHERE at<?', (stamp.timestamp()-30*86400,))
            db.execute('DELETE FROM home_events WHERE id NOT IN (SELECT id FROM home_events ORDER BY id DESC LIMIT 10000)')
        return identifier

    def list(self, profile, visible, since=None, limit=50, offset=0):
        if not isinstance(profile, str) or not profile.strip() or len(profile) > 160 or not callable(visible):
            raise ValueError('Укажите профиль и проверку доступа.')
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
            raise ValueError('Неверная страница истории.')
        if since is not None and (not isinstance(since, datetime) or since.tzinfo is None):
            raise ValueError('Начало периода должно включать часовой пояс.')
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            rows = db.execute('SELECT id,entity,state,previous,source,at,attributes FROM home_events WHERE at>=? ORDER BY at DESC,id DESC',
                              (since.timestamp() if since else 0,)).fetchall()
        allowed = [row for row in rows if visible(row[1])]
        page = allowed[offset:offset+limit]
        return {'events': [{'id': i, 'entity_id': entity, 'state': state, 'previous': previous,
                            'source': source, 'at': datetime.fromtimestamp(at, timezone.utc).isoformat(),
                            'attributes': json.loads(attrs), 'factual': True}
                           for i, entity, state, previous, source, at, attrs in page],
                'total': len(allowed), 'offset': offset,
                'next_offset': offset+len(page) if offset+len(page)<len(allowed) else None}
