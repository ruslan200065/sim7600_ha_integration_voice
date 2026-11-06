"""One coordinator shared by all JARVIS Q1 entities."""
from datetime import timedelta
import logging
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)

class JarvisCoordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass, entry, client):
        super().__init__(hass, _LOGGER, name="JARVIS Q1", update_interval=timedelta(seconds=20))
        self.entry = entry
        self.client = client
        self.last_transcript = ""
        self.last_answer = ""
        self.probe = {}

    async def async_probe(self):
        try:
            self.probe = await self.client.get("/q1/probe",timeout=20)
        except Exception as exc:
            _LOGGER.warning("Hardware probe not yet available: %s",exc)

    async def _async_update_data(self):
        try:
            data = await self.client.get("/status")
        except Exception as exc:
            raise UpdateFailed(str(exc)) from exc
        if data.get("call_state") != "offline":
            try:
                data["gps"] = await self.client.get("/q1/gps",timeout=9)
            except Exception:
                data["gps"] = {}
        else:
            data["gps"] = {}
        return data

    async def listen(self, seconds=12):
        result = await self.client.post("/q1/listen",{"seconds":seconds},timeout=seconds+40)
        self.last_transcript = str(result.get("recognized") or "")
        self.last_answer = str(result.get("response") or "")
        self.async_set_updated_data(self.data or {})
        self.hass.bus.async_fire("jarvis_q1_transcript", {
            "entry_id":self.entry.entry_id, "text":self.last_transcript,
            "response":self.last_answer})
        return result

    async def transcribe(self, seconds=12):
        result = await self.client.post("/q1/transcribe",{"seconds":seconds},timeout=seconds+40)
        self.last_transcript = str(result.get("text") or "")
        self.async_set_updated_data(self.data or {})
        self.hass.bus.async_fire("jarvis_q1_transcript",{
            "entry_id":self.entry.entry_id,"text":self.last_transcript,"response":""})
        return result
