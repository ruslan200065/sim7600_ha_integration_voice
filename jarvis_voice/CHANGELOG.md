## 0.4.34 (experimental)

- Let the LLM select conversational tools, including call completion, gate access and reminder playback.
- Remove phrase matching from voice intents while preserving permission and input checks.

## 0.4.33 (experimental)

- Recognize conversational farewells, add a prompt editor and use a conversational voice preset.
- Support additional device types and validated settings for lights, covers, fans, timers and helpers.

## 0.4.32 (experimental)

- Reset USB PCM at call startup and verify incoming frames and a silent write before playing the greeting.

## 0.4.31 (experimental)

- Retry failed phone audio and speech playback during the same call, reopening the modem audio route when needed.

## 0.4.30 (experimental)

- Disable the call echo text filter by default and make it configurable.

## 0.4.29 (experimental)

- Recover from missed speech and acknowledge delayed voice turns.
- Understand natural requests for reminders and rules without fixed command wording.
- Add a panel for editing notes, preferences, reminders and rules.

## 0.4.28 (experimental)

- Cache Home Assistant registry metadata and invalidate it on registry updates.
- Announce the validated operation and target before executing supported voice actions.
- Give JARVIS caller-specific names, requested contact details and safe settings access.

## 0.4.27 (experimental)

- Add independent OpenAI-compatible LLM profiles, model discovery and configurable reasoning effort.
- Add family access presets and room selection, with more Home Assistant device types and supported controls.

## 0.4.26 (experimental)

- Expose local microphone level, sound activity and a monitoring switch in Home Assistant.
- Document the GSM assistant and installation in Russian.

## 0.4.25 (experimental)

- Reorganize the settings panel and add searchable Home Assistant pickers, editable family cards and a call recipient selector.

## 0.4.24 (experimental)

- Make the gate PIN optional and accept variable-length PINs over SIM7600 DTMF.
- End phone calls on clear farewells or sustained inactivity.
- Page through the calls and SMS journal with a filtered count and navigation controls.

## 0.4.23 (experimental)

- Run a permitted device action once when a zone or device condition becomes true.

## 0.4.22 (experimental)

- Keep the reminder confirmation word out of the spoken prompt so echo filtering accepts the caller’s answer.

## 0.4.21 (experimental)

- Add separate family caller profiles, PIN-protected gate actions and named automation recipients.
- Keep personal notes, learned sensor choices and reminders in local storage. Reminders call only the original requester and can mirror to their personal Home Assistant calendar.
- Add one-shot reminders for arrival at a Home Assistant zone or a sensor state change.
- Add bounded SpeechKit synthesis cache and more sensor classes and values.

## 0.4.20 (experimental)

- Speak only the latest conversation turn while earlier actions finish in the background.

## 0.4.19 (experimental)

- Accept natural requests for camera photos without asking the caller to repeat the command.

## 0.4.18 (experimental)

- Resolve spoken camera names before capture and prioritize the requested device type.

## 0.4.17 (experimental)

- Keep call recognition and playback independent of tool execution.
- Rank entity matches and return complete tool results for large Home Assistant installations.
- Confirm Telegram delivery using the service response and try the same camera substream if its main stream is unavailable.

## 0.4.16 (experimental)

- Add separate opt-in settings for reading all Home Assistant entities and controlling all supported entities.

## 0.4.15 (experimental)

- Find Home Assistant devices by room, registry alias and device type. Show the assistant which devices it can control and keep follow-up commands conversational.

## 0.4.14 (experimental)
- Fix HA action selectors so call camera and DTMF fields are visible in the action editor.

## 0.4.13 (experimental)
- Play a five-second local microphone sample in the private settings panel.

## 0.4.12 (experimental)
- Discover explicitly visible HA devices by name and report their current state.
- Separate observation from control, and support permitted media players, fans and scenes.
- Carry an event camera into a phone call, inspect camera snapshots, sample the Q1 microphone and send photos to a configured Telegram recipient.
- Announce messages through a configured HA TTS engine and allowed speakers.
- Send validated DTMF tones in an active call through the companion action.

## 0.4.11 (experimental)
- Listen during phone playback and stop a reply when the caller interrupts.
- Keep speech pre-roll between phone turns and filter transcripts that match recent assistant speech.

