"""DataUpdateCoordinator for SIM7600."""

from __future__ import annotations

import asyncio
import re
import time
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
    CONF_GNSS_INTERVAL,
    CONF_POLLING_INTERVAL,
    DEFAULT_BALANCE_INTERVAL,
    DEFAULT_GNSS_INTERVAL,
    DEFAULT_POLLING_INTERVAL,
    DOMAIN,
    EVENT_INCOMING_CALL,
    EVENT_SMS_RECEIVED,
    GPS_STALE_AFTER,
    ISSUE_NO_REGISTRATION,
    ISSUE_SIM_PIN,
    LOGGER,
    NO_REGISTRATION_ISSUE_AFTER,
    REGISTERED_STATUSES,
    SMS_CLEANUP_INTERVAL,
)
from .modem import ModemError, SIM7600Modem
from .types import GpsData, SmsData

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


class SIM7600DataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch SIM7600 data and route unsolicited modem events.

    - Static data (IMEI, firmware, manufacturer, model) is read once.
    - GPS is polled every gnss_interval; the fix expires after 10 minutes.
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
        self.gnss_interval: int = int(
            entry_option(entry, CONF_GNSS_INTERVAL, DEFAULT_GNSS_INTERVAL)
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

        self.gps_enabled: bool = False
        self.last_gnss_update: float = 0.0
        self._last_fix_time: float | None = None

        self._consecutive_failures: int = 0

        # Last known values.
        self._cached_rssi: int | None = None
        self._cached_signal_dbm: float | None = None
        self._cached_operator: str | None = None
        self._cached_network_mode: str | None = None
        self._cached_system_mode: str | None = None
        self._cached_reg_status: int | None = None
        self._cached_gprs_reg_status: int | None = None
        self._cached_sim_status: str | None = None
        self._cached_gps: GpsData | None = None
        self._cached_last_sms: SmsData | None = None

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
            self.hass.async_create_background_task(
                self.async_request_refresh(), name=f"{DOMAIN}_sms_refresh"
            )
        elif line.startswith("+CLIP:"):
            match = _CLIP_RE.match(line)
            self._incoming_call(match.group(1) or None if match else None)
        elif line == "RING" or line.startswith("+CRING:"):
            self._ring()
        elif line.startswith(("NO CARRIER", "MISSED_CALL:")):
            self._call_ended()

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
        """Cancel timers on unload."""
        self._call_ended()
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
        modem = self.modem
        if self.imei is None:
            self.imei = await modem.get_imei()
        if self.firmware is None:
            self.firmware = await modem.get_firmware()
        if self.manufacturer is None:
            self.manufacturer = await modem.get_manufacturer()
        if self.model is None:
            self.model = await modem.get_model()

        if not self.gps_enabled:
            self.gps_enabled = await modem.set_gps(True)
            LOGGER.debug("GPS enabled: %s", self.gps_enabled)

        new_rssi = await modem.get_signal_quality()
        if new_rssi is not None:
            self._cached_rssi = new_rssi
            self._cached_signal_dbm = -113 + (new_rssi * 2)

        new_operator = await modem.get_operator()
        if new_operator is not None:
            self._cached_operator = new_operator

        network_info = await modem.get_network_info()
        if network_info.get("mode") is not None:
            self._cached_network_mode = network_info["mode"]
        if network_info.get("system_mode") is not None:
            self._cached_system_mode = network_info["system_mode"]

        new_reg = await modem.get_registration_status()
        if new_reg is not None:
            self._cached_reg_status = new_reg
        new_gprs_reg = await modem.get_gprs_registration_status()
        if new_gprs_reg is not None:
            self._cached_gprs_reg_status = new_gprs_reg
        new_sim_status = await modem.get_sim_status()
        if new_sim_status is not None:
            self._cached_sim_status = new_sim_status

        now = time.time()
        await self._process_sms(now)

        if self.gps_enabled and now - self.last_gnss_update > self.gnss_interval:
            new_gps = await modem.get_gps_info()
            self.last_gnss_update = now
            if new_gps is not None:
                self._cached_gps = new_gps
                self._last_fix_time = now
                LOGGER.debug(
                    "GPS fix: lat=%.6f, lon=%.6f", new_gps.latitude, new_gps.longitude
                )

        await self._update_balance(now)
        self._update_issues(now)

        gps_fresh = (
            self._last_fix_time is not None
            and now - self._last_fix_time <= GPS_STALE_AFTER
        )
        return {
            "rssi": self._cached_rssi,
            "signal_dbm": self._cached_signal_dbm,
            "operator": self._cached_operator,
            "network_mode": self._cached_network_mode,
            "system_mode": self._cached_system_mode,
            "reg_status": self._cached_reg_status,
            "gprs_reg_status": self._cached_gprs_reg_status,
            "imei": self.imei,
            "firmware": self.firmware,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "sim_status": self._cached_sim_status,
            "last_sms": self._cached_last_sms,
            "gps": self._cached_gps if gps_fresh else None,
            "gps_last_fix": (
                dt_util.utc_from_timestamp(self._last_fix_time)
                if self._last_fix_time is not None
                else None
            ),
            "balance": self._balance,
            "balance_updated": (
                dt_util.utc_from_timestamp(self._balance_updated)
                if self._balance_updated is not None
                else None
            ),
        }

    async def _process_sms(self, now: float) -> None:
        messages = await self.modem.get_unread_sms()
        for sms in messages:
            self._cached_last_sms = sms
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
                await self.modem.delete_sms(index)

        if now - self._last_sms_cleanup >= SMS_CLEANUP_INTERVAL:
            if self.modem.has_pending_sms:
                LOGGER.debug("Skipping SMS cleanup: multipart message incomplete")
            else:
                await self.modem.delete_read_sms()
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
        sim = self._cached_sim_status
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
        statuses = (self._cached_reg_status, self._cached_gprs_reg_status)
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
