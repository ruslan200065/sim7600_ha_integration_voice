"""Contract tests for genuine Q1 modem command conventions."""
import asyncio
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from q1_protocol import led_set


class FakeModem:
    def __init__(self, responses):
        self.calls=[]
        self.responses=responses

    async def command(self, cmd, timeout=6):
        self.calls.append(cmd)
        return self.responses.get(cmd, ["OK"])


class Q1ProtocolTests(unittest.TestCase):
    def test_q1_led_original_channels(self):
        modem=FakeModem({})
        res=asyncio.run(led_set(modem,"red"))
        self.assertEqual(res["color"],"red")
        self.assertEqual(modem.calls, [
            "AT+CGDRT=3,1","AT+CGDRT=6,1","AT+CGDRT=41,1",
            "AT+CGSETV=41,1","AT+CGSETV=3,0","AT+CGSETV=6,0"
        ])

    def test_led_rejects_injection(self):
        with self.assertRaises(ValueError):
            asyncio.run(led_set(FakeModem({}),"red\rAT+CFUN=1"))


if __name__=="__main__":
    unittest.main()
