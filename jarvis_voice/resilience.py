"""Opt-in provider fallback without replaying tool side effects."""
from contextlib import aclosing
import array
import asyncio
from collections import deque
import io
import sys
import wave

import aiohttp
from audio import rms
from llm import active_profile, auth_headers, base_url


def fallback_profiles(options):
    primary = active_profile(options)
    profiles = [primary] if primary else []
    identifiers = options.get('llm_fallback_ids') or []
    if not isinstance(identifiers, list):
        raise ValueError('Резервные LLM должны быть списком профилей.')
    seen = {row['id'] for row in profiles}
    for identifier in identifiers:
        if not isinstance(identifier, str):
            raise ValueError('Укажите ID профиля резервной LLM.')
        row = active_profile({**options, 'llm_active': identifier})
        if row and row.get('model') and identifier not in seen:
            base_url(row['base_url'])
            profiles.append(dict(row))
            seen.add(identifier)
    return profiles


def fallback_allowed(executed_tools=False, spoken=False):
    """Provider failover can start only before tools or voice were emitted."""
    return not executed_tools and not spoken


def validate_speech_fallback(config):
    if not isinstance(config, dict):
        raise ValueError('Неверный профиль резервной речи.')
    allowed = {'base_url', 'api_key', 'stt_model', 'tts_model', 'voice'}
    if config.keys() - allowed:
        raise ValueError('Неизвестные настройки резервной речи.')
    result = {key: config.get(key, '') for key in allowed}
    result['base_url'] = base_url(result['base_url'])
    for key in ('api_key', 'stt_model', 'tts_model', 'voice'):
        value = result[key]
        if not isinstance(value, str) or len(value) > (1000 if key == 'api_key' else 256) or any(ord(c) < 32 for c in value):
            raise ValueError('Неверные параметры резервной речи.')
    if not result['stt_model'] or not result['tts_model'] or not result['voice']:
        raise ValueError('Укажите модели STT/TTS и голос резервного сервера.')
    return result


