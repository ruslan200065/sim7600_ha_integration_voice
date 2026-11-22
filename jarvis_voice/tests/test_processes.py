"""Regression tests for subprocess ownership during shutdown."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from processes import start_process, stop_process


@pytest.mark.asyncio
async def test_cancelling_spawn_reaps_late_child(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    process = Mock(returncode=None, communicate=AsyncMock())

    async def delayed_spawn(*args, **kwargs):
        started.set()
        await release.wait()
        return process

    monkeypatch.setattr("processes.asyncio.create_subprocess_exec", delayed_spawn)
    owner = asyncio.create_task(start_process("test-child"))
    await started.wait()
    owner.cancel()
    await asyncio.sleep(0)
    owner.cancel()
    await asyncio.sleep(0)
    assert not owner.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await owner
    process.kill.assert_called_once()
    process.communicate.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_process_drains_exited_child():
    process = Mock(returncode=0, communicate=AsyncMock())
    await stop_process(process)
    process.kill.assert_not_called()
    process.communicate.assert_awaited_once()
