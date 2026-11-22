"""Validate and replay the add-on's ordered call event stream."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .client import JarvisClient, JarvisError
from .const import EVENT_CALL_ENDED, EVENT_CALL_INCOMING

_LOGGER = logging.getLogger(__name__)
_CALL_EVENTS = {EVENT_CALL_INCOMING, EVENT_CALL_ENDED}
_DELIVERY_STATES = {"pending", "companion", "direct"}


@dataclass(frozen=True, slots=True)
class CallEvent:
    sequence: int
    name: str
    delivery: str
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class EventBatch:
    stream: str
    durable: bool
    events: tuple[CallEvent, ...]

    @classmethod
    def from_status(cls, status: dict[str, Any]) -> EventBatch:
        stream = status["call_event_stream"]
        raw_events = status.get("call_events", [])
        if (
            not isinstance(stream, str)
            or not stream
            or not isinstance(raw_events, list)
        ):
            raise ValueError("invalid stream identifier or event list")

        events: list[CallEvent] = []
        previous = 0
        for raw in raw_events:
            if not isinstance(raw, dict):
                raise ValueError("an event is not an object")
            sequence = raw.get("sequence")
            name = raw.get("name")
            delivery = raw.get("delivery")
            data = raw.get("data")
            if type(sequence) is not int or sequence <= previous:
                raise ValueError("event sequences are invalid or out of order")
            if not isinstance(name, str) or name not in _CALL_EVENTS:
                raise ValueError("unknown call event name")
            if not isinstance(delivery, str) or delivery not in _DELIVERY_STATES:
                raise ValueError("unknown call event delivery state")
            if not isinstance(data, dict):
                raise ValueError("invalid call event data")
            events.append(CallEvent(sequence, name, delivery, data))
            previous = sequence
        return cls(stream, bool(status.get("call_events_durable")), tuple(events))


class CallEventReplay:
    def __init__(
        self, hass: HomeAssistant, entry_id: str, client: JarvisClient
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.client = client
        self._store = Store(hass, 1, f"jarvis_q1.call_events.{entry_id}")
        self._saved_cursor: tuple[str | None, int] | None = None
        self._stream: str | None = None
        self._sequence = 0

    async def accept(self, batch: EventBatch) -> None:
        if batch.durable and self._saved_cursor is None:
            saved = await self._store.async_load()
            if isinstance(saved, dict):
                stream = saved.get("stream")
                sequence = saved.get("sequence")
                if isinstance(stream, str) and type(sequence) is int and sequence >= 0:
                    self._stream, self._sequence = stream, sequence
            self._saved_cursor = self._stream, self._sequence

        self._replay(batch)
        if batch.durable:
            await self._save_and_acknowledge(batch)

    def _replay(self, batch: EventBatch) -> None:
        if self._stream is None and not batch.durable:
            # A new integration skips historical events from older, in-memory streams.
            self._sequence = batch.events[-1].sequence if batch.events else 0
        elif batch.stream != self._stream:
            self._sequence = 0
        self._stream = batch.stream

        for event in batch.events:
            if event.sequence <= self._sequence:
                continue
            if event.delivery == "pending":
                break
            if event.delivery == "companion":
                self.hass.bus.async_fire(
                    event.name,
                    {
                        **event.data,
                        "entry_id": self.entry_id,
                        "event_id": f"{batch.stream}:{event.sequence}",
                    },
                )
            self._sequence = event.sequence

    async def _save_and_acknowledge(self, batch: EventBatch) -> None:
        cursor = batch.stream, self._sequence
        if cursor != self._saved_cursor:
            await self._store.async_save(
                {"stream": batch.stream, "sequence": self._sequence}
            )
            self._saved_cursor = cursor
        if batch.events and self._sequence >= batch.events[0].sequence:
            try:
                await self.client.post(
                    "/call-events/ack",
                    {"stream": batch.stream, "sequence": self._sequence},
                    timeout=5,
                )
            except JarvisError:
                _LOGGER.debug(
                    "Call event acknowledgement will be retried", exc_info=True
                )
