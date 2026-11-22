"""Scheduled actions and Home Assistant event delivery."""

import asyncio
import logging
import os
import socket
from datetime import datetime, timezone

from aiohttp import ClientError, ClientTimeout, web
from personal import condition_matches

LOG = logging.getLogger(__name__)


class AutomationService:
    def fire_ha_event(self, name, data, *, delivery=None):
        """Send an event to the Home Assistant bus without blocking the caller."""
        direct = (
            not self.closing
            and self.o.get("ha_events", True) is not False
            and getattr(self.brain, "hass_token", "")
        )
        if not direct and delivery is None:
            return

        async def send():
            if not direct:
                await self._set_call_delivery(delivery, "companion")
                return
            try:
                payload = data
                if delivery is not None and self.call_outbox is not None:
                    payload = {
                        **data,
                        "event_id": f"{self.call_event_stream}:{delivery['sequence']}",
                    }
                await self.brain._ha("POST", "/api/events/" + name, payload, timeout=5)
            except Exception as exc:
                if delivery is not None:
                    await self._set_call_delivery(delivery, "companion")
                LOG.warning("Could not send %s to Home Assistant: %s", name, exc)
            else:
                if delivery is not None:
                    await self._set_call_delivery(delivery, "direct")

        task = asyncio.create_task(send())
        self.ha_event_tasks.add(task)
        task.add_done_callback(self._event_delivery_done)

    def _event_delivery_done(self, task):
        self.ha_event_tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            LOG.error("Could not persist Home Assistant event delivery", exc_info=error)

    async def deliver_reminders(self):
        clock = asyncio.get_running_loop().time
        next_sample = 0
        while True:
            if clock() >= next_sample:
                next_sample = clock() + 15
                try:
                    await self.sample_reminder_conditions()
                    await self.run_due_actions()
                    await self.escalate_undelivered_events()
                except Exception as exc:
                    LOG.warning(
                        "Reminder condition check failed: %s", type(exc).__name__
                    )
            if self.modem.call_state == "idle":
                try:
                    now = datetime.now(timezone.utc)
                    due = await asyncio.to_thread(
                        self.personal.due, now, self.reminder_numbers
                    )
                    if due:
                        identifier, profile, text = due[0]
                        context = await asyncio.to_thread(
                            self.personal.reminder_context, profile, identifier
                        )
                        decision = await asyncio.to_thread(
                            self.policies.decide,
                            profile,
                            now,
                            context.get("notification_urgent", False),
                        )
                        if not decision["allowed"]:
                            await asyncio.to_thread(
                                self.personal.retry_after,
                                identifier,
                                datetime.fromisoformat(decision["defer_until"]),
                            )
                            continue
                        if not await asyncio.to_thread(
                            self.personal.reserve_delivery, identifier, profile, now
                        ):
                            continue
                        if decision["channel"] == "telegram":
                            try:
                                await self.send_telegram_notice(
                                    decision["telegram_target"], "Напоминание. " + text
                                )
                                await asyncio.to_thread(
                                    self.personal.delivered, identifier
                                )
                            except (RuntimeError, ClientError, TimeoutError):
                                LOG.warning("Telegram reminder delivery not confirmed")
                            continue
                        self.reminder_call = (
                            self.modem.call_id + 1,
                            identifier,
                            self.reminder_numbers[profile],
                        )
                        try:
                            await self.dial_number(
                                self.reminder_numbers[profile],
                                "Напоминание. " + text,
                                context.get("camera"),
                            )
                        except (web.HTTPException, RuntimeError, OSError):
                            self.reminder_call = None
                            LOG.info("Reminder call deferred")
                except Exception as exc:
                    LOG.warning("Reminder dispatch failed: %s", type(exc).__name__)
            await asyncio.sleep(5)

    async def run_due_actions(self):
        profiles = {member["key"]: member for member in self.family.values()}
        personal = self.personal

        async def status(identifier, value):
            await asyncio.to_thread(personal.action_status, identifier, value)

        due = await asyncio.to_thread(personal.due_actions, datetime.now(timezone.utc))
        for identifier, profile, condition in due:
            if not await asyncio.to_thread(personal.claim_action, identifier):
                continue
            await status(identifier, "started")
            if profile not in self.reminder_numbers:
                await status(identifier, "profile_removed")
                continue
            brain = self.caller_brain(profiles.get(profile))
            condition_allowed = (
                condition["kind"] == "time"
                or (
                    condition["kind"] == "zone"
                    and condition["entity"] == brain.options.get("reminder_person")
                )
                or (
                    condition["kind"] == "state" and brain._visible(condition["entity"])
                )
            )
            if not condition_allowed:
                await status(identifier, "blocked")
                continue
            try:
                async with brain.action_lock:
                    result = await brain._tool(
                        "call_ha_service", condition["action"], preconfirmed=True
                    )
            except (ClientError, TimeoutError, RuntimeError, ValueError):
                await status(identifier, "unconfirmed")
                continue
            except Exception:
                LOG.exception("Scheduled action failed")
                await status(identifier, "failed")
                continue
            accepted = isinstance(result, dict) and result.get("accepted")
            await status(identifier, "accepted" if accepted else "blocked")

    async def sample_reminder_conditions(self):
        watches = await asyncio.to_thread(self.personal.watches)
        if not watches:
            return
        profiles = {member["key"]: member for member in self.family.values()}
        rows = await self.brain._ha("GET", "/api/states")
        states = {row["entity_id"]: row for row in rows}
        now = datetime.now(timezone.utc)
        for identifier, profile, condition, armed in watches:
            if profile not in self.reminder_numbers:
                continue
            brain = self.caller_brain(profiles.get(profile))
            if condition["kind"] == "zone":
                permitted = condition["entity"] == brain.options.get("reminder_person")
            else:
                permitted = brain._visible(condition["entity"])
            if permitted:
                matched = condition_matches(condition, states)
                await asyncio.to_thread(
                    self.personal.sample, identifier, matched, armed, now
                )

    async def watch_home_events(self):
        previous = {}
        while True:
            try:
                rows = await self.brain._ha("GET", "/api/states")
                for row in rows:
                    entity, value = row.get("entity_id"), row.get("state")
                    if entity in previous and previous[entity] != value:
                        await asyncio.to_thread(
                            self.events_journal.record,
                            entity,
                            str(value),
                            "home_assistant",
                            previous=previous[entity],
                            attributes=row.get("attributes", {}),
                        )
                    previous[entity] = value
            except Exception:
                LOG.warning("Home event observation temporarily unavailable")
            await asyncio.sleep(15)

    async def escalate_undelivered_events(self):
        rows = await asyncio.to_thread(self.personal.claim_failed_events)
        for identifier, profile, text, context in rows:
            decision = await asyncio.to_thread(
                self.escalation.advance,
                context.get("event_id", identifier),
                context.get("source", ""),
                profile,
                not context.get("notification_shared", False),
                context.get("escalation_attempt", 0),
            )
            target = decision.get("recipient")
            if not decision.get("escalate") or target not in self.reminder_numbers:
                continue
            member = next(
                (row for row in self.family.values() if row["key"] == target), None
            )
            camera = context.get("camera")
            if camera and not self.caller_brain(member).camera_allowed(camera):
                context["camera"] = None
            context.pop("escalation_checked", None)
            context["escalation_attempt"] = decision["attempt"]
            await asyncio.to_thread(
                self.personal.reminder,
                target,
                self.contacts.get(target, ""),
                text,
                datetime.now(timezone.utc),
                context,
            )
            await self.record_trace(
                target,
                "event:" + context.get("event_id", identifier),
                "escalation",
                "queued",
                {"source": context.get("source")},
            )

    async def send_telegram_notice(self, target, text):
        payload = {"target": [target], "message": text}
        bot = self.o.get("telegram_photo_target", "")
        if bot:
            payload["entity_id"] = bot
        result = await self.brain._ha(
            "POST",
            "/api/services/telegram_bot/send_message?return_response",
            payload,
            timeout=20,
        )
        chats = result.get("service_response", {}).get("chats", [])
        if not any(chat.get("message_id") for chat in chats):
            raise RuntimeError("Telegram did not confirm delivery")

    async def discover_companion(self):
        supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
        if not supervisor_token:
            LOG.warning("Supervisor token unavailable; manual companion setup required")
            return
        payload = {
            "service": "jarvis_q1",
            "config": {
                "host": socket.gethostname(),
                "port": 8099,
                "token": self.api_token,
            },
        }
        try:
            async with self.http.post(
                "http://supervisor/discovery",
                json=payload,
                headers={"Authorization": "Bearer " + supervisor_token},
                timeout=ClientTimeout(total=8),
            ) as resp:
                if resp.status not in (200, 201):
                    LOG.warning(
                        "Supervisor discovery HTTP %s: %s",
                        resp.status,
                        (await resp.text())[:100],
                    )
                else:
                    LOG.info("JARVIS Q1 companion discovery sent")
        except Exception as exc:
            LOG.warning("Supervisor discovery unavailable: %s", exc)
