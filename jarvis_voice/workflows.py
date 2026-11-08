"""Profile-scoped scenarios and cancellable, observable background workflows.

Runners must call the regular authorized tools. This module grants no device access
and never retries a physical action or attempts to undo an action already sent.
"""
import asyncio
from contextlib import closing
import copy
import json
from pathlib import Path
import sqlite3
from time import time
from uuid import uuid4

ALLOWED_STEP_TOOLS = frozenset({"call_ha_service", "get_entity_state", "announce", "send_camera_photo", "see_camera"})
MAX_STEPS = 20
MAX_SCENARIOS = 100
MAX_TASKS = 100


def validate_steps(steps):
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError("Сценарий должен содержать от 1 до 20 шагов.")
    for step in steps:
        if (not isinstance(step, dict) or set(step) - {"tool", "arguments"}
                or not isinstance(step.get("tool"), str)
                or step.get("tool") not in ALLOWED_STEP_TOOLS
                or not isinstance(step.get("arguments", {}), dict)):
            raise ValueError("Каждый шаг должен содержать разрешённый инструмент и его параметры.")
    data = json.dumps(steps, ensure_ascii=False, allow_nan=False)
    if len(data) > 32000:
        raise ValueError("Сценарий слишком большой.")
    return json.loads(data)


def expected_outcome(service, arguments, before=None):
    """Return only an observable postcondition, never a claim of physical success."""
    before = before or {}
    states = {"turn_on": "on", "turn_off": "off", "open_cover": "open",
              "close_cover": "closed", "unlock": "unlocked", "lock": "locked",
              "media_play": "playing", "media_pause": "paused"}
    entity = arguments.get("entity_id", "")
    domain = entity.split(".", 1)[0]
    if service in {"turn_on", "turn_off"} and domain not in {"light", "switch", "input_boolean", "fan", "humidifier"}:
        return None
    if service in states:
        expected = {"state": states[service]}
        if service == "turn_on" and domain == "light" and "brightness_pct" in arguments:
            expected["brightness"] = round(arguments["brightness_pct"] * 255 / 100)
        return expected
    if service == "toggle" and before.get("state") in {"on", "off"}:
        return {"state": "off" if before["state"] == "on" else "on"}
    attributes = {"volume_set": ("volume_level", "volume_level"),
                  "set_temperature": ("temperature", "temperature"),
                  "set_value": ("state", "value"), "select_option": ("state", "option"),
                  "set_percentage": ("percentage", "percentage"),
                  "set_cover_position": ("current_position", "position")}
    if service in attributes:
        field, argument = attributes[service]
        if argument in arguments:
            return {field: arguments[argument]}
    return None


def outcome_matches(row, expected):
    if not expected or row.get("state") in {None, "unknown", "unavailable"}:
        return False
    for key, value in expected.items():
        actual = row.get("state") if key == "state" else row.get("attributes", {}).get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                if abs(float(actual) - value) > (1 if key == "brightness" else 0.01):
                    return False
            except (TypeError, ValueError):
                return False
        elif actual != value:
            return False
    return True


def _failed(result):
    if isinstance(result, dict):
        return (bool(result.get("error")) or result.get("needs_confirmation") is True
                or result.get("accepted") is False or result.get("ok") is False
                or result.get("verified") is False)
    # A string is an explanatory result, not a machine-readable authorization.
    return not isinstance(result, dict)


