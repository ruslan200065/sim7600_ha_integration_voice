"""Constants for the SIM7600 integration."""

from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

DOMAIN = "sim7600"
MANUFACTURER = "SIMCom"

CONF_SERIAL_PORT = "serial_port"
CONF_BAUD_RATE = "baud_rate"
CONF_POLLING_INTERVAL = "polling_interval"
CONF_GNSS_INTERVAL = "gnss_interval"
CONF_DEBUG_MODE = "debug_mode"
CONF_DEFAULT_RECIPIENT = "default_recipient"
CONF_BALANCE_USSD = "balance_ussd"
CONF_BALANCE_INTERVAL = "balance_interval"
CONF_MANUAL_PATH = "manual_path"

DEFAULT_BAUD = 115200
DEFAULT_POLLING_INTERVAL = 60  # seconds
DEFAULT_GNSS_INTERVAL = 300  # seconds
DEFAULT_BALANCE_INTERVAL = 12  # hours

GPS_STALE_AFTER = 600  # seconds without fix -> location unknown
SMS_CLEANUP_INTERVAL = 3600  # seconds between AT+CMGD=1,1
NO_REGISTRATION_ISSUE_AFTER = 1800  # seconds without network registration

EVENT_SMS_RECEIVED = "sim7600_sms_received"
EVENT_INCOMING_CALL = "sim7600_incoming_call"

SERVICE_SEND_SMS = "send_sms"
SERVICE_SEND_USSD = "send_ussd"
ATTR_NUMBER = "number"
ATTR_MESSAGE = "message"
ATTR_CODE = "code"
ATTR_ENTRY_ID = "entry_id"
ATTR_DEVICE_ID = "device_id"

ISSUE_SIM_PIN = "sim_pin_required"
ISSUE_NO_REGISTRATION = "no_network_registration"

MAX_STATE_LENGTH = 255

# 3GPP TS 27.007 +CREG/+CGREG <stat> values.
REG_STATUS = {
    0: "not_registered",
    1: "home",
    2: "searching",
    3: "denied",
    4: "unknown",
    5: "roaming",
    6: "home_sms_only",
    7: "roaming_sms_only",
    8: "emergency_only",
    9: "home_csfb_not_preferred",
    10: "roaming_csfb_not_preferred",
    11: "emergency_only",
}
REG_STATUS_OPTIONS = sorted(set(REG_STATUS.values()))
REGISTERED_STATUSES = {1, 5, 6, 7, 9, 10}