## 0.4.10 (experimental)
- Release the modem's USB PCM route after each call so later calls can start with a fresh audio route.

## 0.4.9 (experimental)
- Add a call playback volume control with a 50% default for JARVIS speech.
- Retry USB audio registration once when the modem initially rejects it during an active call.

## 0.4.8 (experimental)
- Include SIM detection errors in the network check.

## 0.4.7 (experimental)
- Use modem call-state reports as a fallback for missing RING, caller ID, and call connection indications.
- Show packet attachment and call-state reporting in the phone diagnostics.

## 0.4.6 (experimental)
- Archive call events and incoming SMS in a private SQLite database, with filters and paging in the web panel.
- Import PDU messages from the SIM and modem stores, deduplicate them and join multipart text.
- Show the modem USB mode and data APNs. Add a manual VoLTE enable request that reports the firmware's response.

## 0.4.5 (experimental)
- Include the operator, IMS registration and firmware responses to VoLTE queries in read-only cellular diagnostics.

## 0.4.4 (experimental)
- Buffer streaming synthesis briefly and deliver evenly paced, sample-aligned 20 ms PCM frames to the modem. Log the USB audio rate reported by the modem.

## 0.4.3 (experimental)
- Return the correct HTTP result after dialing and preserve the modem's error response when the call is rejected.

## 0.4.2 (experimental)
- Add outgoing call, answer, hangup, and read-only SIM/network checks to the phone settings panel.
- Accept extended voice ringing indications and international caller IDs from the modem.

## 0.4.1 (experimental)
- Preview the selected camera before applying settings and show a specific message if another process holds the device.

## 0.4.0 (experimental)
- Add a Russian-language setup panel for Home Assistant administrators. It uses the existing Supervisor options and includes camera, modem, GPS, audio and cloud key checks.
- Add labels and explanations to the standard add-on configuration.
- Share camera captures across simultaneous requests and hide call or speech actions when their prerequisites are missing.

## 0.3.2 (experimental)
- Return a clear error when the microphone produces no PCM, and document how to select the Q1 PulseAudio devices on Home Assistant OS.
- Report the default PulseAudio routes and fix parsing of allowlisted media URLs.

## 0.3.1 (experimental)
- Fix the startup crash under s6 by installing SpeechKit gRPC modules into the Python virtual environment and restoring the add-on environment at launch.
- Recover from AT-port read errors and timeouts, distinguish call failures from other AT replies, and bind prompts and caller authorization to the current call.
- Validate HTTP media redirects against the configured hosts and isolate FFmpeg from network and file access.
- Keep concurrent DeepSeek requests and incomplete tool exchanges from corrupting conversation history.
- Support stable camera paths under `/dev/v4l/by-id` and read GPS speed from the correct modem response field.

## 0.3.0 (experimental)
- App bundle contains an auto-installable, dedicated `jarvis_q1` companion custom integration.
- Supervisor discovery for one-click HA integration setup after one HA Core restart.
- Device entities: Q1 snapshot camera, generic media_player, signal/GPS/transcription sensors and action buttons.
- Optional live Q1 raw microphone PCM endpoint and pure `jarvis_q1.transcribe` STT without DeepSeek.
- Independent speak/listen/call/LED/GPS services and media-source/TTS audio playback.
- Security: App-generated random API token, allowlisted media URL hosts, managed marker for safe integration updates.
- Full Assist satellite, hardware microphone and modem phone PCM still require testing.

# Changelog

## 0.2.0 (experimental)
- Stock Q1 hardware mapping and USB diagnostics
- Q1 camera snapshot preferring original YUYV 640x480@30, optional camera opts
- GNSS enable/query and Q1 RGB LED on single AT-port owner
- Optional streaming microphone recognition and PulseAudio speaker
- Correct LiveTTS model and compatible denis/neutral voice
- Flexible UART and video mapping; tests and documentation
- Q1 microphone and modem phone-call PCM still require physical confirmation

# Changelog

## 0.1.0 (experimental)
- Independent HA App with serial call control, USB PCM bridge, SpeechKit v3 streaming STT + LiveTTS, DeepSeek multimodal and allowlisted HA functions
- Authenticated REST interface and docs
- No end-to-end hardware test; verify SIM7600E PCM support and UVC camera independently
