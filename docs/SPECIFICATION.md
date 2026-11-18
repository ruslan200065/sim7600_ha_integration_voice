# SIM7600 HA Integration — Vollständige Spezifikation

Dieses Dokument beschreibt alle Features, Anforderungen, Verhaltensregeln und technischen Details der Integration.
Es dient als verbindliche Grundlage für Neuentwicklung und Refactoring und folgt dem TDD-Ansatz:
**Tests werden auf Basis dieser Spezifikation definiert, bevor Code geschrieben wird.**

---

## 1. Projektüberblick

| Attribut | Wert |
|---|---|
| Name | SIM7600 4G Gateway |
| HA-Domain | `sim7600` |
| Version | 0.1 |
| Ziel | Offizielle Home Assistant Core Integration (HACS-kompatibel) |
| IoT-Klasse | `local_polling` |
| Hardware | Waveshare SIM7600X 4G HAT (SIM7600E-H, SIM7600G-H u. a.) |
| Schnittstelle | Seriell (USB/UART), typisch `/dev/ttyUSB2` oder `/dev/ttyS0` |
| USB-IDs | VID `1E0E`, PID `9001` |

**Kernziele:**
2. SMS-Versand und -Empfang für Alarmsysteme und Benachrichtigungen
3. Echtzeit-Netzwerkmonitoring (Signalstärke, Betreiber, Netzwerkmodus)
4. Geräte-Diagnostik (IMEI, Firmware, SIM-Status)

---

## 2. Systemarchitektur

### 2.1 Schichtmodell

```
┌─────────────────────────────────────────────────────────┐
│             Home Assistant Core                          │
│   (Services, Entity Registry, Config Entries, UI)        │
├──────────────┬──────────────────┬────────────────────────┤
│  sensor.py   │ notify.py        │  __init__.py            │
│  (12 Ent.)   │ (1 Entität)      │  (SMS-Service)          │
├──────────────┴──────────────────┴────────────────────────┤
│              coordinator.py                              │
│   SIM7600DataUpdateCoordinator                           │
│   - Polling (Netzwerk und SMS)                     │
│   - State-Persistenz: letzte bekannte Werte              │
│   - Fehler-isolierte Updates (kein kompletter Ausfall)   │
│   - Eigensteuerung: Retry und Caching      │
├─────────────────────────────────────────────────────────┤
│              modem.py                                    │
│   SIM7600Modem                                           │
│   - Async-Seriell (serial_asyncio_fast)                  │
│   - AT-Befehl-Serialisierung (asyncio.Lock)              │
│   - AT-Protokoll-Parsing         │
├─────────────────────────────────────────────────────────┤
│   Hardware: SIM7600 Modem (USB / UART)                   │
└─────────────────────────────────────────────────────────┘
```

### 2.2 Komponenten

| Datei | Klasse / Inhalt | Verantwortung |
|---|---|---|
| `__init__.py` | `async_setup_entry`, `async_unload_entry` | Integration-Lifecycle, SMS-Service registrieren |
| `const.py` | Konstanten | Domain, Config-Keys, Standardwerte |
| `config_flow.py` | `Sim7600ConfigFlow`, `Sim7600OptionsFlow` | UI-Konfig, USB-Discovery, nachträgliche Optionen |
| `coordinator.py` | `SIM7600DataUpdateCoordinator` | Polling, State-Persistenz, Eigensteuerung |
| `modem.py` | `SIM7600Modem` | AT-Kommunikation, Parsing |
| `sensor.py` | 12 Sensor-Klassen | HA-Sensor-Entitäten |
| `services.yaml` | — | HA-Service-Beschreibung |
| `strings.json` | — | UI-Texte |
| `translations/en.json` | — | Englische Übersetzungen |
| `manifest.json` | — | HA-Integrationsmetadata |

---

## 3. Konfiguration

### 3.1 Konfigurationsparameter

