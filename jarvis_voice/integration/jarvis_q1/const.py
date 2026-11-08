"""JARVIS Q1 companion integration constants."""
DOMAIN = "jarvis_q1"
PLATFORMS = ("sensor", "binary_sensor", "button", "camera", "media_player", "switch")
CONF_HOST = "host"
CONF_PORT = "port"
CONF_TOKEN = "token"
CONF_SOURCE = "source"
CONF_ENTRY_ID = "entry_id"
DEFAULT_PORT = 8099
UNIQUE_ID = "jarvis_q1_voice_bridge"

# Event bus names. SIGNAL_TRANSCRIPT keeps its historic name for compatibility.
SIGNAL_TRANSCRIPT = "jarvis_q1_transcript"
EVENT_TRANSCRIPT = SIGNAL_TRANSCRIPT
EVENT_CALL_INCOMING = "jarvis_call_incoming"
EVENT_CALL_ENDED = "jarvis_call_ended"

STATUS_INTERVAL_SECONDS = 20
MICROPHONE_INTERVAL_SECONDS = 15

SERVICES = ("speak", "listen", "transcribe", "call", "hangup", "send_dtmf", "set_led", "gps_start", "probe")
