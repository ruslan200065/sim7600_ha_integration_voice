"""Parse local family profiles and retain the legacy owner configuration."""
import hashlib
import re

NUMBER = re.compile(r"\+?[1-9][0-9]{7,14}")
ENTITY = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")
GATE_ACTIONS = {"cover": "open_cover", "lock": "unlock", "button": "press", "script": "turn_on"}


def profile_key(name, number):
    return hashlib.sha256(f"{name.casefold()}:{number.lstrip('+')}".encode()).hexdigest()


def family_profiles(lines):
    if not isinstance(lines, list):
        raise ValueError("Укажите список членов семьи.")
    profiles = {}
    names = set()
    for line in lines:
        if not isinstance(line, str):
            raise ValueError("Неверная строка профиля.")
        parts = [part.strip() for part in line.split("|")]
        if not 2 <= len(parts) <= 6:
            raise ValueError("Формат профиля: имя | номер | устройства | калитка | календарь | person.")
        name, number, entities, gate, calendar, person = (parts + [""] * 6)[:6]
        entities = entities.replace(",", " ").split()
        if (not name or len(name) > 60 or not name.isprintable() or not NUMBER.fullmatch(number)
            or any(not ENTITY.fullmatch(item) for item in entities)
            or (gate and (not ENTITY.fullmatch(gate) or gate.split(".")[0] not in GATE_ACTIONS))):
            raise ValueError("Проверьте имя, номер и идентификаторы устройств семьи.")
        if calendar and (not ENTITY.fullmatch(calendar) or not calendar.startswith("calendar.")):
            raise ValueError("Укажите календарь участника вида calendar.name.")
        if person and (not ENTITY.fullmatch(person) or not person.startswith("person.")):
            raise ValueError("Укажите сущность участника вида person.name.")
        number = number.lstrip("+")
        if number in profiles or name.casefold() in names:
            raise ValueError("Имена и номера членов семьи должны быть уникальны.")
        names.add(name.casefold())
        profiles[number] = {"name": name, "number": "+" + number, "entities": entities,
                            "gate": gate, "calendar": calendar, "person": person, "key": profile_key(name, number)}
    return profiles