| Parameter | Key | Typ | Standard | Minimum | Beschreibung |
|---|---|---|---|---|---|
| Serieller Port | `serial_port` | `str` | — (Pflicht) | — | z. B. `/dev/ttyUSB2` |
| Baudrate | `baud_rate` | `int` | `115200` | — | Erlaubt: 9600, 19200, 38400, 57600, 115200 |
| Polling-Intervall | `polling_interval` | `int` | `60` s | `15` s | Netzwerk, SMS, Echtzeit-Daten |
| Debug-Modus | `debug_mode` | `bool` | `False` | — | Trace-Logging für AT-Rohdaten (Level 5) |

### 3.2 Options-Flow (nachträgliche Konfiguration)

Die folgenden Parameter müssen nach der Ersteinrichtung über einen **Options-Flow** änderbar sein:
- `polling_interval`
- `debug_mode`

### 3.3 Konfigurationsflow

**Manuell (UI):**
1. Nutzer wählt seriellen Port aus dynamisch ermittelter Liste
2. Nutzer wählt Baudrate, optionale Polling-Intervalle und Debug-Modus
3. Verbindungsvalidierung via `serial.Serial(port, baud, timeout=1).close()`
4. Bei Fehler: Fehlermeldung `cannot_connect`

**Automatisch (USB-Discovery):**
1. HA erkennt USB-Gerät mit VID `1E0E` / PID `9001`
2. Bestätigungsdialog für Nutzer
3. **Konfiguration mit vollständigem Formular:** Alle Parameter (inkl. Intervalle und Debug) werden abgefragt, nicht nur Standardwerte verwendet.

---

## 4. Entitätenkatalog

### 4.1 Sensor-Plattform (`sensor`)

Alle Sensoren gehören zum Gerät **"SIM7600 Modem"** (Hersteller: SimTech, Modell: SIM7600 Series).

| Entität | Klasse | Einheit | Device Class | State Class | Kategorie |
|---|---|---|---|---|---|
| Signal Strength | `SIM7600SignalSensor` | dBm | `signal_strength` | `measurement` | — |
| Operator | `SIM7600OperatorSensor` | — | — | — | — |
| Network Mode | `SIM7600NetworkModeSensor` | — | — | — | — |
| System Mode | `SIM7600SystemModeSensor` | — | — | — | — |
| IMEI | `SIM7600IMEISensor` | — | — | — | `diagnostic` |
| Firmware Version | `SIM7600FirmwareSensor` | — | — | — | `diagnostic` |
| SIM Status | `SIM7600SIMStatusSensor` | — | — | — | `diagnostic` |
| Last SMS | `SIM7600LastSMSSensor` | — | — | — | — |
| Speed | `SIM7600SpeedSensor` | km/h | `speed` | `measurement` | — |
| Altitude | `SIM7600AltitudeSensor` | m | `distance` | `measurement` | — |

**Zusatzattribute:**
- `Last SMS`: `sender` (str), `timestamp` (str)

## 5. Services

### `sim7600.send_sms`

| Parameter | Typ | Pflicht | Beschreibung |
|---|---|---|---|
| `number` | `string` | ja | Empfänger-Rufnummer (z. B. `+49123456789`) |
| `message` | `string` | ja | Nachrichtentext |

**AT-Ablauf:**
1. `AT+CMGF=1` — Text-Modus
2. `AT+CMGS="<number>"` — Versand initiieren
3. Warten auf `>` (Prompt, Timeout 5 s)
4. Nachrichtentext + `\x1A` (Ctrl+Z)
5. Warten auf `OK` oder `ERROR` (Timeout 10 s)

---

## 6. Verhaltensanforderungen (Eigensteuerung des Coordinators)

Dieser Abschnitt definiert das autonome Verhalten der Integration — wie sie Fehler behandelt, wann sie was abruft und wie sie Zustände persistiert.

### 6.1 State-Persistenz — Beibehaltung letzter bekannter Werte

