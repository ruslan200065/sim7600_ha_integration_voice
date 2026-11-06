import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from brain import Brain


class BrainTools(unittest.IsolatedAsyncioTestCase):
    async def test_observation_does_not_grant_control(self):
        brain = Brain({
            "observed_entities": ["switch.readable"],
            "allow_domains": ["switch"],
        }, None, None)
        brain._ha = AsyncMock(return_value=[
            {"entity_id": "switch.readable", "state": "on",
             "attributes": {"friendly_name": "Лампа", "access_token": "private"}},
            {"entity_id": "switch.hidden", "state": "off"},
        ])
        brain._registry_metadata = AsyncMock(return_value={})
        result = await brain._tool("find_devices", {"query": "лампа"})
        self.assertEqual([row["entity_id"] for row in result["devices"]], ["switch.readable"])
        self.assertFalse(result["devices"][0]["can_control"])
        self.assertNotIn("access_token", result["devices"][0])
        brain._registry_metadata.assert_awaited_once_with({"switch.readable"})
        await brain._tool("call_ha_service", {"entity_id": "switch.readable", "service": "turn_off"})
        brain._ha.assert_awaited_once_with("GET", "/api/states")

    async def test_room_alias_and_inflected_name(self):
        brain = Brain({
            "allowed_entities": ["light.main", "light.bed"],
            "allow_domains": ["light"],
        }, None, None)
        brain._ha = AsyncMock(return_value=[
            {"entity_id": "light." + name, "state": "on", "attributes": {"friendly_name": "Люстра"}}
            for name in ("main", "bed")
        ])
        brain._registry_metadata = AsyncMock(return_value={
            "light.main": {"area": "Гостиная", "area_aliases": ["Зал"], "aliases": ["Верхний"]},
            "light.bed": {"area": "Спальня"},
        })
        for query in ("свет в гостиной", "верхний в зале", "light.main"):
            result = await brain._tool("find_devices", {"query": query})
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["devices"][0]["entity_id"], "light.main")
            self.assertIn("turn_off", result["devices"][0]["control_actions"])
        self.assertEqual((await brain._tool("find_devices", {"query": "люстра"}))["total"], 2)

    async def test_global_read_and_control_are_separate(self):
        sensor = {"entity_id": "sensor.temp", "state": "20"}
        light = {"entity_id": "light.hall", "state": "on"}
        brain = Brain({"observe_all_entities": True}, None, None)
        brain._ha = AsyncMock(return_value=sensor)
        self.assertEqual(await brain._tool("get_entity_state", {"entity_id": "sensor.temp"}), sensor)
        self.assertTrue(brain.camera_allowed("camera.hall"))
        brain._ha.reset_mock()
        await brain._tool("call_ha_service", {"entity_id": "light.hall", "service": "turn_on"})
        brain._ha.assert_not_awaited()

        brain.options = {"control_all_entities": True}
        brain._ha.return_value = light
        self.assertFalse(brain.camera_allowed("camera.hall"))
        self.assertFalse(brain._visible("sensor.temp"))
        self.assertTrue(brain._visible("light.hall"))
        result = await brain._tool("call_ha_service", {"entity_id": "light.hall", "service": "turn_on"})
        self.assertTrue(result["accepted"])
        brain._ha.assert_any_await("POST", "/api/services/light/turn_on", {"entity_id": "light.hall"})
        brain._ha.reset_mock()
        await brain._tool("call_ha_service", {"entity_id": "lock.front", "service": "unlock"})
        brain._ha.assert_not_awaited()

    async def test_snapshot_uses_event_camera_and_configured_photo_recipient(self):
        photo = AsyncMock()
        brain = Brain({
            "observed_entities": ["camera.gate"],
            "telegram_photo_target": "notify.telegram_bot_owner",
        }, None, None, share_photo=photo)
        brain._picture = AsyncMock(return_value=(b"image", "image/jpeg"))
        await brain._tool("send_camera_photo", {}, "camera.gate")
        photo.assert_not_awaited()
        await brain._tool(
            "send_camera_photo", {"target": "notify.untrusted"}, "camera.gate", explicit_photo=True,
        )
        brain._picture.assert_awaited_once_with("camera.gate")
        photo.assert_awaited_once_with("notify.telegram_bot_owner", b"image", "image/jpeg")

    async def test_photo_resolves_camera_name_without_matching_other_domains(self):
        photo = AsyncMock()
        brain = Brain({
            "observe_all_entities": True,
            "telegram_photo_target": "notify.owner",
        }, None, None, share_photo=photo)
        brain._ha = AsyncMock(return_value=[
            {"entity_id": "camera.yard", "attributes": {"friendly_name": "Камера двора"}},
            {"entity_id": "binary_sensor.yard", "attributes": {"friendly_name": "Двор"}},
        ])
        brain._picture = AsyncMock(return_value=(b"image", "image/jpeg"))
        await brain._tool("send_camera_photo", {"camera": "двор"}, explicit_photo=True)
        brain._picture.assert_awaited_once_with("camera.yard")
        photo.assert_awaited_once_with("notify.owner", b"image", "image/jpeg")
