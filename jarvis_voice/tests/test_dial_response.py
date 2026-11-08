"""An accepted modem dial must return success to both HTTP entry points."""

import json
from datetime import datetime, timedelta, timezone
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
with patch.dict(sys.modules, {"speech": SimpleNamespace(SpeechKit=object)}):
    import main as server_module
    from main import Server
from personal import Personal


class DialResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_dial_returns_success_and_keeps_prompt_for_current_call(self):
        service = Server({"api_token": "test", "allowed_number": "+79991234567"})

        async def dial(_number):
            service.modem.call_id += 1
            service.modem.call_state = "dialing"

        service.modem.call_state = "idle"
        service.modem.dial = AsyncMock(side_effect=dial)
        response = await service.dial_number(prompt="test prompt")
        self.assertEqual(json.loads(response.text), {"ok": True, "state": "dialing"})
        self.assertEqual(service.next_prompt, (1, "test prompt", None))

    async def test_dial_reports_modem_rejection_and_clears_prompt(self):
        service = Server({"api_token": "test", "allowed_number": "+79991234567"})
        service.modem.call_state = "idle"
        service.modem.dial = AsyncMock(side_effect=RuntimeError("Dial failed: BUSY"))
        with self.assertRaisesRegex(web.HTTPConflict, "Conflict") as error:
            await service.dial_number()
        self.assertEqual(error.exception.text, "Dial failed: BUSY")
        self.assertIsNone(service.next_prompt)

    async def test_named_family_recipient_is_resolved_without_using_owner(self):
        service = Server({"api_token": "test", "allowed_number": "+12025550101",
                          "family_members": ["Test family | +12025550102"]})
        service.modem.call_state = "idle"
        service.modem.dial = AsyncMock()
        await service.dial_number(member="test FAMILY")
        service.modem.dial.assert_awaited_once_with("+12025550102")

    def gate_service(self, pin, store=None):
        service = Server({"api_token": "test", "gate_pin": pin,
                          "family_members": ["Member | +12025550102 | | cover.test"]})
        service.personal = store or Personal(pathlib.Path(self.tmp.name) / "personal.sqlite3")
        service.modem.active_number = "+12025550102"
        service.modem.call_state = "connected"
        service.start_call_audio = AsyncMock()
        service.audio.stop = AsyncMock()
        service.brain = SimpleNamespace(_ha=AsyncMock(
            return_value={"state": "closed", "attributes": {"friendly_name": "Test lock"}}))

        async def choose_gate(_phrase, *, phone_actions, **kwargs):
            result = await phone_actions["open_assigned_gate"]({})
            return result["instruction"] if isinstance(result, dict) else result
        service.caller_brain = lambda member: SimpleNamespace(reply=choose_gate)
        return service

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def posted(service):
        return [c for c in service.brain._ha.await_args_list if c.args[0] == "POST"]

    async def test_gate_opens_without_optional_pin(self):
        service = self.gate_service("")
        async def dialog(_speech, reply, *_args, on_key, **_):
            text = await reply("Открой калитку", on_progress=lambda text: None)
            self.assertIn("отправлена", text)
            self.assertEqual(await on_key("#"), "")
        with patch.object(server_module, "run_dialog", dialog):
            await service._dialog("", None)
        self.assertEqual(len(self.posted(service)), 1)

    async def test_pin_has_no_four_to_twelve_digit_restriction(self):
        for pin in ("7", "001234567890123456789"):
            with self.subTest(pin_length=len(pin)):
                service = self.gate_service(pin)
                async def dialog(_speech, reply, *_args, on_key, **_):
                    text = await reply("Открой калитку", on_progress=lambda text: None)
                    self.assertIn("решётку", text)
                    self.assertEqual(self.posted(service), [])
                    for digit in pin + "#":
                        result = await on_key(digit)
                    self.assertIn("отправлена", result)
                with patch.object(server_module, "run_dialog", dialog):
                    await service._dialog("", None)
                self.assertEqual(len(self.posted(service)), 1)

    async def test_call_reminder_decline_and_defer_are_scoped_and_not_marked_spoken(self):
        for action in ("decline", "defer"):
            with self.subTest(action=action):
                service = self.gate_service("")
                profile = next(iter(service.family.values()))["key"]
                due = datetime.now(timezone.utc) + timedelta(hours=1)
                identifier = service.personal.reminder(profile, "Member", "private", due)
                service._say = AsyncMock()
                async def decide(_phrase, *, phone_actions, **_):
                    return await phone_actions["handle_call_reminder"]({"action": action, "when": due.isoformat()})
                service.caller_brain = lambda member: SimpleNamespace(reply=decide, future_time=lambda when: due)
                async def dialog(_speech, reply, _source, play, *_args, **_):
                    result = await reply("Потом" if action == "defer" else "Не надо", on_progress=lambda text: None)
                    self.assertTrue(result["ok"])
                    await play("private")
                with patch.object(server_module, "run_dialog", dialog):
                    await service._dialog("private", None, identifier)
                rows = service.personal.dashboard(profile)["reminders"]
                row = next(row for row in rows if row["id"] == identifier)
                self.assertEqual(row["status"], "declined" if action == "decline" else "pending")
                self.assertEqual(row["completed"], action == "decline")

    async def test_gate_opens_with_keypad_pin(self):
        service = self.gate_service("00123456")

        async def dialog(_speech, reply, _source, _play, _hangup, _prompt, *, keypad, on_key, **_):
            await on_key("#")
            service.brain._ha.assert_not_awaited()
            text = await reply("Открой калитку", on_progress=lambda text: None)
            self.assertIn("решётку", text)
            self.assertEqual(self.posted(service), [])
            for key in "3*00123456#":
                await service.event("dtmf_received", {"digit": key, "call_id": service.modem.call_id})
                result = await on_key(keypad.get_nowait())
            self.assertIn("отправлена", result)
            self.assertEqual(await on_key("#"), "")
        with patch.object(server_module, "run_dialog", dialog):
            await service._dialog("", None)
        service.brain._ha.assert_any_await("POST", "/api/services/cover/open_cover", {"entity_id": "cover.test"})
        self.assertEqual(len(self.posted(service)), 1)
        self.assertEqual(service.events, [])
        self.assertIsNone(service.keypad)

    async def test_spoken_pin_is_not_accepted(self):
        service = self.gate_service("1234")

        async def dialog(_speech, reply, *_args, **_):
            await reply("Открой калитку", on_progress=lambda text: None)
            text = await reply("1 2 3 4", on_progress=lambda text: None)
            self.assertIn("клавиатуре", text)
        with patch.object(server_module, "run_dialog", dialog):
            await service._dialog("", None)
        self.assertEqual(self.posted(service), [])

    async def test_pin_lockout_survives_across_calls(self):
        store = Personal(pathlib.Path(self.tmp.name) / "personal.sqlite3")
        for _call in range(3):
            service = self.gate_service("1234", store)

            async def dialog(_speech, reply, *_args, on_key, **_):
                await reply("Открой калитку", on_progress=lambda text: None)
                for key in "9999#":
                    result = await on_key(key)
                self.assertIn("не подошёл", result)
            with patch.object(server_module, "run_dialog", dialog):
                await service._dialog("", None)
        service = self.gate_service("1234", store)

        async def locked(_speech, reply, *_args, on_key, **_):
            text = await reply("Открой калитку", on_progress=lambda text: None)
            self.assertIn("заблокировано", text)
        with patch.object(server_module, "run_dialog", locked):
            await service._dialog("", None)
        self.assertEqual(self.posted(service), [])

    async def test_multiple_locks_require_selection_and_reject_unassigned_entity(self):
        service = Server({"api_token": "test", "gate_pin": "1234", "owner_locks": ["lock.first", "switch.second"]})
        service.personal = Personal(pathlib.Path(self.tmp.name) / "personal.sqlite3")
        service.modem.call_state = "connected"
        service.start_call_audio = AsyncMock()
        service.audio.stop = AsyncMock()
        service.brain = SimpleNamespace(_ha=AsyncMock(return_value={"state": "locked", "attributes": {}}))

        async def choose(_phrase, *, phone_actions, **kwargs):
            missing = await phone_actions["open_assigned_gate"]({})
            self.assertEqual(len(missing["locks"]), 2)
            await phone_actions["open_assigned_gate"]({"entity_id": "lock.foreign"})
            self.assertEqual(self.posted(service), [])
            result = await phone_actions["open_assigned_gate"]({"entity_id": "switch.second"})
            self.assertIn("решётку", result["instruction"])
            return result
        service.caller_brain = lambda member: SimpleNamespace(reply=choose)

        async def dialog(_speech, reply, *args, on_key, **kwargs):
            await reply("Открой второй замок", on_progress=lambda text: None)
            for key in "1234#":
                result = await on_key(key)
            self.assertIn("отправлена", result)
        with patch.object(server_module, "run_dialog", dialog):
            await service._dialog("", None)
        service.brain._ha.assert_any_await("POST", "/api/services/switch/turn_on", {"entity_id": "switch.second"})
        self.assertEqual(len(self.posted(service)), 1)

    async def test_national_number_format_matches_owner(self):
        service = Server({"api_token": "test", "allowed_number": "+79991234567"})
        service.modem.active_number = "89991234567"
        self.assertTrue(service.caller_allowed())
        service.modem.active_number = "+79990000000"
        self.assertFalse(service.caller_allowed())

    async def test_failed_session_restarts_only_while_same_call_connected(self):
        service = Server({"api_token": "test"})
        service.modem.call_state = "connected"
        service.modem.call_id = 12
        service._dialog = AsyncMock(side_effect=[False, None])
        with patch.object(server_module.asyncio, "sleep", AsyncMock()):
            await service._start_session(12, "hello", None)
        self.assertEqual(service._dialog.await_count, 2)
        service._dialog.reset_mock()
        service.modem.call_id = 13
        await service._start_session(12, "hello", None)
        service._dialog.assert_not_awaited()

    async def test_failed_greeting_retries_in_the_same_call(self):
        for error, reset in ((RuntimeError("speech unavailable"), False),
                             (server_module.PCMPlaybackError("USB write failed"), True)):
            with self.subTest(reset=reset):
                service = Server({"api_token": "test"})
                service.modem.call_state = "connected"
                service.modem.call_id = 12

                async def synthesize(_text):
                    yield b"pcm"

                service.speech = SimpleNamespace(synthesize=synthesize)
                service.audio.write_stream = AsyncMock(side_effect=[ExceptionGroup("audio", [error]), None])
                service.audio.stop = AsyncMock()
                service.start_call_audio = AsyncMock()
                await service._say("Привет.")
                self.assertEqual(service.audio.write_stream.await_count, 2)
                if reset:
                    service.audio.stop.assert_awaited_once()
                    service.start_call_audio.assert_awaited_once_with(12)
                else:
                    service.audio.stop.assert_not_awaited()
                    service.start_call_audio.assert_not_awaited()

    async def test_call_restarts_pcm_if_first_route_has_no_frames(self):
        service = Server({"api_token": "test"})
        service.modem.call_state = "connected"
        service.modem.call_id = 12
        service.modem.reset_pcm = AsyncMock()
        service.modem.enable_pcm = AsyncMock()
        service.audio.start = AsyncMock()
        service.audio.stop = AsyncMock()
        service.audio.wait_ready = AsyncMock(side_effect=[TimeoutError(), None])
        await service.start_call_audio(12)
        self.assertEqual(service.modem.reset_pcm.await_count, 2)
        self.assertEqual(service.modem.enable_pcm.await_count, 2)
        service.audio.stop.assert_awaited_once()
