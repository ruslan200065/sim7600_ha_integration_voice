"""Pagination and structured device discovery regressions."""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from brain import Brain


class DeviceSearchTests(unittest.IsolatedAsyncioTestCase):
    def brain(self, rows, metadata=None):
        brain = Brain({"observe_all_entities": True}, None, None)
        brain._ha = AsyncMock(return_value=rows)
        brain._registry_metadata = AsyncMock(return_value=metadata or {})
        return brain

    async def test_all_matches_can_be_paged_with_stable_ties(self):
        rows = [{"entity_id": f"sensor.temp_{i:02}", "state": str(i),
                 "attributes": {"device_class": "temperature"}} for i in range(19)]
        brain = self.brain(rows)
        first = await brain._tool("find_devices", {"device_class": "temperature"})
        self.assertEqual(first["total"], 19)
        self.assertEqual(first["next_offset"], 8)
        brain._ha.return_value = list(reversed(rows))
        second = await brain._tool("find_devices", {"device_class": "temperature", "offset": 8})
        third = await brain._tool("find_devices", {"device_class": "temperature", "offset": 16})
        self.assertIsNone(third["next_offset"])
        identifiers = [row["entity_id"] for page in (first, second, third) for row in page["devices"]]
        self.assertEqual(identifiers, [row["entity_id"] for row in rows])

    async def test_room_alias_class_inference_live_state_and_permission_boundary(self):
        rows = [
            {"entity_id": "sensor.kitchen", "state": "22.5", "last_updated": "today",
             "attributes": {"unit_of_measurement": "°C", "access_token": "secret"}},
            {"entity_id": "sensor.bedroom", "state": "unavailable", "attributes": {"device_class": "temperature"}},
            {"entity_id": "sensor.hidden", "state": "30", "attributes": {"device_class": "temperature"}},
        ]
        brain = self.brain(rows, {"sensor.kitchen": {"area": "Кухня", "area_aliases": ["Столовая"]},
                                  "sensor.bedroom": {"area": "Спальня"}})
        brain.options = {"observed_entities": ["sensor.kitchen", "sensor.bedroom"]}
        result = await brain._tool("find_devices", {"room": "столовой", "device_class": "temperature", "domain": "sensor"})
        self.assertEqual(result["total"], 1)
        row = result["devices"][0]
        self.assertEqual(row["state"], "22.5")
        self.assertTrue(row["available"])
        self.assertEqual(row["device_class"], "temperature")
        self.assertEqual(row["last_updated"], "today")
        self.assertNotIn("secret", str(row))
        all_rows = (await brain._tool("find_devices", {}))["devices"]
        self.assertEqual(len(all_rows), 2)
        self.assertFalse(next(row for row in all_rows if row["entity_id"] == "sensor.bedroom")["available"])

    async def test_invalid_paging_is_rejected_and_past_end_is_empty(self):
        brain = self.brain([{"entity_id": "sensor.test", "state": "1"}])
        for args in ({"offset": -1}, {"limit": 0}, {"limit": 51}, {"offset": True}, {"room": []}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                await brain._tool("find_devices", args)
        page = await brain._tool("find_devices", {"offset": 5})
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["devices"], [])
        self.assertIsNone(page["next_offset"])
