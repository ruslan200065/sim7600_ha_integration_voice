# JARVIS SIM7600 Voice Bridge (experimental alpha)

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
- Voice interaction currently uses VAD turns (half-duplex), not interruption/barge-in. STT uses real streaming gRPC transport but buffers one VAD utterance before sending to Yandex. Future refinement: send samples during capture for lower latency.
- Phone-call audio and Q1 microphone/speaker are **not the same device**. USB UVC video does not imply UAC audio. Q1 audio output/input not wired up yet. Camera via HA camera entity works separately.
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
- POST /dial {"number":"+7999..."} → calls only allowlisted number
- POST /hangup {} → disconnect
- POST /answer {} → accept only allowlisted incoming number
- POST /chat {"text":"Включи свет"} → test DeepSeek+HA without placing a call
- POST /speak {"text":"Привет"} → speak into currently active phone call

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
The LLM only fetches a single snapshot when it calls \`see_camera\`, not constant video. Avoid putting private camera sources on a public URL.

## Diagnostics
- /health says offline: check SIM7600 USB mode and App device permissions.
- +CPCMREG ERROR: check active voice call, module firmware and USB port assignment.
- Call rings without dialogue: inspect /status.last_error and /dev/ttyUSB4, confirm 16k PCM support.
- STT returns nothing: confirm audio amplitude, API service account roles and Yandex credentials.
- No camera: test HA /api/camera_proxy/camera.yandex_q1 manually.
- API unauthorized: configure api_token and Bearer header.

### Security
Never hardcode actual phone numbers or cloud tokens in GitHub. Restrict inbound API to trusted LAN, supply a random bearer token, allowlist HA entities and phone number, and keep max_calls_per_hour low.
