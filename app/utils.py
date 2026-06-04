from __future__ import annotations

import re

PLATE_FORMAT_HINT = "Формат: А123ВС77, А123ВС777, АА77777 или АА777177."
PLATE_ALLOWED_LETTERS = "АВЕКМНОРСТУХ"
STANDARD_PLATE_RE = re.compile(rf"^[{PLATE_ALLOWED_LETTERS}]\d{{3}}[{PLATE_ALLOWED_LETTERS}]{{2}}\d{{2,3}}$")
TAXI_PLATE_RE = re.compile(rf"^[{PLATE_ALLOWED_LETTERS}]{{2}}\d{{5,6}}$")

LAT_TO_CYR = str.maketrans(
    {
        "A": "А",
        "B": "В",
        "E": "Е",
        "K": "К",
        "M": "М",
        "H": "Н",
        "O": "О",
        "P": "Р",
        "C": "С",
        "T": "Т",
        "Y": "У",
        "X": "Х",
        "a": "А",
        "b": "В",
        "e": "Е",
        "k": "К",
        "m": "М",
        "h": "Н",
        "o": "О",
        "p": "Р",
        "c": "С",
        "t": "Т",
        "y": "У",
        "x": "Х",
    }
)


def normalize_plate(value: str) -> str:
    return re.sub(r"[^АВЕКМНОРСТУХ0-9]", "", value.translate(LAT_TO_CYR).upper())


def display_plate(value: str | None) -> str:
    if not value:
        return "без номера"
    normalized = normalize_plate(value)
    return normalized or value


def is_valid_plate(value: str) -> bool:
    plate = normalize_plate(value)
    return bool(STANDARD_PLATE_RE.fullmatch(plate) or TAXI_PLATE_RE.fullmatch(plate))


def is_supervisor(username: str | None, supervisor_username: str) -> bool:
    return bool(username) and username.lstrip("@").lower() == supervisor_username.lstrip("@").lower()


def user_display(username: str | None, full_name: str | None) -> str:
    if username:
        return f"@{username}"
    return full_name or "без username"
