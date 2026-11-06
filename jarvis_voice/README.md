# JARVIS SIM7600 Voice Bridge (experimental alpha)

In Home Assistant, open **Settings → Apps → JARVIS SIM7600 Voice Bridge → Open Web UI**. The setup panel groups hardware, API keys and phone options, saves them to the same Supervisor configuration as the Configuration tab, and can restart this app to apply changes. Only Home Assistant administrators can open the panel. No external port mapping is required.

Use the diagnostics column to check each device independently. A microphone check measures local PCM level and does not send the recording to SpeechKit. The separate SpeechKit check synthesizes a short test phrase. The GPS check reads status without changing the modem configuration.

The **Звонки и SMS** section lists calls observed by the running add-on and incoming messages present in the SIM or modem memory. The archive is private to the add-on and survives restarts. Import can mark an SMS as read on the modem. It leaves messages in their original stores. Use the storage counters to spot a full SIM or modem store.

This project adds an **independent Home Assistant App** to the HACS integration, without stealing its serial port. The App owns the AT and PCM ports. **Do not run the original HACS SIM7600 integration on the same AT port at the same time**.

## Supported now
- SIM7600 AT call dialing, answering, hanging up, call events; hourly rate limiting and caller allowlist
- SIM7600 raw USB PCM loopback path (/dev/ttyUSB4), per-call AT+CPCMFRM / AT+CPCMREG
- SpeechKit v3 streaming recognition transport and bidirectional **LiveTTS** gRPC PCM chunks
- DeepSeek Flash conversation and allowlisted HA services
- On-demand camera snapshots using a configured HA camera entity, sent as image to DeepSeek
- Authenticated local HTTP API for REST commands, plus diagnostics
- SMS/GPS/network remains available from the original integration **only when it is not competing for the same AT port**

## Critical limitations / hardware verification
- This release is **not confirmed end-to-end** on the user's SIM7600E firmware, SIM network, or Yandex Q1. A verified SIM7600G-H can expose raw 16k PCM on ttyUSB4; SIM7600E firmware may differ.
- The audio driver assumes /dev/ttyUSB4 delivers unframed, little-endian 16-bit 16kHz mono PCM. A positive AT command response does **not** prove usable audio. Confirm with hardware first.
- Voice interaction currently uses VAD turns (half-duplex), not interruption/barge-in. STT on phone and optionally Q1 microphone streams PCM chunks to SpeechKit while the user is speaking, after local voice detection. The phone call is still half-duplex (no barge-in).
- Phone-call PCM and local Q1 audio use separate devices. Check the playback and capture devices listed by `/q1/probe` before setting the optional ALSA devices.
- Home Assistant service calling is restricted to explicitly allowlisted entity IDs and light/switch/cover safe operations; no locks, alarms or arbitrary shell execution. Other operations need intentional code changes.
- SpeechKit/DeepSeek API calls are billable. No credentials are committed to GitHub.
- Incoming caller ID may differ by operator and firmware. Auto-answer defaults to false. Don't enable until tested.

## Install on Home Assistant OS
1. Back up Home Assistant.
2. Go to Settings > Apps > App store > ... > Repositories, add:
   https://github.com/ruslan200065/sim7600_ha_integration_voice
3. Install JARVIS SIM7600 Voice Bridge (build can take a while because Yandex gRPC protobuf stubs are generated).
4. Identify actual device paths in Settings > System > Hardware > All hardware. The sample ttyUSB2 and ttyUSB4 are **not guaranteed**. Prefer stable /dev/serial/by-id paths if they point to the correct ports. If your platform doesn't automatically expose devices, edit App device declarations to match.
5. Enter **allowed_number** in E.164, **api_token** (a long random value), **yandex_api_key**, **deepseek_api_key**, optional HA camera entity and your **allowed_entities**.
6. Disable the original SIM7600 HACS integration while the voice App owns its AT port.
7. Start the App, inspect logs. Test calls only after confirming your SIM contract permits voice calls.
8. Open an HTTP port mapping for 8099 **only on a trusted local network** if you need to drive it directly from HA automation; never expose it to Internet.

## API
Header for all endpoints except /health: Authorization: Bearer <api_token>

- GET /health → no secrets, modem state
- GET /status → state, signal, errors
- POST /dial {"number":"+7999...","prompt":"Сэр, возле машины движение. Включить прожектор?"} → calls only allowlisted number and speaks prompt when answered
- POST /hangup {} → disconnect
- POST /answer {} → accept only allowlisted incoming number
- POST /chat {"text":"Включи свет"} → test DeepSeek+HA without placing a call
- POST /speak {"text":"Привет"} → speak into currently active phone call
- POST /q1/listen {"seconds":4} → transcribe from configured ALSA mic, ask DeepSeek, optionally speak via ALSA
- POST /q1/speak {"text":"Привет"} → speak via configured ALSA output

