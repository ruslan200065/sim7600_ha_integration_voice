import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio import rms

class TestAudio(unittest.TestCase):
    def test_silence(self):
        self.assertEqual(rms(b"\x00\x00" * 500), 0)

    def test_signal(self):
        self.assertEqual(rms(b"\xe8\x03" * 100), 1000)

if __name__=="__main__":
    unittest.main()
