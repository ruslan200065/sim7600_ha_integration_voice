"""Generic Q1 speaker that plays HA TTS and media-source files, not just JARVIS speech."""
from typing import Any
from homeassistant.components import media_source
from homeassistant.components.media_player import (
    MediaPlayerEntity, MediaPlayerEntityFeature, MediaPlayerState,
)
from homeassistant.components.media_player.browse_media import async_process_play_media_url
from .entity import JarvisEntity

async def async_setup_entry(hass,entry,async_add_entities):
    c=hass.data["jarvis_q1"][entry.entry_id]
    async_add_entities([Q1Speaker(c)])

class Q1Speaker(JarvisEntity,MediaPlayerEntity):
    _attr_supported_features = MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.STOP

    def __init__(self,coordinator):
        super().__init__(coordinator,"speaker","Speaker")

    @property
    def available(self):
        return super().available and bool((self.coordinator.data or {}).get("capabilities",{}).get("speaker_configured"))

    @property
    def state(self):
        return MediaPlayerState.PLAYING if (self.coordinator.data or {}).get("speaker_playing") else MediaPlayerState.IDLE

    async def async_play_media(self, media_type: str, media_id: str, **kwargs: Any):
        if media_source.is_media_source_id(media_id):
            item=await media_source.async_resolve_media(self.hass,media_id,self.entity_id)
            media_id=item.url
        url=async_process_play_media_url(self.hass,media_id)
        await self.coordinator.client.post("/q1/play_media",{"url":url})
        await self.coordinator.async_request_refresh()

    async def async_media_stop(self):
        await self.coordinator.client.post("/q1/stop_media")
        await self.coordinator.async_request_refresh()
