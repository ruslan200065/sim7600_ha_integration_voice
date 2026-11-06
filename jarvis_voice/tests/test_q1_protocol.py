"""Contract tests for genuine Q1 modem command conventions and GPS conversion."""
import asyncio
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from q1_protocol import gps_coordinate, parse_gpsinfo, gps_status, gps_start, led_set


class FakeModem:
    def __init__(self, responses):
        self.calls=[]
        self.responses=responses

    async def command(self, cmd, timeout=6):
        self.calls.append(cmd)
        return self.responses.get(cmd, ["OK"])


class Q1ProtocolTests(unittest.TestCase):
    def test_decimal_coordinates(self):
        self.assertAlmostEqual(gps_coordinate("5545.0000","N"),55.75)
        self.assertAlmostEqual(gps_coordinate("03737.5000","E",False),37.625)
        self.assertAlmostEqual(gps_coordinate("5545.0000","S"),-55.75)

    def test_invalid_gps(self):
        self.assertEqual(parse_gpsinfo(["+CGPSINFO: ,,,,,,,,"]),{"fix":False})
        with self.assertRaises(ValueError):
            gps_coordinate("9266.000","N")

    def test_gps_info(self):
        res=parse_gpsinfo(["+CGPSINFO: 5545.0000,N,03737.5000,E,061126,174010.0,120.3,12.5,270.0"])
        self.assertTrue(res["fix"])
        self.assertEqual(res["latitude"],55.75)
        self.assertEqual(res["longitude"],37.625)
        self.assertEqual(res["altitude_m"],120.3)
        self.assertEqual(res["speed_knots"],12.5)

    def test_gps_status_same_at_owner(self):
        modem=FakeModem({"AT+CGPS?":["+CGPS: 1,2","OK"],"AT+CGPSINFO":["+CGPSINFO: ,,,,,,,,","OK"]})
        res=asyncio.run(gps_status(modem))
        self.assertFalse(res["fix"])
        self.assertEqual(modem.calls,["AT+CGPS?","AT+CGPSINFO"])

    def test_gps_start_doesnt_write_nvram_or_apn(self):
        modem=FakeModem({"AT+CGPSAUTO?":["+CGPSAUTO: 0","OK"],"AT+CGPS?":["+CGPS: 0,0","OK"]})
        res=asyncio.run(gps_start(modem,assisted=True))
        self.assertTrue(res["gps_active"])
        self.assertIn("AT+CGPS=1,2",modem.calls)
        self.assertFalse(any("CNVW" in c or "CGSOCKCONT" in c for c in modem.calls))

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
