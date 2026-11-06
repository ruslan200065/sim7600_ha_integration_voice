"""Yandex SpeechKit v3 gRPC: streaming STT and bidirectional LiveTTS."""
import grpc
import time
from collections import deque
from audio import rms
import yandex.cloud.ai.stt.v3.stt_pb2 as stt
import yandex.cloud.ai.stt.v3.stt_service_pb2_grpc as stt_grpc
import yandex.cloud.ai.tts.v3.tts_pb2 as tts
import yandex.cloud.ai.tts.v3.tts_service_pb2_grpc as tts_grpc


class SpeechKit:
    def __init__(self, api_key, voice="jane"):
        self.auth = (("authorization", "Api-Key " + api_key),)
        self.voice = voice
        self.stt_channel = grpc.aio.secure_channel("stt.api.cloud.yandex.net:443", grpc.ssl_channel_credentials())
        self.tts_channel = grpc.aio.secure_channel("tts.api.cloud.yandex.net:443", grpc.ssl_channel_credentials())
        self.stt_stub = stt_grpc.RecognizerStub(self.stt_channel)
        self.tts_stub = tts_grpc.SynthesizerStub(self.tts_channel)

    async def close(self):
        await self.stt_channel.close()
        await self.tts_channel.close()

    async def recognize(self, pcm: bytes):
        async def requests():
            opts = stt.StreamingOptions(
                recognition_model=stt.RecognitionModelOptions(
                    audio_format=stt.AudioFormatOptions(raw_audio=stt.RawAudio(
                        audio_encoding=stt.RawAudio.LINEAR16_PCM,
                        sample_rate_hertz=16000,
                        audio_channel_count=1)),
                    language_restriction=stt.LanguageRestrictionOptions(
                        restriction_type=stt.LanguageRestrictionOptions.WHITELIST,
                        language_code=["ru-RU"]),
                    audio_processing_type=stt.RecognitionModelOptions.REAL_TIME))
            yield stt.StreamingRequest(session_options=opts)
            for offset in range(0, len(pcm), 3200):
                yield stt.StreamingRequest(chunk=stt.AudioChunk(data=pcm[offset:offset+3200]))
        final = []
        refined = []
        async for response in self.stt_stub.RecognizeStreaming(requests(), metadata=self.auth, timeout=25):
            kind = response.WhichOneof("Event")
            if kind == "final" and response.final.alternatives:
                final.append(response.final.alternatives[0].text)
            if kind == "final_refinement" and response.final_refinement.normalized_text.alternatives:
                refined.append(response.final_refinement.normalized_text.alternatives[0].text)
        return " ".join(refined or final).strip()


    async def recognize_live(self, source, threshold=700):
        """SpeechKit receives real-time PCM while user talks; local VAD ends turn."""
        preroll=deque(maxlen=5)
        # Don't open a paid SpeechKit stream during a long silent call.
        while True:
            chunk=await source.get()
            preroll.append(chunk)
            if rms(chunk)>=threshold:
                break

        async def requests():
            options = stt.StreamingOptions(
                recognition_model=stt.RecognitionModelOptions(
                    audio_format=stt.AudioFormatOptions(raw_audio=stt.RawAudio(
                        audio_encoding=stt.RawAudio.LINEAR16_PCM,
                        sample_rate_hertz=16000,
                        audio_channel_count=1)),
                    language_restriction=stt.LanguageRestrictionOptions(
                        restriction_type=stt.LanguageRestrictionOptions.WHITELIST,
                        language_code=["ru-RU"]),
                    audio_processing_type=stt.RecognitionModelOptions.REAL_TIME))
            yield stt.StreamingRequest(session_options=options)
            for frame in preroll:
                yield stt.StreamingRequest(chunk=stt.AudioChunk(data=frame))
            last_voice = started = time.monotonic()
            while time.monotonic()-started<20:
                try:
                    frame=await __import__("asyncio").wait_for(source.get(),timeout=3)
                except __import__("asyncio").TimeoutError:
                    break
                now=time.monotonic()
                if rms(frame)>=threshold:
                    last_voice=now
                yield stt.StreamingRequest(chunk=stt.AudioChunk(data=frame))
                if now-last_voice>0.8:
                    break

        final=[]
        refined=[]
        async for response in self.stt_stub.RecognizeStreaming(requests(),metadata=self.auth,timeout=35):
            kind=response.WhichOneof("Event")
            if kind=="final" and response.final.alternatives:
                final.append(response.final.alternatives[0].text)
            if kind=="final_refinement" and response.final_refinement.normalized_text.alternatives:
                refined.append(response.final_refinement.normalized_text.alternatives[0].text)
        return " ".join(refined or final).strip()

    async def synthesize(self, text):
        options = tts.SynthesisOptions(
            voice=self.voice,
            output_audio_spec=tts.AudioFormatOptions(raw_audio=tts.RawAudio(
                audio_encoding=tts.RawAudio.LINEAR16_PCM, sample_rate_hertz=16000)))
        async def requests():
            yield tts.StreamSynthesisRequest(options=options)
            yield tts.StreamSynthesisRequest(synthesis_input=tts.SynthesisInput(text=text.strip()+" "))
            yield tts.StreamSynthesisRequest(force_synthesis=tts.ForceSynthesisEvent())
        async for response in self.tts_stub.StreamSynthesis(requests(), metadata=self.auth, timeout=30):
            if response.audio_chunk.data:
                yield response.audio_chunk.data
