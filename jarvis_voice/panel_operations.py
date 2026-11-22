"""Ingress views for profile data, device vocabulary, history and audits."""

import asyncio
import logging
import re
from datetime import datetime, timezone

from aiohttp import ClientError, web
from brain import SAFE_SERVICES
from setup_options import ENTITY_ID
from setup_options import validate_changes as validate_changes
from workflows import validate_steps

LOG = logging.getLogger(__name__)


class PanelOperations:
    def personal_profiles(self):
        service = self.service
        return {
            service.owner_key or "local": service.o.get("owner_name") or "Владелец",
            **{member["key"]: member["name"] for member in service.family.values()},
        }

    async def personal(self, request):
        profiles = self.personal_profiles()
        profile = request.query.get("profile") or next(iter(profiles))
        if profile not in profiles:
            raise web.HTTPNotFound(text="Профиль не найден.")
        data = await asyncio.to_thread(self.service.personal.dashboard, profile)
        knowledge = getattr(self.service, "knowledge", None)
        if knowledge:
            metadata = {
                row["text"]: row
                for row in await asyncio.to_thread(knowledge.memories, profile)
            }
            for note in data["notes"]:
                note.update(
                    {**metadata.get(note["text"], {}), "owner_name": profiles[profile]}
                )
        return web.json_response(
            {
                **data,
                "profile": profile,
                "profiles": [
                    {"id": key, "name": name} for key, name in profiles.items()
                ],
            }
        )

    async def operations(self, request):
        profiles = self.personal_profiles()
        profile = request.query.get("profile") or next(iter(profiles))
        if profile not in profiles:
            raise web.HTTPNotFound(text="Профиль не найден.")
        policies = getattr(self.service, "policies", None)
        timeline = getattr(self.service, "timeline", None)
        if policies is None or timeline is None:
            raise web.HTTPServiceUnavailable(text="Службы задач ещё не готовы.")
        member = next(
            (item for item in self.service.family.values() if item["key"] == profile),
            None,
        )
        brain = self.service.caller_brain(member) if member else self.service.brain
        manager = getattr(getattr(brain, "features", None), "workflows", None)
        try:
            if request.method == "PATCH":
                data = await request.json()
                if not isinstance(data, dict) or set(data) != {"policy"}:
                    raise ValueError("Ожидались настройки уведомлений.")
                policy = await asyncio.to_thread(policies.set, profile, data["policy"])
                return web.json_response({"policy": policy})
            if request.method == "POST":
                data = await request.json()
                if not isinstance(data, dict) or manager is None:
                    raise ValueError("Задачи этого профиля недоступны.")
                if data.get("action") == "cancel_task" and set(data) == {
                    "action",
                    "id",
                }:
                    if not isinstance(data["id"], str):
                        raise ValueError("Укажите задачу.")
                    return web.json_response(manager.cancel(data["id"]))
                if data.get("action") == "delete_scenario" and set(data) == {
                    "action",
                    "name",
                }:
                    return web.json_response(
                        {
                            "deleted": await asyncio.to_thread(
                                manager.delete, data["name"]
                            )
                        }
                    )
                if data.get("action") == "save_scenario" and set(data) == {
                    "action",
                    "name",
                    "steps",
                }:
                    steps = validate_steps(data["steps"])
                    for step in steps:
                        args = step.get("arguments", {})
                        entity = args.get("entity_id")
                        if entity and (
                            not isinstance(entity, str) or not brain._visible(entity)
                        ):
                            raise ValueError("Устройство недоступно этому участнику.")
                        if (
                            step["tool"]
                            in {"call_ha_service", "get_entity_state", "announce"}
                            and not entity
                        ):
                            raise ValueError("Выберите устройство для каждого шага.")
                        if step["tool"] == "call_ha_service" and args.get(
                            "service"
                        ) not in brain._control_actions(entity):
                            raise ValueError("Действие недоступно этому участнику.")
                        if step["tool"] == "announce" and not brain._control_actions(
                            entity
                        ):
                            raise ValueError("Оповещение недоступно этому участнику.")
                        camera = args.get("camera")
                        if (
                            step["tool"] in {"see_camera", "send_camera_photo"}
                            and camera
                            and (
                                not isinstance(camera, str)
                                or not brain.camera_allowed(camera)
                            )
                        ):
                            raise ValueError("Камера недоступна этому участнику.")
                        # A stored scenario is a plan, never a persistent confirmation.
                        args.pop("confirmed", None)
                    saved = await asyncio.to_thread(manager.save, data["name"], steps)
                    return web.json_response(saved)
                raise ValueError("Неизвестное действие с задачей.")
            offset = int(request.query.get("offset", 0))
            policy, trace, scenarios = await asyncio.gather(
                asyncio.to_thread(policies.get, profile),
                asyncio.to_thread(timeline.list, profile, limit=100, offset=offset),
                asyncio.to_thread(manager.list_scenarios)
                if manager
                else asyncio.sleep(0, result=[]),
            )
            return web.json_response(
                {
                    "profile": profile,
                    "profiles": [
                        {"id": key, "name": value} for key, value in profiles.items()
                    ],
                    "policy": policy,
                    "timeline": trace,
                    "tasks": manager.list_tasks() if manager else [],
                    "scenarios": scenarios,
                }
            )
        except (ValueError, TypeError, OverflowError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc

    async def personal_edit(self, request):
        profile = request.query.get("profile")
        kind, identifier = request.match_info["kind"], request.match_info["id"]
        if profile not in self.personal_profiles() or kind not in {
            "notes",
            "preferences",
            "reminders",
        }:
            raise web.HTTPNotFound(text="Запись не найдена.")
        store = self.service.personal
        knowledge = getattr(self.service, "knowledge", None)
        old_note = None
        if kind == "notes" and knowledge:
            current = await asyncio.to_thread(store.dashboard, profile)
            old_note = next(
                (
                    row["text"]
                    for row in current["notes"]
                    if str(row["id"]) == str(identifier)
                ),
                None,
            )
        if request.method == "DELETE":
            changed = await asyncio.to_thread(
                store.remove_record, profile, kind, identifier
            )
        else:
            try:
                data = await request.json()
            except (ValueError, UnicodeDecodeError) as exc:
                raise web.HTTPBadRequest(text="Неверные данные записи.") from exc
            if not isinstance(data, dict):
                raise web.HTTPBadRequest(text="Неверные данные записи.")
            text = data.get("text")
            if not isinstance(text, str) or not 0 < len(text.strip()) <= 500:
                raise web.HTTPBadRequest(text="Введи текст до 500 символов.")
            text = text.strip()
            if kind == "notes":
                changed = await asyncio.to_thread(
                    store.edit_note, profile, identifier, text
                )
            elif kind == "preferences":
                entity = data.get("entity")
                if not isinstance(entity, str) or not ENTITY_ID.fullmatch(entity):
                    raise web.HTTPBadRequest(text="Выбери сущность Home Assistant.")
                text = " ".join(
                    re.findall(r"[a-zа-я0-9]+", text.lower().replace("ё", "е"))
                )[:160]
                if not text:
                    raise web.HTTPBadRequest(text="Введи фразу для запоминания.")
                changed = await asyncio.to_thread(
                    store.edit_preference, profile, identifier, text, entity
                )
            else:
                due = data.get("due")
                if due is not None:
                    try:
                        due = datetime.fromisoformat(due)
                        if due.tzinfo is None or due <= datetime.now(timezone.utc):
                            raise ValueError
                        due = due.timestamp()
                    except (ValueError, TypeError, OverflowError, OSError) as exc:
                        raise web.HTTPBadRequest(
                            text="Выбери будущую дату и время."
                        ) from exc
                value = data.get("value")
                if value is not None and (
                    not isinstance(value, str) or not 0 < len(value.strip()) <= 80
                ):
                    raise web.HTTPBadRequest(text="Введи значение условия.")
                if value is not None:
                    existing = await asyncio.to_thread(store.dashboard, profile)
                    condition = next(
                        (
                            item["condition"]
                            for item in existing["reminders"]
                            if item["id"] == identifier
                        ),
                        None,
                    )
                    if condition and condition.get("operator") in {"above", "below"}:
                        try:
                            if not -1e20 <= float(value) <= 1e20:
                                raise ValueError
                        except ValueError as exc:
                            raise web.HTTPBadRequest(
                                text="Введи числовой порог."
                            ) from exc
                changed = await asyncio.to_thread(
                    store.edit_reminder,
                    profile,
                    identifier,
                    text,
                    due,
                    value.strip() if value is not None else None,
                )
        if not changed:
            raise web.HTTPConflict(
                text="Запись уже изменилась, выполнена или совпадает с другой."
            )
        if kind == "notes" and knowledge:
            if request.method == "DELETE" and old_note:
                await asyncio.to_thread(knowledge.forget, profile, old_note)
            elif request.method != "DELETE":
                await asyncio.to_thread(
                    knowledge.rename_memory, profile, old_note, text
                )
        return web.json_response({"updated": True})

    async def knowledge(self, request):
        store = self.service.knowledge
        if request.method == "GET":
            return web.json_response(
                {"entities": await asyncio.to_thread(store.entities)}
            )
        entity = request.match_info["entity"]
        if not ENTITY_ID.fullmatch(entity):
            raise web.HTTPBadRequest(text="Выберите сущность Home Assistant.")
        if request.method == "DELETE":
            await asyncio.to_thread(store.remove_entity, entity)
            return web.json_response({"updated": True})
        try:
            data = await request.json()
            if not isinstance(data, dict) or data.keys() - {
                "aliases",
                "room",
                "purpose",
            }:
                raise ValueError("Неверные поля карты дома.")
            # Settings map is administrator-maintained and cannot create a phantom device.
            await self.service.brain._ha("GET", "/api/states/" + entity)
            value = await asyncio.to_thread(
                store.set_entity,
                entity,
                data.get("aliases", []),
                data.get("room", ""),
                data.get("purpose", ""),
            )
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        except (ClientError, TimeoutError, RuntimeError) as exc:
            raise web.HTTPBadGateway(
                text="Не удалось проверить устройство в Home Assistant."
            ) from exc
        return web.json_response({"entity": entity, **value})

    async def entities(self, _request):
        brain = self.service.brain
        try:
            rows = await brain._ha("GET", "/api/states")
            ids = {
                row["entity_id"]
                for row in rows
                if ENTITY_ID.fullmatch(row["entity_id"])
            }
            metadata = await brain._registry_metadata(ids)
        except (RuntimeError, ValueError, TimeoutError, ClientError) as exc:
            raise web.HTTPBadGateway(text="Каталог Home Assistant недоступен.") from exc
        return web.json_response(
            {
                "entities": [
                    {
                        "id": entity,
                        "name": row.get("attributes", {}).get("friendly_name")
                        or entity,
                        "area": metadata.get(entity, {}).get("area"),
                    }
                    for row in rows
                    if (entity := row["entity_id"]) in ids
                ],
                "control_domains": sorted(SAFE_SERVICES),
            }
        )

    async def history(self, request):
        kind = request.query.get("type")
        if kind not in (None, "call", "sms"):
            raise web.HTTPBadRequest(text="Неизвестный тип записи.")
        try:
            offset = int(request.query.get("offset", 0))
            if not 0 <= offset <= 100_000:
                raise ValueError
        except ValueError as exc:
            raise web.HTTPBadRequest(text="Неверная страница журнала.") from exc
        result = await self.service.history.page(kind, offset)
        return web.json_response({**result, "sms": self.service.sms_status})

    async def clear_history(self, request):
        kind = request.match_info["kind"]
        try:
            if kind == "speech_cache":
                cleared = await asyncio.to_thread(self.service.speech_cache.clear)
            elif kind in {"call", "sms"}:
                cleared = await self.service.history.clear(kind)
            else:
                raise web.HTTPNotFound()
        except OSError as exc:
            raise web.HTTPServiceUnavailable(
                text="Не удалось очистить локальные данные."
            ) from exc
        return web.json_response({"cleared": cleared})

    async def audit(self, request):
        try:
            limit = int(request.query.get("limit", 100))
        except ValueError as exc:
            raise web.HTTPBadRequest(text="Неверный размер журнала.") from exc
        store = self.service.personal
        timezone_ = self.service.brain.timezone
        today = datetime.now(timezone_).date().isoformat()
        log, usage, used = await asyncio.gather(
            asyncio.to_thread(store.audit_log, max(1, min(limit, 500))),
            asyncio.to_thread(store.usage),
            asyncio.to_thread(store.tokens_today, today),
        )
        names = self.personal_profiles()
        names.setdefault("room", "Комната")
        for row in (*log, *usage):
            row["profile_name"] = names.get(row["profile"], row["profile"])
        return web.json_response(
            {
                "log": log,
                "usage": usage,
                "today": today,
                "tokens_today": used,
                "daily_limit": int(self.service.o.get("llm_daily_token_limit") or 0),
            }
        )

    async def refresh_sms(self, _request):
        if self.service.modem.call_state != "idle":
            raise web.HTTPConflict(text="SMS обновятся, когда модем освободится.")
        self.service.sms_wake.set()
        return web.json_response({"queued": True}, status=202)
