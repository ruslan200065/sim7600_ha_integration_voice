"""SIM7600 GPS and LED AT command protocol.

From opt/misc/gps.py and opt/watch/signalq_modem.py in user-supplied Q1 archive.
Any AT commands are sent via the *same* Modem single serial-port owner.
Do not auto-write modem NVRAM (original used CNVW) or change carrier APN.
"""
import re

LED_COLORS = {
    "off": (0, 0, 0),
    "white": (1, 1, 1),
    "green": (0, 1, 0),
    "red": (1, 0, 0),
    "blue": (0, 0, 1),
    "pink": (1, 0, 1),
    "yellow": (1, 1, 0),
    "cyan": (0, 1, 1),
}


def gps_coordinate(value, hemisphere, latitude=True):
    """SIM7600 +CGPSINFO ddmm.mmm / dddmm.mmm to decimal degrees."""
    max_deg = 90 if latitude else 180
    if hemisphere not in (("N", "S") if latitude else ("E", "W")):
        raise ValueError("Invalid hemisphere")
    degree_digits = 2 if latitude else 3
    if not re.fullmatch(r"\d{"+str(degree_digits+2)+r"}\.\d+", value):
        raise ValueError("Invalid GPS coordinate format")
    degrees = int(value[:degree_digits])
    minutes = float(value[degree_digits:])
    if degrees > max_deg or minutes >= 60 or (degrees == max_deg and minutes != 0):
        raise ValueError("GPS coordinate out of range")
    signed = degrees + minutes / 60.0
    if hemisphere in ("S","W"):
        signed = -signed
    return round(signed, 7)


def parse_gpsinfo(lines):
    for line in lines:
        if not line.startswith("+CGPSINFO:"):
            continue
        fields = [x.strip() for x in line.partition(":")[2].split(",")]
        if len(fields) < 4 or not fields[0] or not fields[2]:
            return {"fix": False}
        try:
            data = {"fix": True,
                    "latitude": gps_coordinate(fields[0], fields[1], True),
                    "longitude": gps_coordinate(fields[2], fields[3], False)}
            if len(fields) > 4:
                data["date_utc"] = fields[4] or None
            if len(fields) > 5:
                data["time_utc"] = fields[5] or None
            if len(fields) > 6 and fields[6]:
                data["altitude_m"] = float(fields[6])
            if len(fields) > 7 and fields[7]:
                data["speed_knots"] = float(fields[7])
            return data
        except (ValueError, IndexError):
            return {"fix": False, "error": "invalid_gpsinfo"}
    return {"fix": False, "error": "gpsinfo_missing"}


async def gps_status(modem):
    """Read GPS without enabling it or altering cellular data connection."""
    status = await modem.command("AT+CGPS?", timeout=5)
    position = await modem.command("AT+CGPSINFO", timeout=7)
    data = parse_gpsinfo(position)
    data["modem_gps_status"] = next((x for x in status if x.startswith("+CGPS:")), "unknown")
    return data


async def gps_start(modem, assisted=False):
    """Use original Q1 GPSAUTO and optional assisted mode, no NVRAM/APN writes."""
    out = {}
    status = await modem.command("AT+CGPSAUTO?")
    if "+CGPSAUTO: 0" in status:
        out["gps_auto"] = await modem.command("AT+CGPSAUTO=1")
    else:
        out["gps_auto"] = status
    state = await modem.command("AT+CGPS?")
    if any(x.startswith("+CGPS: 1,") for x in state):
        out["gps_active"] = True
    else:
        # Q1 software used CGPS=1,2 for AGPS; 1,1 for standalone GPS.
        result = await modem.command("AT+CGPS=1,2" if assisted else "AT+CGPS=1,1", timeout=10)
        out["start_result"] = result
        out["gps_active"] = bool(result and result[-1] == "OK")
    return out


async def led_set(modem, color):
    if color not in LED_COLORS:
        raise ValueError("Allowed LED colors: " + ", ".join(LED_COLORS))
    values = LED_COLORS[color]
    # Confirmed by original /opt/watch/signalq_modem.py.
    for channel in (3,6,41):
        response = await modem.command(f"AT+CGDRT={channel},1")
        if not response or response[-1] != "OK":
            raise RuntimeError(f"LED GPIO {channel} mode rejected: {response}")
    for channel, level in ((41,values[0]),(3,values[1]),(6,values[2])):
        response = await modem.command(f"AT+CGSETV={channel},{level}")
        if not response or response[-1] != "OK":
            raise RuntimeError(f"LED GPIO {channel} write rejected: {response}")
    return {"color": color}