**Anforderung:** Schlägt ein Polling-Zyklus fehl oder liefert ein AT-Befehl `None` zurück, **dürfen bestehende Sensorwerte nicht auf `unknown` gesetzt werden**, sofern der Wert sinnvoll beibehalten werden kann.

| Datenkategorie | Verhalten bei Fehler |
|---|---|
| RSSI / Signal | Letzter bekannter Wert bleibt. Wird auf `None` gesetzt nur wenn Connection-Fehler dauerhaft (d. h. Exception, nicht nur `None`-Antwort). |
| Operator / Network Mode / System Mode | Letzter bekannter Wert bleibt, bis neuer Wert verfügbar. |
| IMEI / Firmware / Manufacturer / Model | Einmalig abgerufen, danach permanent gecacht. Nie `None` nach erstem Erfolg. |
| SIM Status | Letzter bekannter Wert bleibt. |
| SMS | `last_sms` bleibt auf letzter empfangener Nachricht bis neue eintrifft. |

**Implementierungsmuster im Coordinator:**
- Der Coordinator hält `_last_signal_dbm: float | None` etc. als Instanz-Attribute.
- In `_async_update_data()`: Wenn ein neuer Wert `None` ist und ein alter Wert existiert, wird der alte Wert ins Result-Dict übernommen.
- Nur wenn die gesamte Verbindung fehlschlägt (Exception), wird `UpdateFailed` geworfen.

### 6.2 Polling-Strategie

**Zwei Polling-Ebenen:**

| Ebene | Intervall | Abgerufene Daten |
|---|---|---|
| Netzwerk (schnell) | `polling_interval` (Standard 60 s, min. 15 s) | RSSI, Operator, Network Mode, System Mode, Reg-Status, GPRS-Status, SMS |


### 6.4 SMS-Verwaltung

- Ungelesene SMS werden in jedem Netzwerk-Polling-Zyklus abgerufen (`AT+CMGL="REC UNREAD"`).
- Nach erfolgreichem Auslesen **müssen** SMS aus dem Modem-Speicher gelöscht werden (`AT+CMGD=<index>,0`), um Speicherüberlauf zu verhindern.
- `last_sms` wird nur überschrieben wenn neue Nachricht eintrifft. Alte Nachricht bleibt erhalten.

### 6.5 Retry-Verhalten

- Bei AT-Timeout oder `ERROR`-Antwort: Einzelner Retry nach 500 ms für kritische Befehle (SMS-Versand).
- Nicht-kritische Abfragen (RSSI, Operator etc.): kein Retry im gleichen Zyklus, Retry im nächsten Polling-Zyklus.
- Bei dauerhafter Verbindungslosigkeit (>3 aufeinanderfolgende `UpdateFailed`): Meldung im HA-Log auf WARNING-Level.

### 6.6 Statische Daten (einmaliger Abruf)

Folgende Daten werden nur einmalig beim ersten erfolgreichen Update-Zyklus abgerufen und dauerhaft gecacht:
- IMEI (`AT+CGSN`)
- Firmware (`AT+CGMR`)
- Hersteller (`AT+CGMI`)
- Modell (`AT+CGMM`)

Caching-Bedingung: Wert wird nur dann als gecacht betrachtet, wenn er nicht `None` ist.

---

## 7. Modem-Kommunikationsschicht

### 7.1 Verbindungsmanagement

- Lazy Connect: Verbindung wird beim ersten `send_command`-Aufruf aufgebaut.
- `asyncio.Lock` serialisiert alle AT-Befehlssequenzen.
- Disconnect: Writer schließen, Reader/Writer auf `None` setzen.

### 7.2 AT-Befehlsprotokoll

```
SENDEN:  f"{command}\r\n".encode()
EMPFANG: readline() bis "OK" oder "ERROR" (Default-Timeout: 5 s)
```

- Leere Zeilen überspringen.
- Debug-Modus: Trace-Log (Level 5) aller Rohdaten.
- Timeout → Warning, leere Liste zurück.

