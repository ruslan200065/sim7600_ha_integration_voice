"""Tests for SMS parsing in the SIM7600 modem (PDU mode, AT+CMGL=4)."""

from unittest.mock import patch

from pdu_helpers import DEFAULT_TS_ISO, make_deliver_pdu

from custom_components.sim7600.modem import SIM7600Modem
from custom_components.sim7600.types import SmsData


async def test_get_unread_sms_parsing() -> None:
    """All SMS fields are extracted from an AT+CMGL=4 response."""
    modem = SIM7600Modem("/dev/ttyUSB2", 115200)
    mock_responses = [
        ["OK"],
        [
            "+CMGL: 1,0,,30",
            make_deliver_pdu("+1234567890", "Hello from SIM7600!"),
            "OK",
        ],
    ]

    with patch.object(modem, "send_command", side_effect=mock_responses):
        messages = await modem.get_unread_sms()
    assert len(messages) == 1
    assert isinstance(messages[0], SmsData)
    assert messages[0].sender == "+1234567890"
    assert messages[0].message == "Hello from SIM7600!"
    assert messages[0].timestamp == DEFAULT_TS_ISO


async def test_sms_index_extracted() -> None:
    """The storage index is taken from the +CMGL line."""
    modem = SIM7600Modem("/dev/ttyUSB2", 115200)
    mock_responses = [
        ["OK"],
        ["+CMGL: 7,0,,20", make_deliver_pdu("+1234567890", "Test"), "OK"],
    ]

    with patch.object(modem, "send_command", side_effect=mock_responses):
        messages = await modem.get_unread_sms()
    assert len(messages) == 1
    assert messages[0].index == 7
    assert messages[0].indices == (7,)


async def test_multiple_sms_all_indices() -> None:
    """Indices of several messages are all extracted."""
    modem = SIM7600Modem("/dev/ttyUSB2", 115200)
    mock_responses = [
        ["OK"],
        [
            "+CMGL: 2,0,,20",
            make_deliver_pdu("+111", "First"),
            "+CMGL: 9,0,,20",
            make_deliver_pdu("+222", "Second"),
            "OK",
        ],
    ]

    with patch.object(modem, "send_command", side_effect=mock_responses):
        messages = await modem.get_unread_sms()
    assert len(messages) == 2
    assert messages[0].index == 2
    assert messages[1].index == 9


async def test_read_messages_not_reported_again() -> None:
    """Status 1 (REC READ) single messages are not reported."""
    modem = SIM7600Modem("/dev/ttyUSB2", 115200)
    mock_responses = [
        ["OK"],
        ["+CMGL: 2,1,,20", make_deliver_pdu("+111", "Old"), "OK"],
    ]
    with patch.object(modem, "send_command", side_effect=mock_responses):
        assert await modem.get_unread_sms() == []
