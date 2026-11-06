"""Non-invasive tests: invalid camera paths never run subprocesses."""
import asyncio
import pathlib
import sys
import unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from q1_hardware import VIDEO_PATH, ORIGINAL_CONTROLS, snapshot, probe_camera, apply_original_camera_controls


class Q1HardwareTests(unittest.TestCase):
    def test_q1_profile(self):
        self.assertEqual(ORIGINAL_CONTROLS["brightness"],20)
        self.assertEqual(ORIGINAL_CONTROLS["exposure_auto"],3)
        self.assertTrue(VIDEO_PATH.fullmatch("/dev/video0"))

    def test_no_arbitrary_camera_path(self):
        for path in ("/etc/passwd","/dev/video0;rm -rf /","/dev/video/0","/dev/ttyUSB2"):
            with self.assertRaises(ValueError):
                asyncio.run(snapshot(path))
            with self.assertRaises(ValueError):
                asyncio.run(probe_camera(path))
            with self.assertRaises(ValueError):
                asyncio.run(apply_original_camera_controls(path))


if __name__=="__main__":
    unittest.main()
