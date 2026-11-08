import aiohttp.web  # noqa: F401
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
with patch.dict(sys.modules,{"speech":SimpleNamespace(SpeechKit=object)}):
    from setup_panel import SetupPanel
from aiohttp import web
from notifications import NotificationPolicies
from timeline import Timeline
from workflows import WorkflowManager


class OperationsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory()
        root=Path(self.tmp.name)
        self.owner=WorkflowManager(root/"workflow.sqlite3","owner")
        self.family=WorkflowManager(root/"workflow.sqlite3","member")
        self.service=SimpleNamespace(owner_key="owner",o={},family={"phone":{"key":"member","name":"Member"}},
            policies=NotificationPolicies(root/"notifications.sqlite3"),timeline=Timeline(root/"timeline.sqlite3"),
            brain=SimpleNamespace(features=SimpleNamespace(workflows=self.owner)),
            caller_brain=lambda member:SimpleNamespace(features=SimpleNamespace(workflows=self.family)))
        self.panel=SetupPanel(self.service)
    def tearDown(self):self.tmp.cleanup()
    def request(self,method="GET",profile="owner",body=None,**query):
        return Mock(method=method,query={"profile":profile,**query},json=AsyncMock(return_value=body))
    async def test_profile_boundary_and_scoped_reads(self):
        self.owner.save("Owner",[{"tool":"get_entity_state","arguments":{"entity_id":"sensor.owner"}}])
        self.family.save("Member",[{"tool":"get_entity_state","arguments":{"entity_id":"sensor.member"}}])
        self.service.timeline.record("owner",1,"stt","recognized",details={"text":"private"})
        response=await self.panel.operations(self.request(profile="member"))
        data=json.loads(response.text)
        self.assertEqual([row["name"] for row in data["scenarios"]],["Member"])
        self.assertEqual(data["timeline"]["total"],0)
        self.assertEqual(data["policy"]["channel"],"call")
        with self.assertRaises(web.HTTPNotFound):await self.panel.operations(self.request(profile="stranger"))
    async def test_policy_update_scoped_and_invalid_input_rejected(self):
        response=await self.panel.operations(self.request("PATCH","member",{"policy":{"channel":"telegram","telegram_target":"-1001234567"}}))
        self.assertEqual(json.loads(response.text)["policy"]["channel"],"telegram")
        self.assertEqual(self.service.policies.get("owner")["channel"],"call")
        for body in ({"policy":{"channel":"invalid"}},{"policy":{},"profile":"owner"},[]):
            with self.subTest(body=body),self.assertRaises(web.HTTPBadRequest):
                await self.panel.operations(self.request("PATCH","member",body))
    async def test_cancel_and_delete_do_not_access_other_profile(self):
        started=asyncio.Event()
        async def waiting(*args):
            started.set();await asyncio.Future()
        task=self.owner.start("Owner task",[{"tool":"get_entity_state","arguments":{"entity_id":"sensor.owner"}}],waiting)
        await started.wait()
        response=await self.panel.operations(self.request("POST","member",{"action":"cancel_task","id":task["id"]}))
        self.assertIn("error",json.loads(response.text))
        self.assertEqual(self.owner.get(task["id"])["status"],"running")
        self.owner.save("Private",[{"tool":"get_entity_state","arguments":{"entity_id":"sensor.owner"}}])
        response=await self.panel.operations(self.request("POST","member",{"action":"delete_scenario","name":"Private"}))
        self.assertFalse(json.loads(response.text)["deleted"])
        response=await self.panel.operations(self.request("POST","owner",{"action":"cancel_task","id":task["id"]}))
        self.assertTrue(json.loads(response.text)["cancelled"])
        await self.owner.close()
    async def test_scenario_editor_saves_plan_only_and_checks_selected_permissions(self):
        brain=SimpleNamespace(features=SimpleNamespace(workflows=self.family),
            _visible=lambda entity: entity in {"light.member", "sensor.member"},
            _control_actions=lambda entity: {"turn_on","turn_off"} if entity=="light.member" else set(),
            camera_allowed=lambda entity: entity=="camera.member")
        self.service.caller_brain=lambda member:brain
        steps=[{"tool":"call_ha_service","arguments":{"entity_id":"light.member","service":"turn_on","brightness_pct":30,"confirmed":True}},
               {"tool":"get_entity_state","arguments":{"entity_id":"sensor.member"}}]
        response=await self.panel.operations(self.request("POST","member",{"action":"save_scenario","name":"Evening","steps":steps}))
        self.assertTrue(json.loads(response.text)["saved"])
        saved=self.family.scenario("Evening")
        self.assertEqual(saved[0]["arguments"]["brightness_pct"],30)
        self.assertNotIn("confirmed",saved[0]["arguments"])
        self.assertEqual(self.family.list_tasks(),[])
        self.assertEqual(self.owner.list_scenarios(),[])
        for bad_steps in (
            [{"tool":"call_ha_service","arguments":{"entity_id":"light.owner","service":"turn_on"}}],
            [{"tool":"call_ha_service","arguments":{"entity_id":"sensor.member","service":"turn_on"}}],
            [{"tool":"call_ha_service","arguments":{"entity_id":"light.member","service":"unlock"}}],
            [{"tool":"see_camera","arguments":{"camera":"camera.owner"}}],
            [{"tool":"run_when","arguments":{}}], [],
        ):
            with self.subTest(steps=bad_steps),self.assertRaises(web.HTTPBadRequest):
                await self.panel.operations(self.request("POST","member",{"action":"save_scenario","name":"Forbidden","steps":bad_steps}))
        self.assertEqual([row["name"] for row in self.family.list_scenarios()],["Evening"])

    async def test_bad_page_fails_cleanly(self):
        with self.assertRaises(web.HTTPBadRequest):await self.panel.operations(self.request(offset="-1"))
        with self.assertRaises(web.HTTPBadRequest):await self.panel.operations(self.request(offset="oops"))
