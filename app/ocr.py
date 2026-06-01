from __future__ import annotations

from pathlib import Path

from app.utils import normalize_plate


def recognize_plate_from_image(path: Path) -> str | None:
    try:
        from PIL import Image
        import pytesseract
    except Exception:
        return None

    try:
        text = pytesseract.image_to_string(
            Image.open(path),
            lang="rus+eng",
            config="--psm 7 -c tessedit_char_whitelist=ABEKMHOPCTYXАВЕКМНОРСТУХ0123456789",
        )
    except Exception:
        return None
    plate = normalize_plate(text)
    if len(plate) < 6:
        return None
    return plate
