# Yandex Signal Q1 — hardware protocol from the user's original /opt archive

Original sources: user-provided YaSignalQ1(2).zip. This repository contains **hardware facts and independently written integration code only**, not private config, credentials or the original binaries.

| Device | Source | Confirmed |
| --- | --- | --- |
| USB camera | opt/test/hw-test.py | Sonix VID:PID 0c45:6366; YUYV and H264 |
| Analytics capture | opt/watch/signalq_capture.py | 640x480 YUYV at 30 fps, OpenCV YUV2RGB |
| Camera tuning | opt/watch/etc/camera.opts; scripts/set_camera.sh | 11 V4L2 controls; optional Sonix GOP=29 |
| Speaker | opt/test/hw-test.py; signalq_hw.py | Microchip USB2 Controller Hub ALSA card; playback with PyAudio output 'sysdefault' |
| Microphone | entire archive | **NO tested recording interface found** |
| SIM7600 | opt/watch/signalq_modem.py | /dev/modemAT at 9600 baud in Python, USB VID:PID 1e0e:9001 |
| LTE / QMI | opt/modem/modemcomms.sh | /dev/cdc-wdm0, wwan0; outside the scope of this App |
| GNSS | opt/misc/gps.py, signalq_gps.py | CGPSAUTO, CGPS, CGPSINFO and gpsd |
| Modem LEDs | opt/watch/signalq_modem.py | AT+CGDRT output pins 3,6,41; AT+CGSETV RGB |

## Device topology

**The USB devices must physically connect to the Home Assistant host.** If they still live on the original NanoPi, /dev/video0 and /dev/modemAT are paths on **that NanoPi**, not on HA OS. Either relocate the USB hardware or create a separate authenticated NanoPi bridge.

The original Q1 camera may support Full HD/H264 but the shipped *analytics* code explicitly requests 640x480 YUYV at 30 fps. The new snapshot path tries exactly this mode first. Live H264 recording and proprietary Sonix GOP configuration require additional software; never assume the camera exposes every listed mode on every USB interface.

## Diagnostics

After installing the JARVIS Home Assistant App, configure a private LAN API port, long random api_token, and the real modem AT port and Q1 video node. Every request except /health needs a Bearer header. Use GET:

- /q1/probe — USB VID/PID, available serial and video nodes, V4L2 formats, ALSA/Pulse capture/playback devices. No IMEI/ICCID/IMSI reported.
- /q1/snapshot — one JPEG using the verified Q1 YUYV mode with safe fallbacks.
- /q1/gps — SIM7600 AT+CGPS? and AT+CGPSINFO, converted to decimal WGS84 coordinates, returns fix=false before lock.

Use POST with JSON body:

- /q1/gps/start with {"assisted":true} — CGPSAUTO=1 (if not yet), CGPS=1,2. With false: standalone CGPS=1,1.
- /q1/led with {"color":"green"} — output channel 41 (red), 3 (green), 6 (blue); also supports red, blue, white, pink, yellow, cyan, off.
- /q1/camera/apply_original with {} — **opt-in** application of original camera.opts via v4l2-ctl. Never applies automatically.
- /q1/speak with {"text":"Привет"} — LiveTTS via configured ALSA output (e.g. pulse).
- /q1/listen with {"seconds":12} — microphone live streaming if the user confirms a valid ALSA input.

Example:

~~~bash
curl -H "Authorization: Bearer $JARVIS_TOKEN" http://HA_IP:8099/q1/probe
curl -H "Authorization: Bearer $JARVIS_TOKEN" http://HA_IP:8099/q1/snapshot --output q1.jpg
curl -H "Authorization: Bearer $JARVIS_TOKEN" http://HA_IP:8099/q1/gps
curl -X POST -H "Authorization: Bearer $JARVIS_TOKEN" \
  -H "Content-Type: application/json" -d '{"color":"blue"}' \
  http://HA_IP:8099/q1/led
~~~

## GPS and audio safety

On Home Assistant OS, the Audio plugin can expose the Sonix and Microchip devices through PulseAudio while the app has no direct ALSA card nodes. If `/q1/probe` lists the appropriate Pulse source and sink, select them in the app's Audio settings and use `pulse` for the matching `q1_mic_device` or `q1_speaker_device`. A source ending in `.monitor` records speaker playback. For microphone capture, use an input source such as the Sonix `alsa_input.usb-…` entry.

Original GPS bootstrap included setting operator-specific APN "internet", Google SUPL server, and modifying NVRAM via CNVW. These are **not enabled automatically**: they can break a different SIM/provider or alter module configuration. Current GPS uses the original CGPSAUTO/CGPS protocol without the APN/NVRAM side effects.

The Q1 original software **plays WAV files through the USB ALSA sound card** but has **no microphone capture test**. The Q1 USB audio card and SIM7600 phone PCM are distinct audio paths. Check /q1/probe, confirm a real input in HA's App Audio settings, then configure q1_mic_device/q1_speaker_device as pulse or a known ALSA device. Raw modem USB PCM audio on /dev/ttyUSB4 remains firmware-dependent and unverified for this Q1 SIM7600E.

Disable the original HACS SIM7600 integration while this App owns the AT port, otherwise both readers will corrupt response handling. Start calls only after live modem/GPS/audio tests on your hardware.
