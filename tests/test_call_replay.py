"""Companion replay survives an unsuccessful acknowledgement and Core restart."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant

from jarvis_voice.integration.jarvis_q1.client import JarvisConnectionError
from jarvis_voice.integration.jarvis_q1.coordinator import JarvisCoordinator


async def test_call_replay_deduplicates_after_core_restart(hass: HomeAssistant) -> None:
    events = []
    hass.bus.async_listen("jarvis_call_ended", events.append)
    entry = SimpleNamespace(entry_id="test-entry")
    payload = {
        "call_event_stream": "stream",
        "call_events_durable": True,
        "call_events": [
            {
                "sequence": 1,
                "name": "jarvis_call_ended",
                "delivery": "companion",
                "data": {"call_id": 42},
            },
        ],
    }
    client = SimpleNamespace(
        get=AsyncMock(return_value=payload),
        post=AsyncMock(side_effect=[JarvisConnectionError("offline"), {"ok": True}]),
    )

    first = JarvisCoordinator(hass, entry, client)
    await first._async_update_data()
    restarted = JarvisCoordinator(hass, entry, client)
    await restarted._async_update_data()
    await hass.async_block_till_done()
    assert [event.data["event_id"] for event in events] == ["stream:1"]
    assert client.post.await_count == 2