### 7.3 Implementierte AT-Befehle

| Methode | AT-Befehl | Rückgabe |
|---|---|---|
| `get_signal_quality()` | `AT+CSQ` | `int \| None` (None bei 99) |
| `get_operator()` | `AT+COPS?` | `str \| None` |
| `get_network_info()` | `AT+CPSI?` | `dict[str,str]` mit `mode`, `system_mode`, `mcc_mnc` |
| `get_imei()` | `AT+CGSN` | `str \| None` |
| `get_firmware()` | `AT+CGMR` | `str \| None` |
| `get_manufacturer()` | `AT+CGMI` | `str \| None` |
| `get_model()` | `AT+CGMM` | `str \| None` |
| `get_sim_status()` | `AT+CPIN?` | `str \| None` (z. B. `"READY"`) |
| `get_registration_status()` | `AT+CREG?` | `int \| None` |
| `get_gprs_registration_status()` | `AT+CGREG?` | `int \| None` |
| `send_sms(number, msg)` | `AT+CMGF=1`, `AT+CMGS` | `bool` |
| `get_unread_sms()` | `AT+CMGF=1`, `AT+CMGL="REC UNREAD"` | `list[SmsData]` |
| `delete_sms(index)` | `AT+CMGD=<index>,0` | `bool` |

### 7.5 SMS-Datenmodell (`SmsData`)

```python
@dataclass
class SmsData:
    index: int       # Speicher-Index für AT+CMGD
    sender: str      # Rufnummer des Absenders
    timestamp: str   # Format: "YY/MM/DD,HH:MM:SS+TZ"
    message: str     # Nachrichtentext
```

---

## 8. Coordinator — Vollständiges Datenobjekt

```python
@dataclass
class CoordinatorData:
    # Netzwerk
    rssi: int | None              # 0–31 (Rohwert)
    signal_dbm: float | None      # -113 + rssi * 2
    operator: str | None
    network_mode: str | None      # z. B. "LTE", "GSM", "WCDMA"
    system_mode: str | None       # z. B. "Online", "Limited service"
    reg_status: int | None        # 1=registriert, 5=Roaming
    gprs_reg_status: int | None

    # Diagnostik (gecacht, nie None nach erstem Erfolg)
    imei: str | None
    firmware: str | None
    manufacturer: str | None
    model: str | None
    sim_status: str | None        # z. B. "READY", "SIM PIN"

    # SMS
    last_sms: SmsData | None      # letzte empfangene SMS

```

---

## 9. Nicht-funktionale Anforderungen

### 9.1 Asynchronität

- Alle Modem-I/O: async/await, niemals blockierend im HA-Event-Loop.
- Blockierende Calls (z. B. `serial.Serial` im Config-Flow): via `hass.async_add_executor_job`.

### 9.2 Fehlerbehandlung

| Situation | Verhalten |
|---|---|
| AT-Timeout | WARNING-Log, betroffene Methode gibt `None`/leere Liste zurück |
| Verbindungsfehler in `send_command` | Exception propagiert zum Coordinator |
| Exception in `_async_update_data` | `UpdateFailed` — HA markiert Entities als `unavailable` |
| Partieller Fehler (einzelner AT-Befehl) | Letzter bekannter Wert bleibt, kein `UpdateFailed` |
| SMS-Versand fehlgeschlagen | `False` zurück, kein Exception-Raise, ERROR-Log |

### 9.3 Logging

| Level | Inhalt |
|---|---|
| TRACE (5) | Rohe AT-Zeilen (nur Debug-Modus) |
| DEBUG | Gesendeter Befehl, komplette Antwort-Liste |
| INFO | SMS empfangen |
| WARNING | AT-Timeout, wiederholte Update-Fehler |
| ERROR | Verbindungsfehler, ungültige Service-Parameter, SMS-Versand fehlgeschlagen |

### 9.4 Typsicherheit

- Strict typing für alle Module (`mypy --strict` kompatibel).
- SMS-Daten als `@dataclass`, nicht als `dict[str, str]`.

