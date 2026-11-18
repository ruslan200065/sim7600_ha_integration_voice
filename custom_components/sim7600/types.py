"""Data types for the SIM7600 integration."""

from __future__ import annotations

from dataclasses import dataclass, field


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
