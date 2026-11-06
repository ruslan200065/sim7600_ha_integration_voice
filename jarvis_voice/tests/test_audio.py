import sys
import unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio import PCMTransport, PLAYBACK_FRAME, rms

class TestAudio(unittest.TestCase):
    def test_silence(self):
        self.assertEqual(rms(b"\x00\x00" * 500), 0)

    def test_signal(self):
        self.assertEqual(rms(b"\xe8\x03" * 100), 1000)


class TestCallPlayback(unittest.IsolatedAsyncioTestCase):
    async def test_write_joins_cloud_chunks_into_uniform_pcm_frames(self):
        pcm = b"\xe8\x03" * 700

        async def chunks():
            for start, end in ((0, 3), (3, 767), (767, len(pcm))):
                yield pcm[start:end]

        transport = PCMTransport("/dev/mock")
        transport.running = True
        transport.serial = Mock()
        transport.serial.write.side_effect = len
        await transport.write_stream(chunks())

        frames = [call.args[0] for call in transport.serial.write.call_args_list]
        self.assertEqual([len(frame) for frame in frames], [PLAYBACK_FRAME] * 3)
        self.assertEqual(b"".join(frames), pcm.ljust(PLAYBACK_FRAME * 3, b"\x00"))

    async def test_call_volume_scales_signed_samples_before_serial_write(self):
        async def chunks():
            yield b"\xe8\x03\x18\xfc"

        transport = PCMTransport("/dev/mock", playback_volume=50)
        transport.running = True
        transport.serial = Mock()
        transport.serial.write.side_effect = len
        await transport.write_stream(chunks())

        transport.serial.write.assert_called_once_with(
            b"\xf4\x01\x0c\xfe".ljust(PLAYBACK_FRAME, b"\x00")
        )


if __name__=="__main__":
    unittest.main()