### 9.5 Code-Qualität

- Linter: `ruff` (E, F, I, B, C4, UP, ASYNC)
- Formatter: `ruff format`
- Python: 3.12+

---

## 10. Abhängigkeiten

### 10.1 Laufzeit

| Paket | Zweck |
|---|---|
| `pyserial-asyncio-fast` | Async serielle Verbindung |
| `homeassistant` (Core) | HA-Framework |
| `voluptuous` | Config-Schema-Validierung |

### 10.2 Test-Dependencies

| Paket | Zweck |
|---|---|
| `pytest` | Test-Framework |
| `pytest-asyncio` | Async-Tests |
| `pytest-homeassistant-custom-component` | HA-Testfixtures |

---

## 11. Test-Spezifikation (TDD)

Tests werden **vor der Implementierung** definiert und dienen als ausführbare Spezifikation.

### 11.1 `test_modem.py` — Modem-Unit-Tests

| Test | Beschreibung | Erwartetes Ergebnis |
|---|---|---|
| `test_get_signal_quality_valid` | `+CSQ: 20,0` | `20` |
| `test_get_signal_quality_unknown` | `+CSQ: 99,0` | `None` |
| `test_get_signal_quality_no_response` | Leere Antwort | `None` |
| `test_get_operator_valid` | `+COPS: 0,0,"Telekom.de",7` | `"Telekom.de"` |
| `test_get_operator_no_service` | `+COPS: 0` | `None` |
| `test_get_sim_status_ready` | `+CPIN: READY` | `"READY"` |
| `test_get_sim_status_pin` | `+CPIN: SIM PIN` | `"SIM PIN"` |
| `test_get_unread_sms_single` | Eine ungelesene SMS | Liste mit einem `SmsData`-Objekt inkl. Index |
| `test_get_unread_sms_multiple` | Zwei ungelesene SMS | Liste mit zwei Einträgen |
| `test_get_unread_sms_empty` | Keine SMS | Leere Liste |
| `test_delete_sms_success` | `AT+CMGD=1,0` → `OK` | `True` |
| `test_delete_sms_error` | `AT+CMGD=1,0` → `ERROR` | `False` |
| `test_send_sms_success` | Vollständiger SMS-Handshake | `True` |
| `test_send_sms_timeout_on_prompt` | Kein `>` innerhalb Timeout | `False` |
| `test_send_sms_error_response` | `ERROR` nach `\x1A` | `False` |

### 11.2 `test_coordinator.py` — Coordinator-Tests

| Test | Beschreibung | Erwartetes Ergebnis |
|---|---|---|
| `test_update_success_full` | Alle Modem-Methoden liefern Werte | Vollständiges `CoordinatorData` |
| `test_signal_dbm_calculation` | `rssi=20` | `signal_dbm=-73` |
| `test_static_data_cached` | Zweiter Update-Aufruf | IMEI/Firmware-Methoden nur 1× aufgerufen |
| `test_state_persistence_signal_on_none_response` | `get_signal_quality` gibt `None` | Letzter `signal_dbm`-Wert bleibt |
| `test_no_update_failed_on_partial_error` | Einzelner AT-Befehl schlägt fehl | `UpdateFailed` wird **nicht** geworfen |
| `test_update_failed_on_connection_error` | Exception in `get_signal_quality` | `UpdateFailed` wird geworfen |
| `test_sms_deleted_after_reading` | Neue SMS empfangen | `delete_sms` mit korrektem Index aufgerufen |
| `test_sim_status_in_data` | `get_sim_status` gibt `"READY"` zurück | `data["sim_status"] == "READY"` |

### 11.4 `test_sensor.py` — Sensor-Tests (erweitert)

| Test | Beschreibung |
|---|---|
| `test_signal_sensor_value` | Korrekte dBm-Ausgabe |
| `test_signal_sensor_persisted_value` | `None` in data → alter Wert bleibt |
| `test_sim_status_sensor_ready` | `"READY"` korrekt dargestellt |
| `test_last_sms_attributes` | `sender` und `timestamp` als Attribute |