class WorkflowManager:
    def __init__(self, path, profile):
        self.path = Path(path)
        self.profile = str(profile)
        self.tasks = {}
        self._running = {}

    def _query(self, sql, values=()):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS saved_workflows (
                profile TEXT NOT NULL, name TEXT NOT NULL, steps TEXT NOT NULL,
                updated REAL NOT NULL, PRIMARY KEY(profile,name))""")
            result = db.execute(sql, values).fetchall()
        self.path.chmod(0o600)
        return result

    def save(self, name, steps):
        name = self._name(name)
        steps = validate_steps(steps)
        for step in steps:
            step.setdefault("arguments", {}).pop("confirmed", None)
        existing = self._query("SELECT name FROM saved_workflows WHERE profile=?", (self.profile,))
        if len(existing) >= MAX_SCENARIOS and name not in {row[0] for row in existing}:
            raise ValueError("Сначала удалите ненужные сценарии.")
        self._query("INSERT OR REPLACE INTO saved_workflows VALUES (?,?,?,?)",
                    (self.profile, name, json.dumps(steps, ensure_ascii=False), time()))
        return {"saved": True, "name": name, "steps": steps}

    def list_scenarios(self):
        return [{"name": name, "steps": json.loads(steps), "updated": updated}
                for name, steps, updated in self._query(
                    "SELECT name,steps,updated FROM saved_workflows WHERE profile=? ORDER BY name", (self.profile,))]

    def delete(self, name):
        return bool(self._query("DELETE FROM saved_workflows WHERE profile=? AND name=? RETURNING name",
                                (self.profile, self._name(name))))

    def scenario(self, name):
        rows = self._query("SELECT steps FROM saved_workflows WHERE profile=? AND name=?",
                           (self.profile, self._name(name)))
        if not rows:
            raise ValueError("Сценарий не найден в вашем профиле.")
        return json.loads(rows[0][0])

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            raise ValueError("Название должно содержать от 1 до 100 символов.")
        return name.strip()

    def start(self, name, steps, runner):
        name = self._name(name)
        steps = validate_steps(steps)
        if len(self._running) >= 8:
            raise ValueError("Уже выполняется восемь задач.")
        while len(self.tasks) >= MAX_TASKS:
            old = next((key for key in self.tasks if key not in self._running), None)
            if old is None:
                break
            del self.tasks[old]
        identifier = uuid4().hex
        record = {"id": identifier, "name": name, "status": "pending", "steps": steps,
                  "results": [], "created": time(), "current_step": None}
        self.tasks[identifier] = record
        task = asyncio.create_task(self._run(record, runner))
        self._running[identifier] = task
        task.add_done_callback(lambda finished: self._running.pop(identifier, None))
        return self.get(identifier)

    async def execute(self, name, steps, runner):
        task = self.start(name, steps, runner)
        identifier = task["id"]
        try:
            await self._running[identifier]
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling() or self.tasks[identifier]["status"] != "cancelled":
                raise
        return self.get(identifier)

    async def _run(self, record, runner):
        record["status"] = "running"
        try:
            for index, step in enumerate(record["steps"]):
                record["current_step"] = index
                # Dispatch is a commit point. Cancellation can stop future steps, but
                # cannot establish whether an in-flight physical action was executed.
                record["in_flight"] = True
                result = await runner(step["tool"], copy.deepcopy(step.get("arguments", {})))
                record["in_flight"] = False
                record["results"].append({"step": index, "result": result})
                if _failed(result):
                    record["status"] = "needs_attention"
                    return
                await asyncio.sleep(0)
            record["status"] = "completed"
        except asyncio.CancelledError:
            record["status"] = "cancelled"
            record["warning"] = "Уже отправленное действие могло выполниться; отменены оставшиеся шаги."
            raise
        except Exception as exc:
            record["status"] = "failed"
            record["error"] = type(exc).__name__
        finally:
            record["finished"] = time()

    def get(self, identifier):
        record = self.tasks.get(identifier)
        if record is None:
            return {"error": "Задача не найдена в этом профиле."}
        return copy.deepcopy(record)

    def list_tasks(self):
        return [self.get(identifier) for identifier in reversed(self.tasks)]

    def cancel(self, identifier):
        task = self._running.get(identifier)
        record = self.tasks.get(identifier)
        if not record:
            return {"error": "Задача не найдена в этом профиле."}
        if task is None or task.done():
            return {"cancelled": False, "task": self.get(identifier),
                    "reason": "Задача уже завершена; выполненные действия не отменены."}
        record["status"] = "cancelled"
        record["warning"] = "Уже отправленное действие могло выполниться; отменены оставшиеся шаги."
        task.cancel()
        return {"cancelled": True, "task": self.get(identifier)}

    async def close(self):
        tasks = list(self._running.values())
        for identifier in list(self._running):
            self.cancel(identifier)
        await asyncio.gather(*tasks, return_exceptions=True)
