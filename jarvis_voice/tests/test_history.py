"""Verify persistence, SMS deduplication and completed/missed call state."""

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from history import History


def sms_pdu(text, part=None):
    payload = text.encode("utf-16-be")
    flags = "00"
    if part:
        flags = "40"
        payload = bytes([5, 0, 3, 42, 2, part]) + payload
    return (
        "00"
        + flags
        + "04912143000862116012540000"
        + bytes([len(payload)]).hex()
        + payload.hex()
    )


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = pathlib.Path(directory) / "history.sqlite3"
        self.history = History(self.path)
        await self.history.start()

    async def test_import_joins_multipart_and_deduplicates_across_restart(self):
        first = ["+CMGL: 1,0,,30", sms_pdu("При", 1), "OK"]
        second = ["+CMGL: 4,1,,30", sms_pdu("вет", 2), "OK"]
        await self.history.import_sms(second)
        page = await self.history.page("sms", 0)
        self.assertEqual(page["items"][0]["state"], "1/2")
        await self.history.import_sms(first)
        restarted = History(self.path)
        await restarted.start()
        await restarted.import_sms(first)
        page = await restarted.page("sms", 0)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["items"][0]["text"], "Привет")
        self.assertEqual(page["items"][0]["state"], "received")

    async def test_call_directions_and_results(self):
        await self.history.call("incoming_call", {"call_id": 1})
        await self.history.call("caller_id", {"call_id": 1, "number": "+1234"})
        await self.history.call("call_ended", {"call_id": 1, "reason": "NO CARRIER"})
        await self.history.call("outgoing_call", {"call_id": 2, "number": "+4321"})
        await self.history.call("call_connected", {"call_id": 2})
        await self.history.call("call_ended", {"call_id": 2})
        await self.history.call("incoming_call", {"call_id": 3})
        restarted = History(self.path)
        await restarted.start()
        rows = (await restarted.page("call", 0))["items"]
        self.assertEqual(
            [row["state"] for row in rows], ["interrupted", "completed", "missed"]
        )
        self.assertEqual(rows[1]["direction"], "outgoing")
        self.assertIsNotNone(rows[1]["connected_at"])
        self.assertEqual(rows[2]["number"], "+1234")
