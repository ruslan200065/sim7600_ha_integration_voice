"""Tests für die SIM7600 Modem-Kommunikationsschicht."""

from unittest.mock import patch

import pytest
from pdu_helpers import make_deliver_pdu

from custom_components.sim7600.modem import SIM7600Modem
from custom_components.sim7600.types import GpsData, SmsData


@pytest.fixture
def modem() -> SIM7600Modem:
    """Erstellt eine Modem-Instanz für Tests."""
    return SIM7600Modem("/dev/ttyUSB2", 115200)


# --- Signalstärke ---


async def test_get_signal_quality_valid(modem: SIM7600Modem) -> None:
    """Gültige CSQ-Antwort liefert RSSI-Wert."""
    with patch.object(modem, "send_command", return_value=["+CSQ: 20,0", "OK"]):
        result = await modem.get_signal_quality()
    assert result == 20


async def test_get_signal_quality_unknown(modem: SIM7600Modem) -> None:
    """RSSI 99 (kein Signal) wird als None zurückgegeben."""
    with patch.object(modem, "send_command", return_value=["+CSQ: 99,0", "OK"]):
        result = await modem.get_signal_quality()
    assert result is None


async def test_get_signal_quality_no_response(modem: SIM7600Modem) -> None:
    """Leere Antwort liefert None."""
    with patch.object(modem, "send_command", return_value=["OK"]):
        result = await modem.get_signal_quality()
    assert result is None


# --- SIM-Status ---


async def test_get_sim_status_ready(modem: SIM7600Modem) -> None:
    """SIM bereit: gibt 'READY' zurück."""
    with patch.object(modem, "send_command", return_value=["+CPIN: READY", "OK"]):
        result = await modem.get_sim_status()
    assert result == "READY"


async def test_get_sim_status_pin(modem: SIM7600Modem) -> None:
    """SIM wartet auf PIN: gibt 'SIM PIN' zurück."""
    with patch.object(modem, "send_command", return_value=["+CPIN: SIM PIN", "OK"]):
        result = await modem.get_sim_status()
    assert result == "SIM PIN"


async def test_get_sim_status_no_response(modem: SIM7600Modem) -> None:
    """Keine CPIN-Zeile → None."""
    with patch.object(modem, "send_command", return_value=["OK"]):
        result = await modem.get_sim_status()
    assert result is None


# --- GPS-Informationen ---


async def test_get_gps_info_all_fields_present(modem: SIM7600Modem) -> None:
    """Vollständige GPS-Antwort → GpsData-Objekt mit allen 6 Feldern."""
    line = "+CGPSINFO: 5231.450000,N,01324.550000,E,250321,023504.0,10.0,1.5,0.0"
    with patch.object(modem, "send_command", return_value=[line, "OK"]):
        result = await modem.get_gps_info()
    assert isinstance(result, GpsData)
    assert result.altitude == 10.0
    assert result.speed == 1.5
    assert result.date == "250321"
    assert result.time == "023504.0"


async def test_get_gps_info_northern_eastern(modem: SIM7600Modem) -> None:
    """Nord/Ost-Koordinaten korrekt in Dezimalgrad umgerechnet."""
    line = "+CGPSINFO: 5231.450000,N,01324.550000,E,250321,023504.0,10.0,0.0,0.0"
    with patch.object(modem, "send_command", return_value=[line, "OK"]):
        result = await modem.get_gps_info()
    assert result is not None
    assert result.latitude == pytest.approx(52.524166666, rel=1e-5)
    assert result.longitude == pytest.approx(13.409166666, rel=1e-5)


async def test_get_gps_info_southern_western(modem: SIM7600Modem) -> None:
    """Süd/West-Koordinaten haben negative Vorzeichen."""
    line = "+CGPSINFO: 3113.343286,S,12121.259046,W,250321,023504.0,45.0,0.0,0.0"
    with patch.object(modem, "send_command", return_value=[line, "OK"]):
        result = await modem.get_gps_info()
    assert result is not None
    assert result.latitude < 0
    assert result.longitude < 0


async def test_get_gps_info_empty(modem: SIM7600Modem) -> None:
    """Leere GPS-Antwort (kein Fix) → None."""
    with patch.object(
        modem, "send_command", return_value=["+CGPSINFO: ,,,,,,,,", "OK"]
    ):
        result = await modem.get_gps_info()
    assert result is None


async def test_get_gps_info_missing_speed(modem: SIM7600Modem) -> None:
    """Fehlende Geschwindigkeit → None (alle Felder verpflichtend)."""
    line = "+CGPSINFO: 5231.450000,N,01324.550000,E,250321,023504.0,10.0,,0.0"
    with patch.object(modem, "send_command", return_value=[line, "OK"]):
        result = await modem.get_gps_info()
    assert result is None


async def test_get_gps_info_missing_altitude(modem: SIM7600Modem) -> None:
    """Fehlende Höhe → None."""
    line = "+CGPSINFO: 5231.450000,N,01324.550000,E,250321,023504.0,,0.0,0.0"
    with patch.object(modem, "send_command", return_value=[line, "OK"]):
        result = await modem.get_gps_info()
    assert result is None


