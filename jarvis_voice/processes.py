"""Own subprocess creation and cleanup across cancellation boundaries."""
import asyncio
from contextlib import suppress


async def start_process(*args, **kwargs):
    """Reap a child even when cancellation races with its creation."""
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(*args, **kwargs))
    try:
        return await asyncio.shield(spawn)
    except asyncio.CancelledError:
        try:
            process = await spawn
        except OSError:
            pass
        else:
            await stop_process(process)
        raise


async def stop_process(process):
    """Kill a running child and drain its pipes before releasing the owner."""
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()
    await process.communicate()