Example from a trusted machine after mapping the HTTP port:
\`\`\`bash
curl -s -X POST http://HOME_ASSISTANT_IP:8099/chat \
  -H "Authorization: Bearer YOUR_LOCAL_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"Какие лампы можно включить?"}'
\`\`\`

Sample HA REST command (use secrets.yaml for the Bearer value):
\`\`\`yaml
rest_command:
  jarvis_call:
    url: "http://127.0.0.1:8099/dial"
    method: POST
    headers:
      Authorization: !secret jarvis_auth_header
      Content-Type: application/json
    payload: '{"number":"+79991234567"}'
    timeout: 20
\`\`\`
Note: 127.0.0.1 works **only** if the add-on endpoint is actually reachable at that address from HA Core. In HA OS Core and Apps have separate network namespaces: use the HA host IP and map the port instead.

## Yandex speech details
Uses SpeechKit API v3 via gRPC and generates official protobuf bindings during the add-on image build from yandex-cloud/cloudapi. TTS sends options + text + force_synthesis and plays output chunks immediately. STT uses streaming RecognizeStreaming.

## Camera integration
Connect the Q1 UVC camera to HA through a compatible camera provider (e.g. go2rtc/FFmpeg, if your installation exposes UVC to it). Set \`camera_entity: camera.yandex_q1\` (actual entity ID may differ).
Alternatively set `q1_video_device` to the camera's `/dev/v4l/by-id/…-video-index0` path or the corresponding `/dev/videoN` node for direct UVC capture. Use `/q1/probe` to check that the selected node exposes video formats. Configure `q1_mic_device` and `q1_speaker_device` using the capture and playback lists, respectively. DeepSeek can request a single snapshot using `see_camera`.

## Diagnostics
- /health says offline: check SIM7600 USB mode and App device permissions.
- +CPCMREG ERROR: check active voice call, module firmware and USB port assignment.
- Call rings without dialogue: inspect /status.last_error and /dev/ttyUSB4, confirm 16k PCM support.
- STT returns nothing: confirm audio amplitude, API service account roles and Yandex credentials.
- No camera: test HA /api/camera_proxy/camera.yandex_q1 manually.
- API unauthorized: configure api_token and Bearer header.

### Security
Never hardcode actual phone numbers or cloud tokens in GitHub. Restrict inbound API to trusted LAN, supply a random bearer token, allowlist HA entities and phone number, and keep max_calls_per_hour low.


## Q1 stock archive support (v0.2.0)

The original hardware source map is in [docs/Q1_HARDWARE.md](../docs/Q1_HARDWARE.md). New authenticated REST endpoints:

- GET /q1/probe: USB IDs, V4L2 formats, serial nodes, ALSA/Pulse sources and sinks.
- GET /q1/snapshot: Q1 UVC JPEG at stock YUYV 640x480/30 when possible.
- GET /q1/gps: GNSS state/fix returned in decimal coordinates.
- POST /q1/gps/start {"assisted":true}: start GPS using stock commands, no NVRAM/APN changes.
- POST /q1/led {"color":"red"}: modem RGB channels from the stock firmware.
- POST /q1/camera/apply_original {}: explicit original camera V4L2 controls.
- POST /q1/listen {"seconds":12}: start ALSA microphone and stream PCM to SpeechKit STT; requires a real audio input.
- POST /q1/speak {"text":"Hello"}: stream LiveTTS to configured Q1 speaker.

The App's audio/video/serial mapping no longer assumes both /dev/ttyUSB2 and /dev/ttyUSB4 must always exist. Configure actual device paths. Default LiveTTS is denis / neutral with model livetts; earlier voice jane is incompatible.

The stock archive **proves USB speaker playback** but does not prove an accessible microphone or USB modem call audio. Hardware tests are necessary.

## Bundled companion integration (v0.3.0)

On start the App can copy the companion integration to `/homeassistant/custom_components/jarvis_q1` and publish Supervisor Discovery. After the initial install **restart HA Core once** and confirm the discovered JARVIS Q1 integration; no HACS step is needed. Auto-install requires writable `homeassistant_config` mapping and never overwrites a component not managed by this App. To disable this write access behavior, set `auto_install_integration: false` and use manual setup instead.

The companion adds:
- `camera` snapshot entity for Q1, `media_player` usable by standard HA TTS/media-source playback.
- `sensor` for call state, GPS coordinates and latest speech text; `binary_sensor` for GPS fix and configured microphone.
- `button` for starting voice dialogue, GPS and hanging up.
- HA actions `jarvis_q1.speak`, `jarvis_q1.listen`, `jarvis_q1.transcribe` (without DeepSeek), call, LED and GPS.
- Q1 raw PCM microphone access on `/q1/microphone/stream` with authorization and a time limit.

The normal HA media player allows playback from specific HTTP(S) hosts on `media_allowed_hosts`; the host list must contain the hostname/IP used by your Home Assistant URL. See [Companion Integration](../docs/COMPANION_INTEGRATION.md).

An unverified microphone is **not** advertised as a working Assist Satellite. Native voice satellites require additional pipeline/wake word support; do not confuse the microphone capture service with that full integration.
