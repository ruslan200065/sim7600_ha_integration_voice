# SIM7600E-H AT Command Reference & Implementation Guide

This document defines the interface for communicating with the SIM7600 module using AT commands.

## 1. System- und Identifikationsbefehle

| AT-Befehl | Beschreibung | Syntax | Beispielantwort |
| :--- | :--- | :--- | :--- |
| **AT** | Testbefehl | `AT` | `OK` |
| **ATE1** / **ATE0** | Echo ein/aus | `ATE<n>` | `OK` |
| **AT+CGMI** | Hersteller | `AT+CGMI` | `SIMCOM_Ltd
OK` |
| **AT+CGMM** | Modell | `AT+CGMM` | `SIM7600E-H
OK` |
| **AT+CGSN** | IMEI | `AT+CGSN` | `866123041234567
OK` |
| **AT+CSUB** | Version/Chip | `AT+CSUB` | `SIM7600E-H
OK` |
| **AT+CGMR** | Firmware | `AT+CGMR` | `SIM7600E-H_V1.0.0
OK` |
| **AT+IPREX** | Baudrate | `AT+IPREX=<n>` | `OK` |
| **AT+CRESET** | Soft-Reset | `AT+CRESET` | `OK` |

## 2. Netzwerk- und Signalsteuerung

| AT-Befehl | Beschreibung | Syntax | Beispielantwort |
| :--- | :--- | :--- | :--- |
| **AT+CSQ** | Signalqualität | `AT+CSQ` | `+CSQ: 25,0
OK` |
| **AT+CPIN?** | SIM Status | `AT+CPIN?` | `+CPIN: READY
OK` |
| **AT+COPS?** | Betreiber | `AT+COPS?` | `+COPS: 0,0,"Telekom.de",7
OK` |
| **AT+CREG?** | Netz-Reg. | `AT+CREG?` | `+CREG: 0,1
OK` |
| **AT+CGREG?** | GPRS-Reg. | `AT+CGREG?` | `+CGREG: 0,1
OK` |
| **AT+CPSI?** | System-Info | `AT+CPSI?` | `+CPSI: LTE,Online,262-01,0x1234,12345,71,EUTRAN-BAND3,1300,10,10,-85,-10,20,0,0
OK` |

## 3. SMS-Kommunikation

| AT-Befehl | Beschreibung | Syntax | Beispielantwort |
| :--- | :--- | :--- | :--- |
| **AT+CMGF=1** | Text-Modus | `AT+CMGF=1` | `OK` |
| **AT+CMGS** | Senden | `AT+CMGS="<nr>"` | `> [Text]\x1A
+CMGS: 12
OK` |
| **AT+CMGR** | Lesen | `AT+CMGR=<n>` | `+CMGR: "REC READ","+1234567890",,"21/03/25,02:35:04+00"
Hello World
OK` |
| **AT+CMGL** | Listen | `AT+CMGL="ALL"` | `+CMGL: 1,"REC READ","+1234567890",,"21/03/25,02:35:04+00"
Msg1
OK` |

## Agentische Implementierungshinweise

### 1. Daten-Parsing-Muster
Die Antworten des Moduls folgen oft dem Muster:
```text
+BEFEHL: Wert1,Wert2,...
OK
```
Der Agent muss bei jedem Befehl prüfen, ob `OK` oder ein `ERROR` Code (z.B. `+CME ERROR: 10`) zurückgegeben wurde.

### 2. Kommandoabschluss
Jeder Befehl **muss** mit `
` (Hex `0D 0A`) abgeschlossen werden. Der Agent darf nicht auf ein line-end Zeichen der Umgebung vertrauen.

### 3. SMS-Prompt (`>`)
Beim Senden einer SMS (`AT+CMGS`) darf der Agent den Nachrichtentext erst senden, wenn das Modul mit dem Byte `>` (Hex `3E 20`) antwortet. Der Abschluss erfolgt zwingend mit `0x1A`.

### 4. Zustands-Validierung
Bei jedem `ERROR` oder Timeout muss der Agent:
1. Den Modem-Status mit `AT+CPIN?` prüfen.
2. Die Signalqualität mit `AT+CSQ` prüfen.
3. Ggf. den Befehl nach einer kurzen Pause (500ms) wiederholen.
