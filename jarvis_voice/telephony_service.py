"""SIM7600 calls, audio sessions and call history."""

import asyncio
import hmac
import logging
import re
from contextlib import aclosing
from datetime import datetime, timezone

from aiohttp import ClientError, web
from audio import PCMPlaybackError
from brain import Brain
from family import GATE_ACTIONS
from request_helpers import phone_key
from voice_dialog import CallOutput, run_dialog

LOG = logging.getLogger(__name__)


class TelephonyService:
    async def write_history(self):
        while True:
            name, data = await self.history_queue.get()
            try:
                await self.history.call(name, data)
            except Exception:
                LOG.exception("Could not save call history")
            finally:
                self.history_queue.task_done()

    def pcm_lost(self):
        """USB PCM vanished mid-call: stop the dialog and release the call now."""
        if self.closing or (self.pcm_lost_task and not self.pcm_lost_task.done()):
            return
        self.pcm_lost_task = asyncio.create_task(self._end_call_without_audio())

    async def _end_call_without_audio(self):
        session = self.session
        if session and not session.done():
            session.cancel()
            await asyncio.gather(session, return_exceptions=True)
        if self.modem.call_state not in ("idle", "offline"):
            try:
                await asyncio.wait_for(self.modem.hangup(), 5)
            except Exception as exc:
                LOG.warning("Hangup after PCM loss failed: %s", exc)

    def known_number(self, number):
        key = phone_key(number, self.country)
        if not key:
            return False
        owner = phone_key(self.o.get("allowed_number"), self.country)
        return key in self.family or (bool(owner) and key == owner)

    async def _set_call_delivery(self, event, state):
        event["delivery"] = state
        if self.call_outbox is not None:
            snapshot = {**event, "data": dict(event["data"])}
            await asyncio.to_thread(self.call_outbox.update, snapshot)

    async def _record_call_event(self, name, data, *, delivery="pending"):
        self.call_event_sequence += 1
        event = {
            "sequence": self.call_event_sequence,
            "name": name,
            "data": data,
            "delivery": delivery,
        }
        if self.call_outbox is not None:
            event["sequence"] = await asyncio.to_thread(self.call_outbox.record, event)
        self.call_events.append(event)
        return event

    def _publish_incoming_call(self):
        if self.incoming_call_timer is None:
            return
        self.incoming_call_timer.cancel()
        self.incoming_call_timer = None
        event = self.incoming_call_event
        self.fire_ha_event(event["name"], dict(event["data"]), delivery=event)

    async def _telephony_event(self, name, data):
        if name in ("incoming_call", "outgoing_call", "caller_id", "call_connected"):
            number = data.get("number") or self.modem.active_number
            if number:
                self.event_number = number
        if name == "incoming_call":
            self.event_number = data.get("number") or ""
            self.incoming_call_event = await self._record_call_event(
                "jarvis_call_incoming",
                {
                    "number": self.event_number,
                    "known": self.known_number(self.event_number),
                    "call_id": self.modem.call_id,
                },
                delivery="pending",
            )
            self.incoming_call_timer = asyncio.get_running_loop().call_later(
                2,
                self._publish_incoming_call,
            )
        if name == "caller_id":
            number = getattr(self, "event_number", "")
            event = self.incoming_call_event
            if event is not None and not event["data"]["number"]:
                event["data"].update(number=number, known=self.known_number(number))
                if (
                    self.incoming_call_timer is not None
                    and self.call_outbox is not None
                ):
                    await self._set_call_delivery(event, "pending")
                self._publish_incoming_call()
        elif name in ("call_ended", "call_failed", "modem_offline"):
            if name == "modem_offline" and not (
                self.incoming_call_event or getattr(self, "event_number", "")
            ):
                return
            self._publish_incoming_call()
            event = await self._record_call_event(
                "jarvis_call_ended",
                {
                    "number": getattr(self, "event_number", ""),
                    "reason": data.get("reason") or name,
                    "call_id": data.get("call_id", self.modem.call_id),
                },
            )
            self.fire_ha_event(event["name"], dict(event["data"]), delivery=event)
            self.event_number = ""
            self.incoming_call_event = None
        elif name == "dtmf_received" and not (
            self.keypad and self.keypad[0] == data.get("call_id")
        ):
            # Keys typed into an active JARVIS dialog may be the gate PIN:
            # never copy them to the Home Assistant event bus and logbook.
            self.fire_ha_event(
                "jarvis_dtmf",
                {
                    "digit": data.get("digit"),
                    "call_id": data.get("call_id"),
                },
            )

    async def event(self, name, data):
        async with self.call_event_lock:
            await self._telephony_event(name, data)
        if name == "dtmf_received":
            keypad = self.keypad
            if keypad and keypad[0] == data["call_id"]:
                keys = keypad[1]
                if keys.full():
                    while not keys.empty():
                        keys.get_nowait()
                    keys.put_nowait(None)
                else:
                    keys.put_nowait(data["digit"])
            return
        if name == "sms_received":
            self.sms_wake.set()
        self.events.append({"name": name, "data": data})
        self.events = self.events[-25:]
        LOG.info("JARVIS event: %s", name)
        if name == "modem_connected":
            self.mode = "ready"
        if (
            name == "caller_id"
            and not self.closing
            and self.o.get("auto_answer")
            and self.caller_allowed()
            and (not self.auto_answer_task or self.auto_answer_task.done())
        ):
            self.auto_answer_task = asyncio.create_task(
                self._maybe_answer(self.modem.call_id)
            )
        if (
            name == "call_connected"
            and not self.closing
            and self.speech
            and self.caller_allowed()
        ):
            call_id = self.modem.call_id
            await self.trace_current_call("connection", "connected")
            if self.closing:
                return
            prompt, camera = "", None
            if self.next_prompt and self.next_prompt[0] == call_id:
                _, prompt, camera = self.next_prompt
            self.next_prompt = None
            reminder_id = None
            if self.reminder_call and self.reminder_call[0] == call_id:
                _, identifier, number = self.reminder_call
                if phone_key(number, self.country) == self.caller_key():
                    reminder_id = identifier
                else:
                    prompt = ""
            self.session = asyncio.create_task(
                self._start_session(call_id, prompt, camera, reminder_id)
            )
        if name in ("call_ended", "modem_offline"):
            self.next_prompt = None
            self.reminder_call = None
            if (
                self.session
                and self.session is not asyncio.current_task()
                and not self.session.cancelling()
            ):
                self.session.cancel()
            self.mode = "ready" if name == "call_ended" else "offline"
        if name in {
            "incoming_call",
            "outgoing_call",
            "caller_id",
            "call_connected",
            "call_ended",
            "call_failed",
            "modem_offline",
        }:
            self.history_queue.put_nowait((name, dict(data)))

    async def import_sms(self, lines):
        completed = await self.history.import_sms(lines) or []
        for message in completed:
            self.fire_ha_event(
                "jarvis_sms_received",
                {
                    "number": message["number"],
                    "text": message["text"],
                    "received_at": message["received_at"],
                    "parts": message["parts"],
                    "known": self.known_number(message["number"]),
                },
            )
        return completed

    async def sync_sms(self):
        while True:
            self.sms_wake.clear()
            if self.modem.call_state == "idle":
                try:
                    usage = await self.modem.read_sms(self.import_sms)
                    self.sms_status = {"storage": usage}
                except (RuntimeError, OSError) as exc:
                    if self.modem.call_state == "idle":
                        self.sms_status = {"error": str(exc)}
                except Exception:
                    LOG.exception("Could not synchronize the SMS archive")
                    self.sms_status = {"error": "Не удалось сохранить SMS."}
            try:
                await asyncio.wait_for(self.sms_wake.wait(), timeout=15)
            except TimeoutError:
                pass

    async def _maybe_answer(self, call_id):
        if (
            self.modem.call_id != call_id
            or self.modem.call_state != "ringing"
            or not self.caller_allowed()
        ):
            return
        try:
            await self.modem.answer()
        except (RuntimeError, ConnectionError, OSError):
            LOG.exception("Automatic answer failed")

    def caller_key(self):
        return phone_key(self.modem.active_number, self.country)

    def caller_allowed(self):
        caller = self.caller_key()
        owner = phone_key(self.o.get("allowed_number"), self.country)
        return bool(caller and (caller == owner or caller in self.family))

    def caller_brain(self, member):
        if not member:
            return self.brain
        key = member["key"]
        if key not in self.family_brains:
            entities = member["entities"]
            access = member["access"]
            options = {
                **self.o,
                "camera_entity": "",
                "reminder_calendar": member["calendar"],
                "reminder_person": member["person"],
            }
            if access != "owner":
                options.update(
                    allowed_entities=entities,
                    observed_entities=entities,
                    observe_all_entities=access == "read_all",
                    control_all_entities=False,
                )
            self.family_brains[key] = Brain(
                options,
                self.http,
                None,
                share_photo=self.share_photo,
                knowledge=self.knowledge,
                policies=self.policies,
                timeline=self.timeline,
                autonomy=self.autonomy,
                test_rules=self.test_rules,
                inbox=self.inbox,
                events=self.events_journal,
                contacts=self.contacts,
                personal=self.personal,
                profile=key,
                profile_name=member["name"],
                timezone_name=self.brain.timezone.key,
                restricted_entities=self.gates,
                registry_cache=self.brain.registry_cache,
                caller_number=member["number"],
                owner=False,
            )
        return self.family_brains[key]

    async def _start_session(self, call_id, prompt, camera, reminder_id=None):
        async with self.session_lock:
            for attempt in range(2):
                if (self.modem.call_id, self.modem.call_state) != (
                    call_id,
                    "connected",
                ):
                    return
                failed = await self._dialog(prompt, camera, reminder_id)
                if failed is not False:
                    return
                LOG.warning(
                    "Voice session recovery call=%s attempt=%s", call_id, attempt + 1
                )
                await asyncio.sleep(1)
            LOG.error("Voice session recovery exhausted call=%s", call_id)

    async def start_call_audio(self, call_id):
        for attempt in range(3):
            if (self.modem.call_id, self.modem.call_state) != (call_id, "connected"):
                raise RuntimeError("Call already ended")
            try:
                await self.modem.reset_pcm(call_id)
                await self.audio.start()
                await self.modem.enable_pcm()
                await self.audio.wait_ready()
                LOG.info("Call USB PCM verified in both directions")
                await self.trace_current_call("audio_rx", "ready")
                return
            except (RuntimeError, OSError, TimeoutError) as exc:
                LOG.warning(
                    "Call audio setup attempt %d: %s", attempt + 1, type(exc).__name__
                )
                await self.trace_current_call(
                    "audio_rx",
                    "failed",
                    {"attempt": attempt + 1, "error": type(exc).__name__},
                )
                await self.audio.stop()
                if attempt == 2:
                    raise
                await asyncio.sleep(0.25 * (attempt + 1))

    async def _say(self, text):
        if not self.speech:
            raise RuntimeError("Yandex SpeechKit API key is missing")
        call_id = self.modem.call_id
        async with self.speaking:
            for attempt in range(2):
                if (self.modem.call_id, self.modem.call_state) != (
                    call_id,
                    "connected",
                ):
                    return
                try:

                    async def traced_chunks():
                        emitted = False
                        try:
                            async with aclosing(self.speech.synthesize(text)) as source:
                                async for chunk in source:
                                    if chunk and not emitted:
                                        emitted = True
                                        await self.trace_current_call("tts", "ready")
                                    yield chunk
                        except Exception as exc:
                            await self.trace_current_call(
                                "tts", "failed", {"error": type(exc).__name__}
                            )
                            raise

                    async with aclosing(traced_chunks()) as chunks:
                        await self.audio.write_stream(chunks)
                    return
                except Exception as exc:
                    failures = [exc]
                    while any(isinstance(error, ExceptionGroup) for error in failures):
                        failures = [
                            nested
                            for error in failures
                            for nested in (
                                error.exceptions
                                if isinstance(error, ExceptionGroup)
                                else [error]
                            )
                        ]
                    kinds = [
                        str(error)
                        if isinstance(error, PCMPlaybackError)
                        else type(error).__name__
                        for error in failures
                    ]
                    LOG.warning(
                        "Call playback attempt %d: %s", attempt + 1, ", ".join(kinds)
                    )
                    if attempt or (self.modem.call_id, self.modem.call_state) != (
                        call_id,
                        "connected",
                    ):
                        raise
                    await asyncio.sleep(0.2)
                    if any(isinstance(error, PCMPlaybackError) for error in failures):
                        await self.audio.stop()
                        await self.start_call_audio(call_id)

    async def _dialog(self, prompt, camera, reminder_id=None):
        brain = None
        caller = ""
        initial_reminder = reminder_id
        call_id = self.modem.call_id
        try:
            self.mode = "starting_audio"
            await self.start_call_audio(self.modem.call_id)
            self.mode = "talking"
            caller = self.caller_key()
            member = self.family.get(caller)
            brain = self.caller_brain(member)
            reset = getattr(brain, "reset_history", None)
            if (
                callable(reset)
                and getattr(brain, "session_call", None) != self.modem.call_id
            ):
                reset()
                brain.session_call = self.modem.call_id
            pending_pin = 0.0
            pending_family_message = None
            family_messages = (
                await asyncio.to_thread(self.inbox.pending, brain.profile)
                if getattr(brain, "profile", None)
                else []
            )
            digits = ""
            allowed_locks = member["locks"] if member else self.owner_locks
            gate = ""
            gate_name = ""
            gate_confirmed = False
            pin = self.o.get("gate_pin", "")
            clock = asyncio.get_running_loop().time
            call_id = self.modem.call_id
            keypad = asyncio.Queue(maxsize=32)
            self.keypad = (call_id, keypad)

            async def open_gate():
                if (self.modem.call_id, self.modem.call_state) != (
                    call_id,
                    "connected",
                ):
                    return ""
                domain = gate.split(".", 1)[0]
                policy = await asyncio.to_thread(
                    self.autonomy.evaluate,
                    member["key"] if member else self.owner_key or "local",
                    gate,
                    GATE_ACTIONS[domain],
                    {"entity_id": gate},
                )
                if policy["decision"] == "deny" or (
                    policy["decision"] == "confirm" and not gate_confirmed
                ):
                    return "Открытие ограничено вашими правилами самостоятельности."
                try:
                    await self.brain._ha(
                        "POST",
                        f"/api/services/{domain}/{GATE_ACTIONS[domain]}",
                        {"entity_id": gate},
                    )
                except (RuntimeError, TimeoutError, ClientError):
                    return "Home Assistant не подтвердил команду открытия."
                return "Команда открытия отправлена."

            async def check_pin(candidate):
                nonlocal pending_pin, digits
                pending_pin = 0.0
                digits = ""
                now = datetime.now(timezone.utc)
                # Failures persist in /data, so brute force across calls or restarts is locked out.
                if await asyncio.to_thread(self.personal.pin_locked, caller, now):
                    return "Открытие по телефону временно заблокировано."
                if not hmac.compare_digest(candidate.encode(), pin.encode()):
                    await asyncio.to_thread(self.personal.pin_failed, caller, now)
                    LOG.warning("Wrong gate PIN from caller")
                    return "Код не подошёл."
                await asyncio.to_thread(self.personal.pin_succeeded, caller)
                return await open_gate()

            async def press(key):
                nonlocal pending_pin, digits
                if pending_pin <= clock():
                    return ""
                if key is None:
                    pending_pin, digits = 0.0, ""
                    return "Ввод прерван. Попроси открыть замок ещё раз."
                if key == "#":
                    return await check_pin(digits)
                if key == "*":
                    digits = ""
                elif key in "0123456789" and len(digits) <= len(pin):
                    digits += key
                pending_pin = clock() + 30
                return ""

            async def end_call(args):
                goodbye = args.get("goodbye")
                goodbye = (
                    " ".join(goodbye.split())[:160] if isinstance(goodbye, str) else ""
                )
                return CallOutput(goodbye or "До связи.", end_call=True)

            async def list_family_messages(_args):
                return {
                    "messages": await asyncio.to_thread(
                        self.inbox.pending, brain.profile
                    )
                }

            async def read_family_message(args):
                nonlocal pending_family_message
                rows = await asyncio.to_thread(self.inbox.pending, brain.profile)
                message = next(
                    (row for row in rows if row["id"] == args.get("id")), None
                )
                if message is None:
                    return {"error": "Сообщение уже обработано или недоступно."}
                pending_family_message = (message["id"], message["text"])
                return CallOutput(message["text"])

            async def read_reminder(_args):
                if not reminder_id:
                    return "Это напоминание уже озвучено."
                return CallOutput(prompt)

            async def handle_call_reminder(args):
                nonlocal reminder_id
                if not reminder_id:
                    return {"error": "Это напоминание уже обработано."}
                profile = member["key"] if member else self.owner_key
                action = args.get("action")
                if action == "decline":
                    changed = await asyncio.to_thread(
                        self.personal.decline_reminder, profile, reminder_id
                    )
                elif action == "defer":
                    when = args.get("when")
                    if not isinstance(when, str):
                        return {"error": "Уточни новое время напоминания."}
                    try:
                        due = brain.future_time(when)
                    except (ValueError, TypeError, OverflowError):
                        return {"error": "Уточни будущее время напоминания."}
                    changed = await asyncio.to_thread(
                        self.personal.defer_reminder, profile, reminder_id, due
                    )
                else:
                    return {"error": "Выбери decline или defer по смыслу просьбы."}
                if not changed:
                    return {
                        "error": "Напоминание уже обработано или принадлежит другому участнику."
                    }
                reminder_id = None
                return {"ok": True, "action": action}

            async def reply(phrase, *, on_progress):
                await self.record_trace(
                    member["key"] if member else self.owner_key or "local",
                    call_id,
                    "recognized",
                    "received",
                    {
                        "text": "[код не сохраняется]"
                        if pending_pin > clock()
                        else phrase
                    },
                )
                started = clock()

                async def list_locks(_args):
                    devices = []
                    for entity in allowed_locks:
                        try:
                            row = await self.brain._ha("GET", "/api/states/" + entity)
                        except (RuntimeError, TimeoutError, ClientError):
                            continue
                        devices.append(
                            {
                                "entity_id": entity,
                                "name": row.get("attributes", {}).get("friendly_name")
                                or entity,
                                "state": row.get("state"),
                                "service": GATE_ACTIONS[entity.split(".", 1)[0]],
                            }
                        )
                    return {"locks": devices}

                async def request_gate(args):
                    nonlocal pending_pin, digits, gate, gate_name, gate_confirmed
                    selected = args.get("entity_id") or (
                        allowed_locks[0] if len(allowed_locks) == 1 else ""
                    )
                    if not isinstance(selected, str) or selected not in allowed_locks:
                        return {
                            "error": "Выбери назначенный замок по названию из списка.",
                            **await list_locks({}),
                        }
                    row = await self.brain._ha("GET", "/api/states/" + selected)
                    if row.get("state") in {"unavailable", "unknown"}:
                        return "Выбранный замок сейчас недоступен."
                    if pending_pin > clock() and gate != selected:
                        return "Сначала заверши ввод кода для уже выбранного замка."
                    policy = await asyncio.to_thread(
                        self.autonomy.evaluate,
                        member["key"] if member else self.owner_key or "local",
                        selected,
                        GATE_ACTIONS[selected.split(".", 1)[0]],
                        {"entity_id": selected},
                    )
                    if policy["decision"] == "deny":
                        return {"error": policy["reason"]}
                    if policy["decision"] == "confirm":
                        ask = brain._needs_confirmation(
                            ("gate", selected),
                            args,
                            "Подтвердите открытие выбранного замка.",
                            required=True,
                        )
                        if ask:
                            return ask
                        gate_confirmed = True
                    else:
                        gate_confirmed = False
                    gate = selected
                    gate_name = (
                        row.get("attributes", {}).get("friendly_name")
                        or "назначенный замок"
                    )
                    if not pin:
                        on_progress(f"Открываю «{gate_name}».")
                        return await open_gate()
                    if not re.fullmatch(r"[0-9]+", pin):
                        return "Открытие по телефону недоступно: код должен состоять из цифр."
                    if await asyncio.to_thread(
                        self.personal.pin_locked, caller, datetime.now(timezone.utc)
                    ):
                        return "Открытие по телефону временно заблокировано после неверных кодов."
                    digits = ""
                    pending_pin = clock() + 30
                    return {
                        "lock": gate_name,
                        "instruction": "Для открытия нужен код. Попроси набрать его на клавиатуре и нажать решётку.",
                    }

                if pending_pin > clock() and re.fullmatch(
                    r"[0-9][0-9 ,\-]*", phrase.strip().rstrip(".!?")
                ):
                    # Spoken digits would be sent to cloud STT and the LLM: accept the PIN only via DTMF.
                    return "Код нужно набрать на клавиатуре телефона и нажать решётку."
                actions = {
                    "end_phone_call": end_call,
                    "list_family_messages": list_family_messages,
                    "read_family_message": read_family_message,
                }
                if reminder_id:
                    actions["read_call_reminder"] = read_reminder
                    actions["handle_call_reminder"] = handle_call_reminder
                if allowed_locks:
                    actions["list_assigned_locks"] = list_locks
                    actions["open_assigned_gate"] = request_gate
                answer = await brain.reply(
                    phrase,
                    camera_entity=camera,
                    on_progress=on_progress,
                    phone_actions=actions,
                )
                await self.record_trace(
                    member["key"] if member else self.owner_key or "local",
                    call_id,
                    "reply",
                    "ready",
                    {"text": answer.text if isinstance(answer, CallOutput) else answer},
                    (clock() - started) * 1000,
                )
                return answer

            async def play(text):
                nonlocal reminder_id, pending_family_message
                if initial_reminder and text == prompt and not reminder_id:
                    return
                started = clock()
                try:
                    await self._say(text)
                except Exception:
                    await self.record_trace(
                        member["key"] if member else self.owner_key or "local",
                        call_id,
                        "playback",
                        "failed",
                        duration_ms=(clock() - started) * 1000,
                    )
                    raise
                await self.record_trace(
                    member["key"] if member else self.owner_key or "local",
                    call_id,
                    "playback",
                    "sent",
                    duration_ms=(clock() - started) * 1000,
                )
                if (
                    pending_family_message
                    and text == pending_family_message[1]
                    and (self.modem.call_id, self.modem.call_state)
                    == (call_id, "connected")
                ):
                    await asyncio.to_thread(
                        self.inbox.delivered, brain.profile, pending_family_message[0]
                    )
                    pending_family_message = None
                if (
                    reminder_id
                    and text == prompt
                    and (self.modem.call_id, self.modem.call_state)
                    == (call_id, "connected")
                ):
                    self.personal.delivered(reminder_id)
                    reminder_id = None

            await run_dialog(
                self.speech,
                reply,
                self.audio.queue,
                play,
                self.modem.hangup,
                (
                    "У меня для вас напоминание. Можно озвучить?"
                    if reminder_id
                    else prompt
                    or (
                        "Здравствуйте. Это Джарвис. Для вас есть сообщения от семьи. Озвучить?"
                        if family_messages
                        else "Здравствуйте. Это Джарвис. Слушаю вас."
                    )
                ),
                background_tools=True,
                keypad=keypad,
                on_key=press,
                echo_filter=self.o.get("call_echo_filter", False),
                on_diagnostic=lambda stage, status, details, duration: (
                    self.record_trace(
                        member["key"] if member else self.owner_key or "local",
                        call_id,
                        stage,
                        status,
                        details,
                        duration,
                    )
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = str(exc)[:250]
            self.mode = "error"
            LOG.exception("Voice session failed call=%s", self.modem.call_id)
            return False
            # Do not hang up just because the speech service fails: leave the call under user control.
        finally:
            self.keypad = None
            if brain is not None:
                self.schedule_call_summary(brain, caller, call_id)
            await self.audio.stop()

    def schedule_call_summary(self, brain, caller, call_id):
        """After a call, ask the LLM for a short summary and post it to HA in the background."""
        if (
            self.closing
            or not self.o.get("call_summary", False)
            or not callable(getattr(brain, "summarize", None))
        ):
            return
        if not getattr(self.brain, "hass_token", None):
            return

        async def run():
            try:
                summary = await brain.summarize()
            except Exception:
                LOG.exception("Call summary failed")
                return
            if not summary:
                return
            member = self.family.get(caller)
            who = (
                member["name"]
                if member
                else (
                    "Владелец"
                    if caller
                    == phone_key(self.o.get("allowed_number", ""), self.country)
                    else caller or "неизвестный"
                )
            )
            try:
                await self.brain._ha(
                    "POST",
                    "/api/services/persistent_notification/create",
                    {
                        "title": f"JARVIS: звонок ({who})",
                        "message": summary,
                        "notification_id": f"jarvis_call_{call_id}",
                    },
                    timeout=8,
                )
            except (RuntimeError, TimeoutError, ClientError) as exc:
                LOG.warning("Could not post call summary: %s", exc)
            self.fire_ha_event(
                "jarvis_call_summary",
                {"call_id": call_id, "caller": who, "summary": summary},
            )

        tasks = self.summary_tasks
        task = asyncio.create_task(run())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def trace_current_call(self, stage, status, details=None):
        profile = (
            self.family.get(self.caller_key(), {}).get("key")
            or self.owner_key
            or "local"
        )
        await self.record_trace(profile, self.modem.call_id, stage, status, details)

    def speech_fallback_used(self, stage):
        task = asyncio.create_task(self.trace_current_call(stage, "fallback"))
        task.add_done_callback(
            lambda finished: finished.exception() if not finished.cancelled() else None
        )

    async def record_trace(
        self, profile, call_id, stage, status, details=None, duration_ms=None
    ):
        try:
            await asyncio.to_thread(
                self.timeline.record,
                profile,
                str(call_id),
                stage,
                status,
                duration_ms,
                details,
            )
        except Exception:
            LOG.warning("Could not store conversation trace")

    async def dial_number(self, number=None, prompt="", camera=None, member=None):
        owner = self.o.get("allowed_number", "")
        family = getattr(self, "family", {})
        if member:
            if not isinstance(member, str) or number:
                raise web.HTTPBadRequest(text="Select one family recipient")
            wanted = member.strip()
            # Prefer the stable profile key; names are kept as a fallback for older clients.
            profile = next(
                (entry for entry in family.values() if entry.get("key") == wanted), None
            ) or next(
                (
                    entry
                    for entry in family.values()
                    if entry["name"].casefold() == wanted.casefold()
                ),
                None,
            )
            if not profile:
                raise web.HTTPBadRequest(text="Unknown family recipient")
            number = profile["number"]
        number = number or owner
        country = getattr(self, "country", "7")
        if (
            not isinstance(number, str)
            or not number
            or (
                phone_key(number, country) != phone_key(owner, country)
                and phone_key(number, country) not in family
            )
        ):
            raise web.HTTPForbidden(text="Number not allowlisted")
        prompt = str(prompt).strip()
        if len(prompt) > 1200:
            raise web.HTTPBadRequest(text="prompt too long")
        if camera and not self.brain.camera_allowed(camera):
            raise web.HTTPForbidden(text="Camera is not allowlisted")
        if self.modem.call_state != "idle":
            raise web.HTTPConflict(text="Modem is busy or offline")
        pending_prompt = self.next_prompt = (self.modem.call_id + 1, prompt, camera)
        try:
            await self.modem.dial(number)
            return web.json_response({"ok": True, "state": self.modem.call_state})
        except (RuntimeError, ValueError, OSError) as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        finally:
            if self.next_prompt is pending_prompt and (
                self.modem.call_id,
                self.modem.call_state,
            ) != (pending_prompt[0], "dialing"):
                self.next_prompt = None
