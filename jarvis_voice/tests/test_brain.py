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
        result = await brain._tool("find_devices", {"query": "лампа"})
        self.assertEqual(result["devices"], [
            {"entity_id": "switch.readable", "state": "on", "friendly_name": "Лампа"},
        ])
        await brain._tool("call_ha_service", {"entity_id": "switch.readable", "service": "turn_off"})
        brain._ha.assert_awaited_once_with("GET", "/api/states")

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
