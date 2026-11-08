import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from knowledge import Knowledge


class KnowledgeTests(unittest.TestCase):
    def test_home_vocabulary_validation_and_persistence(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "knowledge.sqlite3"
            store = Knowledge(path)
            store.set_entity("light.kitchen", ["Большой свет", "Большой свет"], "Кухня", "Основной свет")
            self.assertEqual(Knowledge(path).entities()["light.kitchen"]["aliases"], ["Большой свет"])
            with self.assertRaises(ValueError):
                store.set_entity("../../secret", [])
            with self.assertRaises(ValueError):
                store.set_entity("light.kitchen", ["bad\nname"])
            self.assertTrue(store.remove_entity("light.kitchen"))
            self.assertEqual(store.entities(), {})

    def test_memory_owner_and_provenance_are_preserved(self):
        with TemporaryDirectory() as directory:
            store = Knowledge(Path(directory) / "knowledge.sqlite3")
            store.remember("first", "Люблю тёплый свет", kind="preference")
            original = store.memories("first")[0]
            self.assertEqual(original["source"], "voice")
            self.assertEqual(store.memories("second"), [])
            self.assertFalse(store.forget("second", original["text"]))
            store.remember("first", original["text"], source="panel", kind="preference")
            self.assertEqual(store.memories("first")[0]["created_at"], original["created_at"])
            store.rename_memory("first", original["text"], "Люблю мягкий свет")
            self.assertEqual(store.memories("first")[0]["created_at"], original["created_at"])
            self.assertEqual(store.memories("first")[0]["kind"], "preference")
            self.assertEqual(store.memories("first")[0]["source"], "panel")
            self.assertEqual(store.memories("first")[0]["text"], "Люблю мягкий свет")
            self.assertTrue(store.forget("first", "Люблю мягкий свет"))
            self.assertEqual(store.memories("first"), [])


class KnowledgePanelTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_map_rejects_invalid_fields_and_checks_ha_entity(self):
        import json
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock
        from aiohttp import web
        from setup_panel import SetupPanel
        with TemporaryDirectory() as directory:
            knowledge = Knowledge(Path(directory) / "knowledge.sqlite3")
            brain = SimpleNamespace(_ha=AsyncMock(return_value={"entity_id": "light.test"}))
            panel = SetupPanel(SimpleNamespace(knowledge=knowledge, brain=brain))
            request = Mock(method="PATCH",match_info={"entity":"light.test"},json=AsyncMock(return_value={
                "aliases":["Люстра"],"room":"Кухня","purpose":"Основной свет"}))
            response = await panel.knowledge(request)
            self.assertEqual(json.loads(response.text)["aliases"],["Люстра"])
            brain._ha.assert_awaited_once_with("GET","/api/states/light.test")
            request.json=AsyncMock(return_value={"api_key":"bad"})
            with self.assertRaises(web.HTTPBadRequest):
                await panel.knowledge(request)
            request.method="DELETE"
            await panel.knowledge(request)
            self.assertEqual(knowledge.entities(),{})

    async def test_dashboard_preserves_existing_notes_and_provenance(self):
        import json
        from types import SimpleNamespace
        from unittest.mock import Mock
        from setup_panel import SetupPanel
        from personal import Personal
        with TemporaryDirectory() as directory:
            knowledge=Knowledge(Path(directory)/"knowledge.sqlite3")
            personal=Personal(Path(directory)/"personal.sqlite3")
            personal.remember("owner","Старый факт")
            personal.remember("owner","Новый факт")
            knowledge.remember("owner","Новый факт",kind="preference")
            panel=SetupPanel(SimpleNamespace(knowledge=knowledge,personal=personal,owner_key="owner",o={},family={}))
            response=await panel.personal(Mock(query={}))
            notes=json.loads(response.text)["notes"]
            self.assertEqual(len(notes),2)
            note=next(row for row in notes if row["text"]=="Новый факт")
            self.assertEqual(note["kind"],"preference")
            self.assertEqual(note["source"],"voice")
            self.assertEqual(note["owner_name"],"Владелец")
