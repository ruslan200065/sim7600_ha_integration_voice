import aiohttp.web  # noqa: F401
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
with patch.dict(sys.modules, {"speech": SimpleNamespace(SpeechKit=object)}):
    from brain import Brain


def make_brain(**options):
    base = {"llm_profiles": [], "deepseek_api_key": "key", "deepseek_model": "deepseek-chat"}
    return Brain({**base, **options}, None, None)


class SandboxTest(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_override_and_no_tools(self):
        brain = make_brain()
        brain.budget_exceeded = lambda: False
        brain._complete = AsyncMock(return_value="Добрый вечер!")
        reply = await brain.sandbox("Привет", "Говори как дворецкий.")
        self.assertEqual(reply, "Добрый вечер!")
        profile, messages, purpose = brain._complete.await_args.args
        self.assertEqual(purpose, "sandbox")
        self.assertTrue(messages[0]["content"].startswith("Говори как дворецкий."))
        self.assertEqual(messages[1], {"role": "user", "content": "Привет"})

    async def test_empty_reply_is_an_error(self):
        brain = make_brain()
        brain.budget_exceeded = lambda: False
        brain._complete = AsyncMock(return_value="")
        with self.assertRaises(RuntimeError):
            await brain.sandbox("Привет")

    async def test_budget_blocks_sandbox(self):
        brain = make_brain()
        brain.budget_exceeded = lambda: True
        brain._complete = AsyncMock()
        with self.assertRaises(RuntimeError):
            await brain.sandbox("Привет")
        brain._complete.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