### 11.6 `test_config_flow.py` — Config Flow-Tests

| Test | Bestehend | Erweiterung |
|---|---|---|
| `test_flow_init` | ✅ | — |
| `test_user_flow_success` | ✅ | Prüfung aller Konfigfelder inkl. Intervalle |
| `test_user_flow_cannot_connect` | ✅ | — |
| `test_usb_discovery` | ✅ | Prüfung: Discovery-Flow fragt Intervall-Parameter ab |
| `test_options_flow_changes_interval` | ❌ | Neu: Options-Flow ändert `polling_interval` |

### 11.7 `test_sms.py` — SMS-Tests (erweitert)

| Test | Beschreibung |
|---|---|
| `test_sms_index_extracted` | Index aus `+CMGL`-Zeile korrekt geparst |
| `test_multiple_sms_indices` | Alle Indizes korrekt |

---

## 12. CI/CD-Pipeline

**Trigger:** Push auf `main`, `develop`, `feature/*`; PR auf `main`, `develop`

| Stage | Tool | Konfiguration |
|---|---|---|
| Lint | `ruff check .` | `pyproject.toml` |
| Format | `ruff format --check .` | `pyproject.toml` |
| Typ-Check | `mypy custom_components/sim7600` | `.mypy.ini` |
| Tests | `pytest --import-mode=importlib` | `pyproject.toml` |
| Snapshot Release | `softprops/action-gh-release@v2` | nur `develop`/`feature/*` |

**Snapshot-Schema:** `<version>-dev.<short_sha>` (z. B. `0.1-dev.08ccabd`)

**Git-Flow:**
- `main` — Produktion
- `develop` — Integration
- `feature/*` — Features
- `release/*` — Release-Vorbereitung
- `hotfix/*` — Notfall-Patches

**Lokale CI-Parität (vor jedem Push):**
```bash
source venv/bin/activate && ruff check . && ruff format --check . && mypy custom_components/sim7600 && PYTHONPATH=. pytest
```

---

## 13. HACS-Kompatibilität

- `hacs.json` und `manifest.json` vorhanden.
- Installation via HACS Custom Repositories möglich.
- Repository: `https://github.com/tschoor/sim7600_ha_integration`

---

## 14. Behobene Defekte (gegenüber Ist-Zustand)

| # | Defekt | Lösung |
|---|---|---|
| 1 | `SIM7600SIMStatusSensor` liest nie befüllten Key | `get_sim_status()` in Modem implementieren (`AT+CPIN?`), im Coordinator aufrufen |
| 2 | USB-Discovery fragt keine optionalen Parameter ab | Discovery-Flow um vollständiges Konfigformular erweitern |
| 4 | Signal `None` bei Timeout → Sensor zeigt `unknown` | State-Persistenz: letzter Signal-Wert bleibt |
| 5 | SMS werden nicht gelöscht → Modem-Speicher läuft voll | `delete_sms(index)` nach Auslesen aufrufen |
| 6 | Kein Options-Flow → Intervall-Änderung nur via Neukonfiguration | `Sim7600OptionsFlow` implementieren |
| 9 | `get_chip_details()` implementiert aber nie aufgerufen | Entweder in Coordinator nutzen oder entfernen |

---

## 15. Referenz: AT-Befehlsquickref

Vollständige Referenz: `docs/AT_COMMAND_REFERENCE.md`

**Protokollregeln:**
1. Jeder Befehl endet mit `\r\n` (Hex `0D 0A`)
2. SMS-Prompt: Erst nach `> ` (Hex `3E 20`) Text senden
3. SMS-Abschluss: `\x1A` (Ctrl+Z, Hex `1A`)
4. Bei `ERROR`: 500 ms warten, einmal wiederholen; dann `None`/`False` zurückgeben
