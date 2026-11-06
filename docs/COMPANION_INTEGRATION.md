# JARVIS Q1 companion integration (experimental v0.3.0)

## One-install workflow

1. Add this GitHub repository in Home Assistant Settings → Apps → App Store → Repositories.
2. Install JARVIS SIM7600 Voice Bridge. The App has option `auto_install_integration: true` by default.
3. On startup the App copies its bundled, matching-version companion to `/homeassistant/custom_components/jarvis_q1`. The App will **never overwrite** another manually-installed integration without its own management marker.
4. App sends Supervisor Discovery (hostname, port and automatically generated API secret; none are written to GitHub).
5. **Restart Home Assistant Core once**. Open Settings → Devices & services and confirm discovered JARVIS Q1 Voice Gateway. Further App updates also require a Core restart if the integration files changed.

This is deliberately not a hidden HA restart, and uninstalling the App does not silently delete the companion. Writable `homeassistant_config` is required only for the auto-install; review that permission or disable `auto_install_integration` and install manually.

## Reusable HA entities

| Platform | Purpose |
| --- | --- |
| camera | Q1 USB UVC JPEG snapshot; enabled after `q1_video_device` is configured |
| media_player | Q1 speaker; accepts `media_player.play_media`, HA media-source files and compatible `tts.speak` actions |
| sensor | Modem call state, cellular signal, GPS latitude/longitude, last microphone transcript |
| binary_sensor | GPS fix and microphone-configuration indicators (configured does not guarantee working capture) |
| button | Listen and respond, enable GPS, end call |

HA creates entity IDs from the device and names; use the IDs listed under your own device, not guesses from this document.

## Independent HA actions

- `jarvis_q1.speak` with `text` — use streaming Yandex LiveTTS on the Q1 speaker.
- `jarvis_q1.transcribe` with `seconds` (3–30) — streaming microphone STT, **no DeepSeek**; provides service response with recognized text, fires `jarvis_q1_transcript` event and updates a sensor.
- `jarvis_q1.listen` with `seconds` — record a voice turn, send it to DeepSeek and optionally speak the result.
- `jarvis_q1.call` with optional `prompt`; `jarvis_q1.hangup`.
- `jarvis_q1.set_led` with color; `jarvis_q1.gps_start`; `jarvis_q1.probe`.

Example announcement from any automation:

~~~yaml
action: jarvis_q1.speak
data:
  text: "Ворота открылись"
~~~

Example capture for reuse in automation:

~~~yaml
action: jarvis_q1.transcribe
data:
  seconds: 12
~~~

The integration also exposes a last-transcript sensor and emits the `jarvis_q1_transcript` HA event, including `text` and `response`. Other automations can trigger on this event.

Q1 microphone raw stream, if ALSA input works: authenticated `GET /q1/microphone/stream?seconds=15`, 16 kHz signed mono PCM. No continuous recording runs unless a user-triggered client requests it.

## General-purpose speaker

The Q1 media player can use a configured HA TTS provider:

~~~yaml
action: tts.speak
target:
  entity_id: tts.YOUR_TTS_PROVIDER
data:
  media_player_entity_id: media_player.YOUR_Q1_SPEAKER
  message: "На улице обнаружено движение"
~~~

Audio-source URLs must use HTTP(S), with host explicitly permitted via the App `media_allowed_hosts`. Defaults include `homeassistant`, `supervisor` and `homeassistant.local`; add your real HA hostname/IP when needed.

## Hardware and capability limits

The original Q1 archive confirms Microchip **speaker playback**, but not a microphone capture device. A real Q1 mic must be tested in `/q1/probe` before STT or the raw audio endpoint can work. Likewise SIM7600E phone PCM over USB is not confirmed from that archive. Camera direct USB access requires the device to be on the same physical HA host; NanoPi-attached USB cannot appear automatically on another HA OS machine.

This is reusable mic/speaker support, **not yet** a full native `assist_satellite` entity with wake-word, multi-turn Assist pipeline, and intercom. The explicit raw PCM endpoint is the foundation for a separate satellite layer.

API traffic is authenticated by an App-generated secret shared only through Supervisor discovery; do not expose port 8099 to Internet.
