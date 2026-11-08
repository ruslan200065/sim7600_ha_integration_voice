"""Explicit, profile-scoped corrections and device autonomy boundaries."""
from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import threading

ENTITY = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")
DOMAIN = re.compile(r"[a-z][a-z0-9_]*")
RANGE_FIELDS = {"temperature", "brightness_pct", "percentage", "volume_level", "value", "position"}


def profile_id(profile):
    if not isinstance(profile, str) or not profile.strip() or len(profile) > 160:
        raise ValueError("Укажите личный профиль.")
    return profile


def phrase_key(phrase):
    if not isinstance(phrase, str) or not phrase.strip() or len(phrase) > 500:
        raise ValueError("Укажите короткую фразу для исправления.")
    key = " ".join(re.findall(r"[a-zа-я0-9]+", phrase.lower().replace("ё", "е")))
    if not key:
        raise ValueError("Фраза должна содержать слова.")
    return key


class Autonomy:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._ready = False

    def _query(self, sql, values=()):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("PRAGMA secure_delete=ON")
            if not self._ready:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS explicit_corrections (
                        profile TEXT, phrase TEXT, entity TEXT, updated TEXT,
                        PRIMARY KEY(profile,phrase));
                    CREATE TABLE IF NOT EXISTS autonomy_rules (
                        profile TEXT, target TEXT, mode TEXT, ranges TEXT, updated TEXT,
                        PRIMARY KEY(profile,target));
                """)
                self._ready = True
            self.path.chmod(0o600)
            return db.execute(sql, values).fetchall()

    def learn(self, profile, phrase, entity, confirmed=False):
        profile_id(profile)
        key = phrase_key(phrase)
        if not isinstance(entity, str) or not ENTITY.fullmatch(entity):
            raise ValueError("Выберите устройство Home Assistant.")
        if confirmed is not True:
            return {"saved": False, "needs_confirmation": True, "phrase": key, "entity": entity,
                    "instruction": "Предложи запомнить исправленный выбор и дождись согласия человека."}
        self._query("INSERT OR REPLACE INTO explicit_corrections VALUES (?,?,?,?)",
                    (profile, key, entity, datetime.now(timezone.utc).isoformat()))
        return {"saved": True, "phrase": key, "entity": entity}

    def resolve(self, profile, phrase):
        profile_id(profile)
        rows = self._query("SELECT entity FROM explicit_corrections WHERE profile=? AND phrase=?",
                           (profile, phrase_key(phrase)))
        return rows[0][0] if rows else ""

    def corrections(self, profile):
        profile_id(profile)
        return [{"phrase": phrase, "entity": entity, "updated_at": updated, "source": "explicit_correction"}
                for phrase, entity, updated in self._query(
                    "SELECT phrase,entity,updated FROM explicit_corrections WHERE profile=? ORDER BY updated DESC", (profile,))]

    def forget(self, profile, phrase):
        profile_id(profile)
        return bool(self._query("DELETE FROM explicit_corrections WHERE profile=? AND phrase=? RETURNING phrase",
                                (profile, phrase_key(phrase))))

    def set_rule(self, profile, target, mode, ranges=None):
        profile_id(profile)
        if not isinstance(target, str) or not (ENTITY.fullmatch(target) or DOMAIN.fullmatch(target)):
            raise ValueError("Укажите устройство или домен Home Assistant.")
        if not isinstance(mode, str) or mode not in {"allow", "confirm", "observe"}:
            raise ValueError("Режим: allow, confirm или observe.")
        ranges = {} if ranges is None else ranges
        if not isinstance(ranges, dict) or ranges.keys() - RANGE_FIELDS:
            raise ValueError("Неизвестные диапазоны параметров устройства.")
        for limits in ranges.values():
            if not isinstance(limits, dict) or not limits or limits.keys() - {"min", "max"}:
                raise ValueError("Диапазон должен содержать min и/или max.")
            if any(type(value) not in (int, float) or not math.isfinite(value) for value in limits.values()):
                raise ValueError("Границы диапазона должны быть конечными числами.")
            if limits.get("min", -math.inf) > limits.get("max", math.inf):
                raise ValueError("Минимум не должен превышать максимум.")
        if len(self.rules(profile)) >= 128 and not any(rule["target"] == target for rule in self.rules(profile)):
            raise ValueError("Сначала удалите ненужные правила самостоятельности.")
        now = datetime.now(timezone.utc).isoformat()
        self._query("INSERT OR REPLACE INTO autonomy_rules VALUES (?,?,?,?,?)",
                    (profile, target, mode, json.dumps(ranges, allow_nan=False), now))
        return {"target": target, "mode": mode, "ranges": ranges, "updated_at": now}

    def rules(self, profile):
        profile_id(profile)
        return [{"target": target, "mode": mode, "ranges": json.loads(ranges), "updated_at": updated}
                for target, mode, ranges, updated in self._query(
                    "SELECT target,mode,ranges,updated FROM autonomy_rules WHERE profile=? ORDER BY target", (profile,))]

    def remove_rule(self, profile, target):
        profile_id(profile)
        return bool(self._query("DELETE FROM autonomy_rules WHERE profile=? AND target=? RETURNING target", (profile, target)))

    def evaluate(self, profile, entity, service, args):
        profile_id(profile)
        if not isinstance(entity, str) or not ENTITY.fullmatch(entity) or not isinstance(args, dict) or not isinstance(service, str):
            return {"decision": "deny", "reason": "Неверные параметры устройства."}
        rows = {rule["target"]: rule for rule in self.rules(profile)}
        rule = rows.get(entity) or rows.get(entity.split(".", 1)[0])
        if not rule:
            return {"decision": "default", "reason": "Действуют обычные разрешения профиля."}
        if rule["mode"] == "observe":
            return {"decision": "deny", "reason": "Для устройства разрешено только наблюдение.", "rule": rule}
        for field, limits in rule["ranges"].items():
            if field not in args:
                continue
            value = args[field]
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or value < limits.get("min", -math.inf) or value > limits.get("max", math.inf)):
                return {"decision": "deny", "reason": "Запрошенное значение вне разрешённого диапазона.", "field": field, "rule": rule}
        return {"decision": rule["mode"], "reason": "Правило самостоятельности профиля.", "rule": rule}
