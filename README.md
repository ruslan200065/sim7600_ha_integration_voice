# SIM7600 4G & GPS Gateway Integration for Home Assistant

This custom integration allows you to connect a SIM7600 4G & GPS module to Home Assistant via a serial port (USB). It provides real-time monitoring of network status, signal strength, SMS capabilities, and GPS tracking.

## Features

*   **Network Monitoring**:
    *   Signal Strength (RSSI in dBm)
    *   Operator name
    *   Network Mode (LTE, GSM, etc.)
    *   System status
*   **Diagnostics**:
    *   IMEI number
    *   Firmware version
    *   SIM card status
*   **SMS Support**:
    *   Send SMS via `sim7600.send_sms` service
    *   Sensor for the last received SMS (with sender and timestamp attributes)
*   **GPS Tracking**:
    *   Integrated as a `device_tracker` entity (latitude, longitude, altitude)
*   **Setup**: 
    *   Easy UI configuration
    *   Support for automatic USB discovery

## Installation

### Via HACS (Recommended)
1. Open HACS in your Home Assistant.
2. Click the three dots (⋮) in the top right -> **Custom repositories**.
3. Add `https://github.com/ruslan200065/sim7600_ha_integration_voice` as category **Integration**.
4. Search for "SIM7600 4G & GPS Gateway" and click **Download**.
5. Restart Home Assistant.

### Manual Installation
1. Copy the `custom_components/sim7600` directory to your `custom_components` folder in your Home Assistant configuration directory.
2. Restart Home Assistant.

## Configuration
1. Go to **Settings** -> **Devices & Services**.
2. Click **+ Add Integration**.
3. Search for **SIM7600 4G & GPS Gateway**.
4. Select the serial port (e.g., `/dev/ttyUSB2`) and baud rate.

## Services
*   `sim7600.send_sms`:
    *   `number`: Recipient's phone number.
    *   `message`: Message text.

---
*Built with ❤️ for Home Assistant.*


## JARVIS SIM7600 Voice Bridge / Yandex Signal Q1 (experimental)

This fork also contains an independent **Home Assistant App**, located in the jarvis_voice folder.

- SIM7600 call control, optional experimental raw PCM voice channel, Yandex streaming STT + LiveTTS, DeepSeek and allowlisted HA tools.
- Original Yandex Signal Q1 protocols: UVC camera (0c45:6366), GPS, modem RGB LEDs, Microchip USB speaker.
- Self-diagnostics, UVC snapshots, optional ALSA/Pulse microphone and playback.

Install the repository in the Home Assistant App store as a custom repository:
https://github.com/ruslan200065/sim7600_ha_integration_voice

See [docs/Q1_HARDWARE.md](docs/Q1_HARDWARE.md) and [jarvis_voice/README.md](jarvis_voice/README.md).

**Experimental hardware:** The stock Q1 archive does not demonstrate microphone input or phone-call PCM transfer on this SIM7600E firmware. Do not run both this App and the original HACS integration against the same AT port.

## JARVIS Q1 companion Home Assistant integration

The `jarvis_voice` Home Assistant App now bundles and **auto-installs** its matching custom integration `jarvis_q1` (option `auto_install_integration: true`). Start the App, restart Home Assistant Core once, then confirm **JARVIS Q1 Voice Gateway** under discovered integrations in Devices & Services. No separate HACS installation or manual copying is needed.

Creates a Q1 camera, general-purpose `media_player` speaker, cellular/GPS sensors, microphone transcript sensor and buttons. HA actions include `jarvis_q1.speak`, `jarvis_q1.transcribe`, `jarvis_q1.listen`, `jarvis_q1.call`, LED and GPS. Speech transcription can be consumed by any automation without DeepSeek; raw Q1 PCM microphone streaming is available for custom clients. Full Assist satellite and hardware microphone compatibility still require further work/testing.

See [Companion Integration](docs/COMPANION_INTEGRATION.md) for step-by-step installation, use and security constraints.
