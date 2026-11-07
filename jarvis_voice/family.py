"""Parse local family profiles and retain the legacy owner configuration."""
import hashlib
import re

NUMBER = re.compile(r"\+?[1-9][0-9]{7,14}")
ENTITY = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")
GATE_ACTIONS = {"cover": "open_cover", "lock": "unlock", "button": "press", "script": "turn_on", "switch": "turn_on", "input_boolean": "turn_on"}


def lock_entities(value):
    if isinstance(value, str):
        value = value.replace(",", " ").split()
    if not isinstance(value, list) or any(
        not isinstance(entity, str) or not ENTITY.fullmatch(entity)
        or entity.split(".", 1)[0] not in GATE_ACTIONS for entity in value
    ):
        raise ValueError("Выбери замок, привод, кнопку, сценарий или реле.")
    return list(dict.fromkeys(value))


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
        if not 2 <= len(parts) <= 7:
            raise ValueError("Формат профиля: имя | номер | устройства | калитка | календарь | person | доступ.")
        name, number, entities, gate, calendar, person, access = (parts + [""] * 7)[:7]
        entities = entities.replace(",", " ").split()
        locks = lock_entities(gate)
        if (not name or len(name) > 60 or not name.isprintable() or not NUMBER.fullmatch(number)
            or any(not ENTITY.fullmatch(item) for item in entities)
):
            raise ValueError("Проверьте имя, номер и идентификаторы устройств семьи.")
        if calendar and (not ENTITY.fullmatch(calendar) or not calendar.startswith("calendar.")):
            raise ValueError("Укажите календарь участника вида calendar.name.")
        if person and (not ENTITY.fullmatch(person) or not person.startswith("person.")):
            raise ValueError("Укажите сущность участника вида person.name.")
        access = access or "selected"
        if access not in {"selected", "read_all", "owner"}:
            raise ValueError("Неверный режим доступа участника.")
        number = number.lstrip("+")
        if number in profiles or name.casefold() in names:
            raise ValueError("Имена и номера членов семьи должны быть уникальны.")
        names.add(name.casefold())
        profiles[number] = {"name": name, "number": "+" + number, "entities": entities,
                            "gate": locks[0] if locks else "", "locks": locks, "calendar": calendar, "person": person, "access": access, "key": profile_key(name, number)}
    return profiles
