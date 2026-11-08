from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from llm import active_profile, base_url, completion_options, merge_profiles


class ProfileTests(unittest.TestCase):
    def test_switching_preserves_each_key_and_url_change_requires_explicit_key(self):
        profiles = merge_profiles([
            {"id": "one", "name": "First", "base_url": "https://one.example/v1/",
             "model": "first", "effort": "off", "api_key": "test-one"},
            {"id": "two", "name": "Second", "base_url": "https://two.example/v1",
             "model": "second", "api_key": "test-two"},
        ], [])
        public = [{k: v for k, v in item.items() if k != "api_key"} for item in profiles]
        saved = merge_profiles(public[::-1], profiles)
        config = {"llm_active": "one", "llm_profiles": saved, "deepseek_api_key": "test-built-in"}
        self.assertEqual(active_profile(config)["api_key"], "test-one")
        config["llm_active"] = "two"
        self.assertEqual(active_profile(config)["api_key"], "test-two")
        config["llm_active"] = "deepseek"
        self.assertEqual(active_profile(config)["api_key"], "test-built-in")
        with self.assertRaises(ValueError):
            merge_profiles([{**public[0], "base_url": "https://other.example/v1"}], saved)
        self.assertEqual(merge_profiles([{**public[0], "api_key": ""}], saved)[0]["api_key"], "")

    def test_effort_and_endpoint_validation(self):
        self.assertEqual(completion_options({"id": "custom", "effort": "default"}), {"max_tokens": 1024})
        self.assertEqual(completion_options({"id": "custom", "effort": "off"}), {"max_tokens": 1024})
        self.assertEqual(completion_options({"id": "custom", "effort": "max"}),
                         {"reasoning_effort": "high", "max_tokens": 4096})
        self.assertEqual(completion_options({"id": "custom", "effort": "low"})["reasoning_effort"], "low")
        self.assertEqual(completion_options({"id": "deepseek", "effort": "off"})["thinking"], {"type": "disabled"})
        self.assertEqual(completion_options({"id": "deepseek", "effort": "max"})["reasoning_effort"], "max")
        for url in ("ftp://example.org", "https://user@example.org", "https://example.org/?token=x",
                    "https://example.org/#x", "http://api.example.org/v1", "http://8.8.8.8/v1",
                    "https://example.org:99999/v1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                base_url(url)
        for url in ("http://192.168.1.10:11434/v1", "http://localhost:8080/v1", "http://ollama:11434/v1",
                    "http://llm.local/v1", "https://api.example.org/v1/"):
            with self.subTest(url=url):
                self.assertEqual(base_url(url), url.rstrip("/"))
