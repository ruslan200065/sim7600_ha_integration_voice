import aiohttp.web  # noqa: F401
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
with patch.dict(sys.modules, {"speech": SimpleNamespace(SpeechKit=object)}):
    from brain import Brain, sentence_cut


class Lines:
    def __init__(self, lines):
        self.lines = lines

    def __aiter__(self):
        self.iterator = iter(self.lines)
        return self

    async def __anext__(self):
        try:
            return next(self.iterator)
        except StopIteration:
            raise StopAsyncIteration from None


class FakeResponse:
    def __init__(self, chunks, content_type="text/event-stream"):
        self.status = 200
        self.content_type = content_type
        lines = [("data: " + json.dumps(chunk, ensure_ascii=False) + "\n").encode() for chunk in chunks]
        self.content = Lines(lines + [b"data: [DONE]\n"])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeHttp:
    def __init__(self, response):
        self.response = response
        self.payloads = []

    def post(self, url, *, json, **kwargs):
        self.payloads.append(json)
        return self.response


def delta(text=None, **extra):
    body = {"choices": [{"delta": {"content": text, **extra}, "finish_reason": None}]}
    return body


class StreamingTest(unittest.IsolatedAsyncioTestCase):
    def make(self, chunks, **options):
        http = FakeHttp(FakeResponse(chunks))
        base = {"llm_profiles": [], "deepseek_api_key": "key", "deepseek_model": "deepseek-chat"}
        return Brain({**base, **options}, http, None), http

    def test_sentence_cut_skips_short_fragments(self):
        self.assertEqual(sentence_cut("Да. ", 0), 0)
        text = "Свет в гостиной уже включён. Ещё"
        self.assertEqual(text[:sentence_cut(text, 0)], "Свет в гостиной уже включён.")

    async def test_sentences_are_voiced_before_reply_ends(self):
        brain, http = self.make([
            delta("Свет в гостиной уже включён. "),
            delta("Температура двадцать два"),
            delta(" градуса."),
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        ])
        spoken = []
        reply = await brain.reply("Как дома?", on_progress=spoken.append, phone_actions={})
        self.assertTrue(http.payloads[0]["stream"])
        self.assertEqual(spoken, ["Свет в гостиной уже включён."])
        self.assertEqual(reply, "Температура двадцать два градуса.")
        self.assertEqual(brain.history[-1]["content"],
                         "Свет в гостиной уже включён. Температура двадцать два градуса.")

    async def test_tool_calls_are_assembled_and_not_voiced(self):
        brain, _ = self.make([
            delta("Сейчас проверю, что там с ним. ", tool_calls=[
                {"index": 0, "id": "c1", "function": {"name": "get_entity_state", "arguments": '{"entity_'}}]),
            delta(None, tool_calls=[{"index": 0, "function": {"arguments": 'id": "light.hall"}'}}]),
        ])
        data = await brain._stream_chat(brain.http.response, [].append)
        call = data["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(call["function"]["arguments"], '{"entity_id": "light.hall"}')
        self.assertEqual(data["spoken"], 0)

    async def test_disabled_option_and_non_phone_replies_do_not_stream(self):
        brain, http = self.make([], stream_replies=False)
        http.response = FakeResponse([], content_type="application/json")

        async def body():
            return {"choices": [{"message": {"content": "Готово."}, "finish_reason": "stop"}]}
        http.response.json = body
        self.assertEqual(await brain.reply("Привет", on_progress=lambda text: None), "Готово.")
        self.assertNotIn("stream", http.payloads[0])
        brain.options["stream_replies"] = True
        self.assertEqual(await brain.reply("Привет"), "Готово.")
        self.assertNotIn("stream", http.payloads[1])


if __name__ == "__main__":
    unittest.main()
