"""Shared request-body and phone-number normalization."""

import re

from aiohttp import web


def phone_key(number, country="7"):
    """Normalise caller numbers so 8XXXXXXXXXX, +7XXXXXXXXXX and 7XXXXXXXXXX compare equal."""
    digits = re.sub(r"[^0-9]", "", number or "")
    if country == "7" and len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif country and len(digits) == 10 and not digits.startswith(country):
        digits = country + digits
    return digits


async def read_json(request):
    """Parse a JSON object body or answer 400 instead of a 500 traceback."""
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise web.HTTPBadRequest(text="Body must be JSON") from exc
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="Body must be a JSON object")
    return data
