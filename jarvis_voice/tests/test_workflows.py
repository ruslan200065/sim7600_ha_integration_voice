"""Workflow cancellation and verification must not promise undo or duplicate writes."""
import asyncio
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workflows import WorkflowManager, expected_outcome, outcome_matches, validate_steps


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_cancel_prevents_dispatch(self):
        with TemporaryDirectory() as directory:
            manager = WorkflowManager(Path(directory) / "data.sqlite3", "owner")
            calls = []
            async def runner(tool, arguments):
                calls.append((tool, arguments))
                return {"accepted": True}
            task = manager.start("Light", [{"tool": "call_ha_service", "arguments": {"entity_id": "light.test", "service": "turn_on"}}], runner)
            self.assertTrue(manager.cancel(task["id"])["cancelled"])
            await asyncio.sleep(0)
            self.assertEqual(calls, [])
            self.assertEqual(manager.get(task["id"])["status"], "cancelled")
            await manager.close()

    async def test_cancel_inflight_warns_and_never_dispatches_following_step(self):
        with TemporaryDirectory() as directory:
            manager = WorkflowManager(Path(directory) / "data.sqlite3", "owner")
            entered = asyncio.Event()
            calls = []
            async def runner(tool, arguments):
                calls.append(arguments["entity_id"])
                entered.set()
                await asyncio.Future()
            steps = [{"tool": "call_ha_service", "arguments": {"entity_id": "light.first"}},
                     {"tool": "call_ha_service", "arguments": {"entity_id": "light.second"}}]
            pending = asyncio.create_task(manager.execute("Lights", steps, runner))
            await entered.wait()
            identifier = manager.list_tasks()[0]["id"]
            result = manager.cancel(identifier)
            self.assertIn("могло выполниться", result["task"]["warning"])
            record = await pending
            self.assertEqual(record["status"], "cancelled")
            self.assertEqual(calls, ["light.first"])
            await manager.close()

    async def test_caller_cancellation_is_not_swallowed(self):
        with TemporaryDirectory() as directory:
            manager = WorkflowManager(Path(directory) / "data.sqlite3", "owner")
            entered = asyncio.Event()
            async def runner(*args):
                entered.set()
                await asyncio.Future()
            pending = asyncio.create_task(manager.execute("Wait", [{"tool": "get_entity_state"}], runner))
            await entered.wait()
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            await manager.close()

    async def test_steps_are_ordered_and_stop_at_failed_or_unconfirmed_action(self):
        with TemporaryDirectory() as directory:
            manager = WorkflowManager(Path(directory) / "data.sqlite3", "owner")
            calls = []
            async def runner(tool, arguments):
                calls.append(arguments["entity_id"])
                return {"accepted": len(calls) != 2}
            steps = [{"tool": "call_ha_service", "arguments": {"entity_id": "light." + str(i)}} for i in range(3)]
            result = await manager.execute("Ordered", steps, runner)
            self.assertEqual(calls, ["light.0", "light.1"])
            self.assertEqual(result["status"], "needs_attention")
            self.assertFalse(manager.cancel(result["id"])["cancelled"])
            self.assertEqual(len(result["results"]), 2)
            await manager.close()

    async def test_scenarios_are_persistent_private_and_revalidated(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "data.sqlite3"
            first = WorkflowManager(path, "first")
            second = WorkflowManager(path, "second")
            steps = [{"tool": "call_ha_service", "arguments": {"entity_id": "light.test", "service": "turn_on"}}]
            first.save("Leaving", steps)
            steps[0]["arguments"]["service"] = "turn_off"
            self.assertEqual(first.scenario("Leaving")[0]["arguments"]["service"], "turn_on")
            self.assertEqual(second.list_scenarios(), [])
            with self.assertRaises(ValueError):
                second.scenario("Leaving")
            self.assertFalse(second.delete("Leaving"))
            self.assertEqual(WorkflowManager(path, "first").list_scenarios()[0]["name"], "Leaving")
            self.assertTrue(first.delete("Leaving"))
            self.assertEqual(first.list_scenarios(), [])
            with self.assertRaises(ValueError):
                validate_steps([{"tool": "open_assigned_gate", "arguments": {}}])
            with self.assertRaises(ValueError):
                validate_steps([{"tool": "call_ha_service", "arguments": "invalid"}])

    async def test_new_task_does_not_wait_for_blocked_task(self):
        with TemporaryDirectory() as directory:
            manager = WorkflowManager(Path(directory) / "data.sqlite3", "owner")
            started = asyncio.Event()
            async def blocked(*args):
                started.set()
                await asyncio.Future()
            first = manager.start("Slow", [{"tool": "get_entity_state"}], blocked)
            await started.wait()
            async def fast(*args):
                return {"state": "on"}
            async with asyncio.timeout(0.5):
                result = await manager.execute("Fast", [{"tool": "get_entity_state"}], fast)
            self.assertEqual(result["status"], "completed")
            manager.cancel(first["id"])
            await manager.close()


class VerificationTests(unittest.TestCase):
    def test_unrelated_attribute_update_does_not_verify_light_on(self):
        expected = expected_outcome("turn_on", {"entity_id": "light.test"})
        self.assertFalse(outcome_matches({"state": "off", "attributes": {"last_updated": "new"}}, expected))
        self.assertTrue(outcome_matches({"state": "on"}, expected))
        self.assertFalse(outcome_matches({"state": "unavailable"}, expected))

    def test_toggle_uses_before_state_without_resending_and_unknown_is_unverifiable(self):
        self.assertEqual(expected_outcome("toggle", {}, {"state": "on"}), {"state": "off"})
        self.assertIsNone(expected_outcome("toggle", {}, {"state": "unavailable"}))
        self.assertIsNone(expected_outcome("press", {"entity_id": "button.test"}))
        self.assertIsNone(expected_outcome("turn_on", {"entity_id": "scene.test"}))

    def test_parameter_values_are_verified_in_native_attributes(self):
        expected = expected_outcome("set_temperature", {"temperature": 22})
        self.assertTrue(outcome_matches({"state": "heat", "attributes": {"temperature": 22}}, expected))
        self.assertFalse(outcome_matches({"state": "heat", "attributes": {"temperature": 18}}, expected))
        self.assertTrue(outcome_matches({"state": "42"}, expected_outcome("set_value", {"value": 42})))
