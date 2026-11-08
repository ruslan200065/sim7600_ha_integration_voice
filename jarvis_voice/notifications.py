"""Local recipient policies and bounded event episodes; no delivery side effects."""
from contextlib import closing
from datetime import datetime, time, timedelta, timezone
import json
import re
import sqlite3
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_POLICY = {"channel": "call", "quiet_start": "", "quiet_end": "", "timezone": "Europe/Moscow", "urgent_bypass": False, "telegram_target": ""}
POLICY_KEYS = frozenset(DEFAULT_POLICY)
CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
TELEGRAM_TARGET = re.compile(r"(?:-?[1-9][0-9]{0,19}|@[A-Za-z][A-Za-z0-9_]{4,31})")


def _profile(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 160:
        raise ValueError("Укажите профиль получателя.")
    return value


def _now(value):
    value = value or datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Время должно включать часовой пояс.")
    return value


def validate_policy(changes, current=None):
    if not isinstance(changes, dict) or changes.keys() - POLICY_KEYS:
        raise ValueError("Неизвестные настройки уведомлений.")
    policy = {**DEFAULT_POLICY, **(current or {}), **changes}
    if not isinstance(policy["channel"], str) or policy["channel"] not in {"call", "telegram"}:
        raise ValueError("Канал уведомлений: call или telegram.")
    for key in ("quiet_start", "quiet_end"):
        if not isinstance(policy[key], str) or (policy[key] and not CLOCK.fullmatch(policy[key])):
            raise ValueError("Часы тишины: HH:MM.")
    if bool(policy["quiet_start"]) != bool(policy["quiet_end"]) or (policy["quiet_start"] and policy["quiet_start"] == policy["quiet_end"]):
        raise ValueError("Укажите начало и конец часов тишины; одинаковые значения недопустимы.")
    if not isinstance(policy["timezone"], str):
        raise ValueError("Укажите часовой пояс IANA.")
    try:
        ZoneInfo(policy["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Неизвестный часовой пояс.") from exc
    if type(policy["urgent_bypass"]) is not bool:
        raise ValueError("Срочный обход часов тишины должен быть включён или выключен.")
    target = policy["telegram_target"]
    if type(target) is int:
        target = str(target)
    if not isinstance(target, str) or (target and not TELEGRAM_TARGET.fullmatch(target)):
        raise ValueError("Telegram: укажите chat_id или @username.")
    policy["telegram_target"] = target
    if policy["channel"] == "telegram" and not target:
        raise ValueError("Для Telegram нужен получатель.")
    return policy


class NotificationPolicies:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS notification_policies(profile TEXT PRIMARY KEY, policy TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS notification_episodes(
                    profile TEXT, source TEXT, id TEXT NOT NULL, first_at REAL NOT NULL,
                    last_at REAL NOT NULL, count INTEGER NOT NULL, context TEXT NOT NULL,
                    PRIMARY KEY(profile,source));
            """)
        self.path.chmod(0o600)

    def get(self, profile):
        _profile(profile)
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT policy FROM notification_policies WHERE profile=?", (profile,)).fetchone()
        return {**DEFAULT_POLICY, **(json.loads(row[0]) if row else {})}

    def set(self, profile, changes):
        _profile(profile)
        # Merge inside one write transaction so parallel independent edits are retained.
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT policy FROM notification_policies WHERE profile=?", (profile,)).fetchone()
            policy = validate_policy(changes, json.loads(row[0]) if row else None)
            db.execute("INSERT OR REPLACE INTO notification_policies VALUES (?,?)", (profile, json.dumps(policy, ensure_ascii=False)))
        return policy

    def decide(self, profile, now=None, urgent=False):
        policy = self.get(profile)
        local = _now(now).astimezone(ZoneInfo(policy["timezone"]))
        defer = None
        if policy["quiet_start"] and not (urgent and policy["urgent_bypass"]):
            start = time.fromisoformat(policy["quiet_start"])
            end = time.fromisoformat(policy["quiet_end"])
            current = local.time().replace(tzinfo=None)
            overnight = start > end
            quiet = (current >= start or current < end) if overnight else start <= current < end
            if quiet:
                day = local.date() + timedelta(days=1 if overnight and current >= start else 0)
                # Select a future occurrence across an autumn repeated hour; normalize
                # spring clock gaps through a UTC timestamp so the returned ISO is real.
                candidates = [datetime.fromtimestamp(datetime.combine(day, end, tzinfo=local.tzinfo).replace(fold=fold).timestamp(), local.tzinfo)
                              for fold in (0,1)]
                future = [candidate for candidate in candidates if candidate.timestamp() > local.timestamp()]
                defer = min(future, key=lambda value: value.timestamp()) if future else datetime.combine(day + timedelta(days=1), end, tzinfo=local.tzinfo)
        return {"channel": policy["channel"], "telegram_target": policy["telegram_target"],
                "allowed": defer is None, "defer_until": defer.isoformat() if defer else None,
                "reason": "quiet_hours" if defer else "ready"}

    def coalesce(self, profile, source, context, now=None, window_seconds=60):
        _profile(profile)
        if not isinstance(source, str) or not source.strip() or len(source) > 200:
            raise ValueError("Укажите источник события.")
        if type(window_seconds) is not int or not 1 <= window_seconds <= 3600:
            raise ValueError("Окно объединения: от 1 до 3600 секунд.")
        if not isinstance(context, dict):
            raise ValueError("Контекст события должен быть объектом.")
        serialized = json.dumps(context, ensure_ascii=False)
        if len(serialized) > 8000:
            raise ValueError("Слишком большой контекст события.")
        stamp = _now(now).timestamp()
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id,first_at,last_at,count FROM notification_episodes WHERE profile=? AND source=?", (profile,source)).fetchone()
            same = row is not None and row[1] <= stamp < row[1] + window_seconds
            identifier, first, count = (row[0], row[1], row[3] + 1) if same else (uuid4().hex, stamp, 1)
            db.execute("INSERT OR REPLACE INTO notification_episodes VALUES (?,?,?,?,?,?,?)",
                       (profile, source, identifier, first, stamp, count, serialized))
            db.execute("DELETE FROM notification_episodes WHERE last_at<?", (stamp - 86400,))
        return {"id": identifier, "source": source, "count": count, "first_at": first, "last_at": stamp,
                "context": context, "first": not same, "notify": not same}
