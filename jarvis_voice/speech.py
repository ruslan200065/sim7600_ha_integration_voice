"""Yandex SpeechKit v3 gRPC: streaming STT and bidirectional LiveTTS."""
import asyncio
import grpc
import time
from collections import deque
from contextlib import aclosing
from audio import rms
import yandex.cloud.ai.stt.v3.stt_pb2 as stt
import yandex.cloud.ai.stt.v3.stt_service_pb2_grpc as stt_grpc
import yandex.cloud.ai.tts.v3.tts_pb2 as tts
import yandex.cloud.ai.tts.v3.tts_service_pb2_grpc as tts_grpc


def update_transcript(response, kind, finals, refinements):
    if kind == "final" and response.final.alternatives:
        finals[response.audio_cursors.final_index] = response.final.alternatives[0].text
    if kind == "final_refinement" and response.final_refinement.normalized_text.alternatives:
        update = response.final_refinement
        refinements[update.final_index] = update.normalized_text.alternatives[0].text


# Keep idle channels alive so the first request after a long pause does not
# hit a silently dropped connection (UNAVAILABLE).
GRPC_OPTIONS = (
    ("grpc.keepalive_time_ms", 60_000),
    ("grpc.keepalive_timeout_ms", 10_000),
    ("grpc.keepalive_permit_without_calls", 1),
    ("grpc.http2.max_pings_without_data", 0),
)


def transcript_text(finals, refinements):
    return " ".join(refinements.get(index) or finals.get(index, "")
                    for index in sorted(finals.keys() | refinements.keys())).strip()


class SpeechKit:
    def __init__(self, api_key, voice="denis", role="neutral", *, cache=None):
        self.auth = (("authorization", "Api-Key " + api_key),)
        self.voice = voice
        self.role = role
        self.cache = cache
        self.stt_channel = grpc.aio.secure_channel(
            "stt.api.cloud.yandex.net:443", grpc.ssl_channel_credentials(), options=GRPC_OPTIONS)
        self.tts_channel = grpc.aio.secure_channel(
            "tts.api.cloud.yandex.net:443", grpc.ssl_channel_credentials(), options=GRPC_OPTIONS)
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
        final = {}
        refined = {}
        async for response in self.stt_stub.RecognizeStreaming(requests(), metadata=self.auth, timeout=25):
            kind = response.WhichOneof("Event")
            update_transcript(response, kind, final, refined)
        return transcript_text(final, refined)


    async def recognize_live(self, source, threshold=700, *, on_voice_start=None, on_partial=None):
        """SpeechKit receives real-time PCM while user talks; local VAD ends turn."""
        preroll=deque(maxlen=5)
        # Don't open a paid SpeechKit stream during a long silent call.
        while True:
            chunk=await source.get()
            preroll.append(chunk)
            if rms(chunk)>=threshold:
                if on_voice_start:
                    on_voice_start()
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
                    audio_processing_type=stt.RecognitionModelOptions.REAL_TIME),
                # SpeechKit's own end-of-utterance detector answers faster than a fixed local pause.
                eou_classifier=stt.EouClassifierOptions(default_classifier=stt.DefaultEouClassifier(
                    type=stt.DefaultEouClassifier.HIGH, max_pause_between_words_hint_ms=600)))
            yield stt.StreamingRequest(session_options=options)
            for frame in preroll:
                yield stt.StreamingRequest(chunk=stt.AudioChunk(data=frame))
            last_voice = started = time.monotonic()
            while time.monotonic()-started<20:
                try:
                    frame=await asyncio.wait_for(source.get(), timeout=3)
                except TimeoutError:
                    break
                now=time.monotonic()
                if rms(frame)>=threshold:
                    last_voice=now
                yield stt.StreamingRequest(chunk=stt.AudioChunk(data=frame))
                if now-last_voice>1.0:
                    break

        final = {}
        refined = {}
        call = self.stt_stub.RecognizeStreaming(requests(), metadata=self.auth, timeout=35)
        try:
            async for response in call:
                kind = response.WhichOneof("Event")
                if kind == "partial" and on_partial and response.partial.alternatives:
                    on_partial(response.partial.alternatives[0].text)
                update_transcript(response, kind, final, refined)
                # Stop as soon as SpeechKit decides the phrase is over; the local pause is only a fallback.
                if kind == "eou_update" and transcript_text(final, refined):
                    break
        finally:
            call.cancel()
        return transcript_text(final, refined)

    async def synthesize(self, text):
        source = self.cache.stream(text, self.voice, self.role, self._synthesize) if self.cache else self._synthesize(text)
        async with aclosing(source) as chunks:
            async for chunk in chunks:
                yield chunk

    async def _synthesize(self, text):
        options = tts.SynthesisOptions(
            model="livetts", voice=self.voice, role=self.role,
            output_audio_spec=tts.AudioFormatOptions(raw_audio=tts.RawAudio(
                audio_encoding=tts.RawAudio.LINEAR16_PCM, sample_rate_hertz=16000)))
        async def requests():
            yield tts.StreamSynthesisRequest(options=options)
            yield tts.StreamSynthesisRequest(synthesis_input=tts.SynthesisInput(text=text.strip()+" "))
            yield tts.StreamSynthesisRequest(force_synthesis=tts.ForceSynthesisEvent())
        # Playback paces the stream in real time, so long phrases need a longer deadline.
        deadline = 30 + len(text) * 0.12
        async for response in self.tts_stub.StreamSynthesis(requests(), metadata=self.auth, timeout=deadline):
            if response.audio_chunk.data:
                yield response.audio_chunk.data
