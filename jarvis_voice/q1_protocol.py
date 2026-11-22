"""SIM7600 LED AT command protocol.

From opt/watch/signalq_modem.py in user-supplied Q1 archive.
Any AT commands are sent via the *same* Modem single serial-port owner.
Do not auto-write modem NVRAM (original used CNVW) or change carrier APN.
"""

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
# Configuration order follows the source Q1 commands. Output order is RGB.
GPIO_SETUP_ORDER = (3, 6, 41)
GPIO_RGB_CHANNELS = (41, 3, 6)


async def led_set(modem, color):
    if color not in LED_COLORS:
        raise ValueError("Allowed LED colors: " + ", ".join(LED_COLORS))
    for channel in GPIO_SETUP_ORDER:
        response = await modem.command(f"AT+CGDRT={channel},1")
        if not response or response[-1] != "OK":
            raise RuntimeError(f"LED GPIO {channel} mode rejected: {response}")
    for channel, level in zip(GPIO_RGB_CHANNELS, LED_COLORS[color], strict=True):
        response = await modem.command(f"AT+CGSETV={channel},{level}")
        if not response or response[-1] != "OK":
            raise RuntimeError(f"LED GPIO {channel} write rejected: {response}")
    return {"color": color}
