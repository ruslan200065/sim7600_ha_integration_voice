"""Persisted personal automation previews. This module never executes an action."""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import threading
from personal import condition_matches

ENTITY = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")


def _profile(profile):
    if not isinstance(profile, str) or not profile.strip() or len(profile) > 160:
        raise ValueError("Выберите личный профиль.")
    return profile


def _aware_time(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Время должно содержать часовой пояс.")
    return value


def validate_condition(condition):
    if not isinstance(condition, dict):
        raise ValueError("Укажите условие проверки.")
    kind = condition.get("kind")
    if kind == "time":
        _aware_time(condition.get("when"))
        allowed = {"kind", "when"}
    elif kind in {"state", "zone"}:
        if not isinstance(condition.get("entity"), str) or not ENTITY.fullmatch(condition["entity"]):
            raise ValueError("Выберите сущность условия.")
        if kind == "zone":
            if not isinstance(condition.get("zone"), str) or not ENTITY.fullmatch(condition["zone"]) or not condition["zone"].startswith("zone."):
                raise ValueError("Выберите зону Home Assistant.")
            allowed = {"kind", "entity", "zone"}
        else:
            if condition.get("operator") not in {"equals", "above", "below"} or not isinstance(condition.get("value"), str):
                raise ValueError("Укажите оператор equals/above/below и значение условия.")
            if condition["operator"] in {"above", "below"}:
                import math
                if not math.isfinite(float(condition["value"])):
                    raise ValueError("Порог должен быть конечным числом.")
            allowed = {"kind", "entity", "operator", "value"}
    else:
        raise ValueError("Тип условия: state, zone или time.")
    if condition.keys() - allowed:
        raise ValueError("В условии есть неизвестные поля.")
    return json.loads(json.dumps(condition, ensure_ascii=False, allow_nan=False))


def validate_action(action):
    if not isinstance(action, dict) or set(action) - {"tool", "arguments"} or not isinstance(action.get("arguments", {}), dict):
        raise ValueError("Укажите действие и параметры.")
    if action.get("tool") not in {"call_ha_service", "notify", "announce", "send_camera_photo"}:
        raise ValueError("Это действие недоступно в пробном правиле.")
    if action["tool"] in {"call_ha_service", "announce"}:
        entity = action.get("arguments", {}).get("entity_id")
        if not isinstance(entity, str) or not ENTITY.fullmatch(entity):
            raise ValueError("Выберите устройство действия.")
    if action["tool"] == "call_ha_service" and not isinstance(action["arguments"].get("service"), str):
        raise ValueError("Выберите действие устройства.")
    data = json.dumps(action, ensure_ascii=False, allow_nan=False)
    if len(data) > 8000:
        raise ValueError("Действие слишком большое.")
    return json.loads(data)


class Rules:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._ready = False

    def _query(self, sql, values=()):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("PRAGMA secure_delete=ON")
            if not self._ready:
                db.execute("""CREATE TABLE IF NOT EXISTS preview_rules (
                    profile TEXT, name TEXT, condition TEXT, action TEXT, recipient TEXT,
                    updated TEXT, PRIMARY KEY(profile,name))""")
                self._ready = True
            self.path.chmod(0o600)
            return db.execute(sql, values).fetchall()

    def save(self, profile, name, condition, action, recipient=None):
        _profile(profile)
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError("Название правила: от 1 до 100 символов.")
        recipient = profile if recipient is None else _profile(recipient)
        condition, action = validate_condition(condition), validate_action(action)
        if len(self.list(profile)) >= 100 and name.strip() not in {row["name"] for row in self.list(profile)}:
            raise ValueError("Сначала удалите ненужные пробные правила.")
        self._query("INSERT OR REPLACE INTO preview_rules VALUES (?,?,?,?,?,?)",
                    (profile, name.strip(), json.dumps(condition, ensure_ascii=False), json.dumps(action, ensure_ascii=False),
                     recipient, datetime.now(timezone.utc).isoformat()))
        return {"saved": True, "name": name.strip(), "dry_run": True, "enabled": False}

    def list(self, profile):
        _profile(profile)
        return [{"name": name, "condition": json.loads(condition), "action": json.loads(action),
                 "recipient": recipient, "updated_at": updated, "dry_run": True, "enabled": False}
                for name, condition, action, recipient, updated in self._query(
                    "SELECT name,condition,action,recipient,updated FROM preview_rules WHERE profile=? ORDER BY name", (profile,))]

    def delete(self, profile, name):
        _profile(profile)
        return bool(self._query("DELETE FROM preview_rules WHERE profile=? AND name=? RETURNING name", (profile, name)))

    def preview(self, profile, name, states, now=None):
        _profile(profile)
        rule = next((row for row in self.list(profile) if row["name"] == name), None)
        if rule is None:
            raise ValueError("Пробное правило не найдено в вашем профиле.")
        if not isinstance(states, dict):
            raise ValueError("Укажите текущие состояния устройств.")
        now = _aware_time(now or datetime.now(timezone.utc))
        condition = rule["condition"]
        if condition["kind"] == "time":
            matched = now >= _aware_time(condition["when"])
        else:
            matched = condition_matches(condition, states)
        return {**rule, "would_trigger": matched is True, "condition_known": matched is not None,
                "evaluated_at": now.isoformat(), "executed": False,
                "explanation": "Пробная оценка текущего условия: действия, звонки и сообщения не выполнялись. При активации одноразового правила переход состояния оценивается отдельно."}

    def evaluate_all(self, profile, states, now=None):
        return [self.preview(profile, row["name"], states, now) for row in self.list(profile)]
