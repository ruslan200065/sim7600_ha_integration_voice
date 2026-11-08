"""Bounded disk cache for complete synthesized PCM streams."""
import asyncio
import hashlib
import os
from contextlib import aclosing
from pathlib import Path
from uuid import uuid4

# 16 kHz mono s16 = 32000 B/s; even very fast speech stays under ~40 chars/s.
# A stream shorter than this is treated as truncated and never cached.
MIN_BYTES_PER_CHAR = 32000 // 40


class SpeechCache:
    def __init__(self, directory, limit=64 * 1024 * 1024):
        self.directory = Path(directory)
        self.limit = limit

    async def stream(self, text, voice, role, synthesize):
        key = hashlib.sha256(f"livetts:16000:{voice}:{role}:{text.strip()}".encode()).hexdigest()
        path = self.directory / (key + ".pcm")
        try:
            cached = await asyncio.to_thread(self.read, path)
        except OSError:
            cached = b""
        if cached:
            for offset in range(0, len(cached), 3200):
                yield cached[offset:offset + 3200]
            return
        chunks = bytearray()
        async with aclosing(synthesize(text)) as stream:
            async for chunk in stream:
                if len(chunks) <= 512 * 1024:
                    chunks.extend(chunk)
                yield chunk
        if MIN_BYTES_PER_CHAR * len(text.strip()) <= len(chunks) <= 512 * 1024 and chunks:
            try:
                await asyncio.to_thread(self.write, path, bytes(chunks))
            except OSError:
                pass

    @staticmethod
    def read(path):
        data = path.read_bytes()
        os.utime(path, None)
        return data

    def write(self, path, content):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.directory / uuid4().hex
        try:
            with open(temporary, "wb", opener=lambda name, flags: os.open(name, flags, 0o600)) as file:
                file.write(content)
            temporary.replace(path)
            entries = sorted(((item.stat(), item) for item in self.directory.glob("*.pcm")),
                             key=lambda entry: entry[0].st_mtime)
            total = sum(info.st_size for info, _ in entries)
            for info, item in entries:
                if total <= self.limit:
                    break
                item.unlink(missing_ok=True)
                total -= info.st_size
        finally:
            temporary.unlink(missing_ok=True)
