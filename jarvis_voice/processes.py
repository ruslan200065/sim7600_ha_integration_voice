"""Own subprocess creation and cleanup across cancellation boundaries."""

import asyncio
from contextlib import suppress


async def finish_task(task):
    """Wait for cleanup after cancellation, including repeated cancellation requests."""
    while not task.done():
        with suppress(asyncio.CancelledError):
            await asyncio.shield(task)
    return task.result()


async def start_process(*args, **kwargs):
    """Reap a child even when cancellation races with its creation."""
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(*args, **kwargs))
    try:
        return await asyncio.shield(spawn)
    except asyncio.CancelledError:
        try:
            process = await finish_task(spawn)
        except OSError:
            pass
        else:
            await finish_task(asyncio.create_task(stop_process(process)))
        raise


async def stop_process(process):
    """Kill a running child and drain its pipes before releasing the owner."""
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()
    await process.communicate()


async def cancel_task(task):
    """Cancel a task once and let its cleanup finish before releasing ownership."""
    task.cancel()
    completion = asyncio.gather(task, return_exceptions=True)
    try:
        await asyncio.shield(completion)
    except asyncio.CancelledError:
        await finish_task(completion)
        raise
