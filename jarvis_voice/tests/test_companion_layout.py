"""Validate bundled integration discovery/install package and schema without HA installed."""
import ast
import json
import pathlib
import unittest

BASE=pathlib.Path(__file__).resolve().parents[1]
COMPANION=BASE/"integration"/"jarvis_q1"

class CompanionTests(unittest.TestCase):
    def test_manifest(self):
        doc=json.loads((COMPANION/"manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["domain"],"jarvis_q1")
        self.assertTrue(doc["config_flow"])
        for name in ("__init__.py","config_flow.py","coordinator.py","sensor.py",
                     "button.py","media_player.py","camera.py","services.yaml"):
            self.assertTrue((COMPANION/name).exists(),name)

    def test_python_syntax(self):
        for path in COMPANION.glob("*.py"):
            with self.subTest(file=path.name):
                ast.parse(path.read_text(encoding="utf-8"))

    def test_launcher_has_installer(self):
        self.assertIn("install_integration.py",(BASE/"run.sh").read_text(encoding="utf-8"))

    def test_discovery_configured(self):
        config=(BASE/"config.yaml").read_text(encoding="utf-8")
        self.assertIn("- jarvis_q1",config)
        self.assertIn("auto_install_integration",config)
        self.assertIn("homeassistant_config",config)

if __name__=="__main__":
    unittest.main()