def pcm24_to_16(pcm):
    """Resample mono signed little-endian PCM24k to PCM16k without audioop."""
    if len(pcm) % 2:
        raise ValueError('Incomplete PCM sample')
    samples = array.array('h')
    samples.frombytes(pcm)
    if sys.byteorder != 'little':
        samples.byteswap()
    output = array.array('h')
    for index in range((len(samples) * 2) // 3):
        position = index * 1.5
        first = int(position)
        fraction = position - first
        second = min(first + 1, len(samples) - 1)
        output.append(round(samples[first] * (1 - fraction) + samples[second] * fraction))
    if sys.byteorder != 'little':
        output.byteswap()
    return output.tobytes()


class OpenAICompatibleSpeech:
    """HTTP /audio/transcriptions WAV input and /audio/speech PCM24k output contract."""
    def __init__(self, session, config):
        self.http = session
        self.config = validate_speech_fallback(config)

    async def close(self):
        # The shared HTTP session belongs to Server.
        pass

    async def recognize(self, pcm):
        if not pcm:
            return ''
        if len(pcm) > 16000 * 2 * 30:
            raise ValueError('Speech recording exceeds 30 seconds')
        recording = io.BytesIO()
        with wave.open(recording, 'wb') as wav:
            wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            wav.writeframes(pcm)
        form = aiohttp.FormData()
        form.add_field('model', self.config['stt_model'])
        form.add_field('file', recording.getvalue(), filename='voice.wav', content_type='audio/wav')
        async with self.http.post(self.config['base_url'] + '/audio/transcriptions',
                                 headers=auth_headers(self.config['api_key']), data=form,
                                 allow_redirects=False, timeout=aiohttp.ClientTimeout(total=30)) as response:
            if response.status != 200:
                raise RuntimeError('Fallback STT HTTP ' + str(response.status))
            data = await response.json()
        if not isinstance(data, dict) or not isinstance(data.get('text'), str):
            raise RuntimeError('Fallback STT returned invalid transcription')
        return data['text'].strip()[:2000]

    async def synthesize(self, text):
        payload = {'model': self.config['tts_model'], 'voice': self.config['voice'],
                   'input': text, 'response_format': 'pcm'}
        async with self.http.post(self.config['base_url'] + '/audio/speech',
                                 headers=auth_headers(self.config['api_key']), json=payload,
                                 allow_redirects=False, timeout=aiohttp.ClientTimeout(total=40)) as response:
            if response.status != 200:
                raise RuntimeError('Fallback TTS HTTP ' + str(response.status))
            raw = bytearray()
            async for chunk in response.content.iter_chunked(32768):
                raw.extend(chunk)
                if len(raw) > 8 * 1024 * 1024:
                    raise RuntimeError('Fallback TTS audio exceeds size limit')
        pcm = pcm24_to_16(bytes(raw))
        if not pcm:
            raise RuntimeError('Fallback TTS returned empty audio')
        for offset in range(0, len(pcm), 3200):
            yield pcm[offset:offset + 3200]

    async def recognize_live(self, source, threshold=700, *, on_voice_start=None, on_partial=None):
        pcm = await collect_utterance(source, threshold, on_voice_start=on_voice_start)
        text = await self.recognize(pcm)
        if text and on_partial:
            on_partial(text)
        return text


async def collect_utterance(source, threshold=700, *, initial=(), on_voice_start=None):
    frames = list(initial)
    voiced = any(rms(frame) >= threshold for frame in frames)
    clock = asyncio.get_running_loop().time
    started = clock()
    last_voice = started
    # Waiting for the first voice remains cancellable by the dialog idle timer.
    while not voiced or clock() - started < 20:
        try:
            frame = await asyncio.wait_for(source.get(), timeout=1)
        except TimeoutError:
            if voiced:
                break
            continue
        if rms(frame) >= threshold:
            if not voiced and on_voice_start:
                on_voice_start()
            voiced = True
            last_voice = clock()
        if voiced:
            frames.append(frame)
        else:
            frames = (frames + [frame])[-5:]
        if voiced and clock() - last_voice >= 0.8:
            break
        if sum(map(len, frames)) > 16000 * 2 * 25:
            break
    return b''.join(frames)


class SpeechRouter:
    """Keep the streaming primary fast; use the opt-in backup only on failure."""
    def __init__(self, primary, fallback, on_fallback=None):
        self.primary, self.fallback = primary, fallback
        self.on_fallback = on_fallback

    def _mark(self, stage):
        if self.on_fallback:
            self.on_fallback(stage)

    async def close(self):
        await self.primary.close()
        await self.fallback.close()

    async def recognize(self, pcm):
        try:
            return await self.primary.recognize(pcm)
        except Exception:
            self._mark('stt')
            return await self.fallback.recognize(pcm)

    async def recognize_live(self, source, threshold=700, *, on_voice_start=None, on_partial=None):
        frames = deque(maxlen=250)
        class CapturedSource:
            async def get(self):
                frame = await source.get()
                frames.append(frame)
                return frame
        try:
            return await self.primary.recognize_live(CapturedSource(), threshold,
                on_voice_start=on_voice_start, on_partial=on_partial)
        except Exception:
            self._mark('stt')
            pcm = await collect_utterance(source, threshold, initial=frames, on_voice_start=on_voice_start)
            text = await self.fallback.recognize(pcm)
            if text and on_partial:
                on_partial(text)
            return text

    async def synthesize(self, text):
        emitted = False
        try:
            async with aclosing(self.primary.synthesize(text)) as chunks:
                async for chunk in chunks:
                    if chunk:
                        emitted = True
                        yield chunk
            if emitted:
                return
            raise RuntimeError('Primary TTS returned empty audio')
        except Exception:
            if emitted:
                # Never append a second voice after partially playing the first.
                raise
            self._mark('tts')
            async with aclosing(self.fallback.synthesize(text)) as chunks:
                async for chunk in chunks:
                    yield chunk
