"""DataUpdateCoordinator for SIM7600."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BALANCE_INTERVAL,
    CONF_BALANCE_USSD,
    CONF_DEFAULT_RECIPIENT,
    CONF_POLLING_INTERVAL,
    DEFAULT_BALANCE_INTERVAL,
    DEFAULT_POLLING_INTERVAL,
    DOMAIN,
    EVENT_INCOMING_CALL,
    EVENT_SMS_RECEIVED,
    ISSUE_NO_REGISTRATION,
    ISSUE_SIM_PIN,
    LOGGER,
    NO_REGISTRATION_ISSUE_AFTER,
    REGISTERED_STATUSES,
    SMS_CLEANUP_INTERVAL,
)
from .modem import ModemError, SIM7600Modem
from .types import SmsData

# A ringing call repeats RING (+CLIP) every few seconds. Events inside this
# window belong to the same call.
CALL_WINDOW = 10.0
# Time to wait for +CLIP after RING before firing an event without number.
RING_CLIP_WAIT = 1.0
_CLIP_RE = re.compile(r'^\+CLIP:\s*"([^"]*)"')
_SIM_LOCKED_MARKERS = ("PIN", "PUK")


def entry_option(entry: ConfigEntry, key: str, default: Any) -> Any:
    """Read a setting from options, then data, then the default."""
    value = entry.options.get(key)
    if value is None:
        value = entry.data.get(key, default)
    return default if value is None else value


@dataclass(slots=True)
class NetworkState:
    """Last successful values from the independent network queries."""

    rssi: int | None = None
    operator: str | None = None
    mode: str | None = None
    system_mode: str | None = None
    registration: int | None = None
    packet_registration: int | None = None
    sim_status: str | None = None

    @property
    def signal_dbm(self) -> int | None:
        if self.rssi is None:
            return None
        return -113 + self.rssi * 2


class SIM7600DataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch SIM7600 data and route unsolicited modem events.

    - Static data (IMEI, firmware, manufacturer, model) is read once.
    - Last known values are kept when a single AT command returns nothing.
    - UpdateFailed is raised when the connection fails or the modem stops
      answering (ModemTimeoutError after consecutive timeouts).
    - Every received SMS fires `sim7600_sms_received` and is deleted.
    - +CMTI triggers an immediate refresh, RING/+CLIP fires
      `sim7600_incoming_call`.
    """

    def __init__(
        self, hass: HomeAssistant, modem: SIM7600Modem, entry: ConfigEntry
    ) -> None:
        """Initialize the coordinator."""
        self.polling_interval: int = int(
            entry_option(entry, CONF_POLLING_INTERVAL, DEFAULT_POLLING_INTERVAL)
        )
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=self.polling_interval),
        )
        self.modem = modem
        self.entry_id: str = entry.entry_id
        self.default_recipient: str | None = (
            entry_option(entry, CONF_DEFAULT_RECIPIENT, "") or None
        )
        self.balance_ussd: str | None = (
            entry_option(entry, CONF_BALANCE_USSD, "") or None
        )
        self.balance_interval: float = (
            float(entry_option(entry, CONF_BALANCE_INTERVAL, DEFAULT_BALANCE_INTERVAL))
            * 3600
        )

        # Static data, read once.
        self.imei: str | None = None
        self.firmware: str | None = None
        self.manufacturer: str | None = None
        self.model: str | None = None

        self._consecutive_failures: int = 0

        self._network = NetworkState()
        self._last_sms: SmsData | None = None
        self._sms_refresh_task: asyncio.Task[None] | None = None
        self._sms_refresh_pending = False

        # First cleanup only after one interval, so read SMS that existed
        # before the integration was set up are not removed immediately.
        self._last_sms_cleanup: float = time.time()
        self._last_balance_update: float = 0.0
        self._balance: str | None = None
        self._balance_updated: float | None = None
        self._unregistered_since: float | None = None

        self._call_active_until: float = 0.0
        self._ring_handle: asyncio.TimerHandle | None = None
        self.last_call: dict[str, Any] | None = None

        modem.urc_callback = self.handle_urc

    # ------------------------------------------------------------------
    # Unsolicited result codes
    # ------------------------------------------------------------------

    @callback
    def handle_urc(self, line: str) -> None:
        """Handle a URC from the modem (runs in the event loop)."""
        if line.startswith("+CMTI:"):
            LOGGER.debug("New SMS indication: %s", line)
            self._sms_refresh_pending = True
            if self._sms_refresh_task is None or self._sms_refresh_task.done():
                self._sms_refresh_task = self.hass.async_create_background_task(
                    self._refresh_for_sms(), name=f"{DOMAIN}_sms_refresh"
                )
        elif line.startswith("+CLIP:"):
            match = _CLIP_RE.match(line)
            self._incoming_call(match.group(1) or None if match else None)
        elif line == "RING" or line.startswith("+CRING:"):
            self._ring()
        elif line.startswith(("NO CARRIER", "MISSED_CALL:")):
            self._call_ended()

    async def _refresh_for_sms(self) -> None:
        while self._sms_refresh_pending:
            self._sms_refresh_pending = False
            await self.async_request_refresh()

    def _ring(self) -> None:
        now = time.monotonic()
        if now < self._call_active_until:
            self._call_active_until = now + CALL_WINDOW
            return
        if self._ring_handle is None:
            self._ring_handle = self.hass.loop.call_later(
                RING_CLIP_WAIT, self._ring_without_clip
            )

    @callback
    def _ring_without_clip(self) -> None:
        self._ring_handle = None
        if time.monotonic() >= self._call_active_until:
            self._incoming_call(None)

    def _incoming_call(self, number: str | None) -> None:
        if self._ring_handle is not None:
            self._ring_handle.cancel()
            self._ring_handle = None
        now = time.monotonic()
        active = now < self._call_active_until
        self._call_active_until = now + CALL_WINDOW
        if active:
            return
        self.last_call = {"number": number, "time": dt_util.utcnow().isoformat()}
        self.hass.bus.async_fire(
            EVENT_INCOMING_CALL, {"number": number, "entry_id": self.entry_id}
        )

    def _call_ended(self) -> None:
        self._call_active_until = 0.0
        if self._ring_handle is not None:
            self._ring_handle.cancel()
            self._ring_handle = None

    async def async_shutdown(self) -> None:
        """Stop unsolicited updates and cancel pending work."""
        self.modem.urc_callback = None
        self._call_ended()
        self._sms_refresh_pending = False
        if self._sms_refresh_task is not None:
            self._sms_refresh_task.cancel()
            await asyncio.gather(self._sms_refresh_task, return_exceptions=True)
            self._sms_refresh_task = None
        await super().async_shutdown()

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the modem."""
        try:
            data = await self._fetch()
        except Exception as err:
            self._consecutive_failures += 1
            if self._consecutive_failures > 3:
                LOGGER.warning(
                    "SIM7600 unreachable for %d consecutive update cycles",
                    self._consecutive_failures,
                )
            raise UpdateFailed(f"Error communicating with SIM7600: {err}") from err
        self._consecutive_failures = 0
        return data

    async def _fetch(self) -> dict[str, Any]:
        await self._refresh_identity()
        await self._refresh_network()
        now = time.time()
        await self._process_sms(now)

        await self._update_balance(now)
        self._update_issues(now)

        return {
            "rssi": self._network.rssi,
            "signal_dbm": self._network.signal_dbm,
            "operator": self._network.operator,
            "network_mode": self._network.mode,
            "system_mode": self._network.system_mode,
            "reg_status": self._network.registration,
            "gprs_reg_status": self._network.packet_registration,
            "imei": self.imei,
            "firmware": self.firmware,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "sim_status": self._network.sim_status,
            "last_sms": self._last_sms,
            "balance": self._balance,
            "balance_updated": (
                dt_util.utc_from_timestamp(self._balance_updated)
                if self._balance_updated is not None
                else None
            ),
        }

    async def _refresh_identity(self) -> None:
        modem = self.modem
        if self.imei is None:
            self.imei = await modem.get_imei()
        if self.firmware is None:
            self.firmware = await modem.get_firmware()
        if self.manufacturer is None:
            self.manufacturer = await modem.get_manufacturer()
        if self.model is None:
            self.model = await modem.get_model()

    async def _refresh_network(self) -> None:
        modem = self.modem
        new_rssi = await modem.get_signal_quality()
        if new_rssi is not None:
            self._network.rssi = new_rssi

        new_operator = await modem.get_operator()
        if new_operator is not None:
            self._network.operator = new_operator

        network_info = await modem.get_network_info()
        if network_info.get("mode") is not None:
            self._network.mode = network_info["mode"]
        if network_info.get("system_mode") is not None:
            self._network.system_mode = network_info["system_mode"]

        new_reg = await modem.get_registration_status()
        if new_reg is not None:
            self._network.registration = new_reg
        new_gprs_reg = await modem.get_gprs_registration_status()
        if new_gprs_reg is not None:
            self._network.packet_registration = new_gprs_reg
        new_sim_status = await modem.get_sim_status()
        if new_sim_status is not None:
            self._network.sim_status = new_sim_status

    async def _process_sms(self, now: float) -> None:
        messages = await self.modem.get_unread_sms()
        deletion_failed = False
        for sms in messages:
            self._last_sms = sms
            self.hass.bus.async_fire(
                EVENT_SMS_RECEIVED,
                {
                    "sender": sms.sender,
                    "text": sms.message,
                    "timestamp": sms.timestamp,
                    "entry_id": self.entry_id,
                    "parts": sms.parts,
                    "complete": sms.complete,
                },
            )
            for index in sms.indices:
                if not await self.modem.delete_sms(index):
                    deletion_failed = True

        if deletion_failed:
            LOGGER.debug(
                "Postponing bulk SMS cleanup after an individual delete failure"
            )
            return
        if now - self._last_sms_cleanup >= SMS_CLEANUP_INTERVAL:
            if self.modem.has_pending_sms:
                LOGGER.debug("Skipping SMS cleanup: multipart message incomplete")
            elif await self.modem.delete_read_sms():
                self._last_sms_cleanup = now

    async def _update_balance(self, now: float) -> None:
        if not self.balance_ussd:
            return
        if now - self._last_balance_update < self.balance_interval:
            return
        self._last_balance_update = now
        try:
            reply = await self.modem.send_ussd(self.balance_ussd)
        except (ModemError, ValueError) as err:
            LOGGER.warning("Balance request %s failed: %s", self.balance_ussd, err)
            return
        if reply.message is not None:
            self._balance = reply.message
            self._balance_updated = now

    def _update_issues(self, now: float) -> None:
        sim = self._network.sim_status
        pin_issue = f"{ISSUE_SIM_PIN}_{self.entry_id}"
        if sim is not None:
            upper = sim.upper()
            if upper != "READY" and any(m in upper for m in _SIM_LOCKED_MARKERS):
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    pin_issue,
                    is_fixable=False,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key=ISSUE_SIM_PIN,
                    translation_placeholders={"status": sim, "port": self.modem.port},
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, pin_issue)

        reg_issue = f"{ISSUE_NO_REGISTRATION}_{self.entry_id}"
        statuses = (self._network.registration, self._network.packet_registration)
        if all(status is None for status in statuses):
            return
        if any(status in REGISTERED_STATUSES for status in statuses):
            self._unregistered_since = None
            ir.async_delete_issue(self.hass, DOMAIN, reg_issue)
            return
        if self._unregistered_since is None:
            self._unregistered_since = now
        if now - self._unregistered_since >= NO_REGISTRATION_ISSUE_AFTER:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                reg_issue,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_NO_REGISTRATION,
                translation_placeholders={
                    "minutes": str(int((now - self._unregistered_since) // 60)),
                    "port": self.modem.port,
                },
            )
