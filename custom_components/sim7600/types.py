"""Data types for the SIM7600 integration."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GpsData:
    """GPS position from the modem (AT+CGPSINFO).

    All fields are mandatory. If a field is missing in the modem response,
    None is returned instead of a partially filled GpsData object.
    `speed` is reported by the modem in knots. `fix_time` is the local
    monotonic-independent wall clock time (time.time()) the fix was received.
    """

    latitude: float
    longitude: float
    altitude: float
    speed: float
    date: str
    time: str
    fix_time: float | None = None


@dataclass
class SmsData:
    """SMS message read from modem storage (AT+CMGL).

    `index` is the storage index of the first part. `indices` lists all
    storage slots of a (possibly concatenated) message.
    """

    index: int
    sender: str
    timestamp: str
    message: str
    indices: tuple[int, ...] = field(default_factory=tuple)
    parts: int = 1
    complete: bool = True

    def __post_init__(self) -> None:
        """Default indices to the single storage index."""
        if not self.indices:
            self.indices = (self.index,)
