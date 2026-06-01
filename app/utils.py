from __future__ import annotations

import re

CYR_TO_LAT = str.maketrans(
    {
        "А": "A",
        "В": "B",
        "Е": "E",
        "К": "K",
        "М": "M",
        "Н": "H",
        "О": "O",
        "Р": "P",
        "С": "C",
        "Т": "T",
        "У": "Y",
        "Х": "X",
        "а": "A",
        "в": "B",
        "е": "E",
        "к": "K",
        "м": "M",
        "н": "H",
        "о": "O",
        "р": "P",
        "с": "C",
        "т": "T",
        "у": "Y",
        "х": "X",
    }
)


def normalize_plate(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.translate(CYR_TO_LAT).upper())


def is_supervisor(username: str | None, supervisor_username: str) -> bool:
    return bool(username) and username.lstrip("@").lower() == supervisor_username.lstrip("@").lower()


def user_display(username: str | None, full_name: str | None) -> str:
    if username:
        return f"@{username}"
    return full_name or "без username"