async def test_get_gps_info_missing_date(modem: SIM7600Modem) -> None:
    """Fehlende Datum → None."""
    line = "+CGPSINFO: 5231.450000,N,01324.550000,E,,023504.0,10.0,0.0,0.0"
    with patch.object(modem, "send_command", return_value=[line, "OK"]):
        result = await modem.get_gps_info()
    assert result is None


async def test_get_gps_info_missing_time(modem: SIM7600Modem) -> None:
    """Fehlende Uhrzeit → None."""
    line = "+CGPSINFO: 5231.450000,N,01324.550000,E,250321,,10.0,0.0,0.0"
    with patch.object(modem, "send_command", return_value=[line, "OK"]):
        result = await modem.get_gps_info()
    assert result is None


# --- SMS-Empfang ---


async def test_get_unread_sms_includes_index(modem: SIM7600Modem) -> None:
    """SMS-Index aus +CMGL-Zeile wird in SmsData.index gespeichert."""
    with patch.object(
        modem,
        "send_command",
        side_effect=[
            ["OK"],
            ["+CMGL: 3,0,,20", make_deliver_pdu("+1234567890", "Hello"), "OK"],
        ],
    ):
        messages = await modem.get_unread_sms()
    assert len(messages) == 1
    assert isinstance(messages[0], SmsData)
    assert messages[0].index == 3
    assert messages[0].sender == "+1234567890"
    assert messages[0].message == "Hello"


async def test_get_unread_sms_multiple_messages(modem: SIM7600Modem) -> None:
    """Mehrere ungelesene SMS mit korrekten Indizes."""
    with patch.object(
        modem,
        "send_command",
        side_effect=[
            ["OK"],
            [
                "+CMGL: 1,0,,20",
                make_deliver_pdu("+111", "First"),
                "+CMGL: 5,0,,20",
                make_deliver_pdu("+222", "Second"),
                "OK",
            ],
        ],
    ):
        messages = await modem.get_unread_sms()
    assert len(messages) == 2
    assert messages[0].index == 1
    assert messages[0].message == "First"
    assert messages[1].index == 5
    assert messages[1].message == "Second"


async def test_get_unread_sms_empty(modem: SIM7600Modem) -> None:
    """Keine ungelesenen SMS → leere Liste."""
    with patch.object(
        modem,
        "send_command",
        side_effect=[["OK"], ["OK"]],
    ):
        messages = await modem.get_unread_sms()
    assert messages == []


# --- SMS-Löschung ---


async def test_delete_sms_success(modem: SIM7600Modem) -> None:
    """Erfolgreiche SMS-Löschung gibt True zurück."""
    with patch.object(modem, "send_command", return_value=["OK"]):
        result = await modem.delete_sms(1)
    assert result is True


async def test_delete_sms_error(modem: SIM7600Modem) -> None:
    """Fehler bei SMS-Löschung gibt False zurück."""
    with patch.object(modem, "send_command", return_value=["ERROR"]):
        result = await modem.delete_sms(1)
    assert result is False


async def test_delete_sms_sends_correct_command(modem: SIM7600Modem) -> None:
    """Korrekter AT-Befehl AT+CMGD=<index>,0 wird gesendet."""
    with patch.object(modem, "send_command", return_value=["OK"]) as mock_cmd:
        await modem.delete_sms(7)
    mock_cmd.assert_called_once_with("AT+CMGD=7,0")


# --- GPS-Aktivierung ---


async def test_set_gps_success(modem: SIM7600Modem) -> None:
    """AT+CGPS=1 mit OK-Antwort → True."""
    with patch.object(modem, "send_command", return_value=["OK"]):
        result = await modem.set_gps(True)
    assert result is True


async def test_set_gps_retries_on_timeout(modem: SIM7600Modem) -> None:
    """Kein OK beim ersten Versuch → 500ms warten und einmalig wiederholen."""
    call_count = 0

    async def mock_send(cmd: str, **kwargs: object) -> list[str]:
        nonlocal call_count
        call_count += 1
        return ["OK"] if call_count > 1 else []

    with patch.object(modem, "send_command", side_effect=mock_send):
        with patch("custom_components.sim7600.modem.asyncio.sleep") as mock_sleep:
            result = await modem.set_gps(True)

    assert call_count == 2
    mock_sleep.assert_called_once_with(0.5)
    assert result is True


async def test_set_gps_error_treated_as_already_enabled(modem: SIM7600Modem) -> None:
    """ERROR (GPS evtl. schon aktiv) → True ohne Retry."""
    with patch.object(modem, "send_command", return_value=["ERROR"]):
        result = await modem.set_gps(True)
    assert result is True


# --- Operator ---


async def test_get_operator_valid(modem: SIM7600Modem) -> None:
    """Gültige COPS-Antwort liefert Betreibernamen."""
    with patch.object(
        modem, "send_command", return_value=['+COPS: 0,0,"Telekom.de",7', "OK"]
    ):
        result = await modem.get_operator()
    assert result == "Telekom.de"


async def test_get_operator_no_service(modem: SIM7600Modem) -> None:
    """COPS ohne Betreiber (kein Netz) → None."""
    with patch.object(modem, "send_command", return_value=["+COPS: 0", "OK"]):
        result = await modem.get_operator()
    assert result is None


# SMS sending (success, error, prompt timeout + ESC) is covered end-to-end
# with a fake serial port in test_modem_protocol.py.
