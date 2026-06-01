from __future__ import annotations

import logging
import re
from pathlib import Path

from app.utils import normalize_plate

PLATE_RE = re.compile(r"[ABEKMHOPCTYX]\d{3}[ABEKMHOPCTYX]{2}\d{2,3}")
OCR_WHITELIST = "ABEKMHOPCTYXАВЕКМНОРСТУХabekmhopctyxавекмнорстух0123456789"
LETTER_POSITIONS = {0, 4, 5}
LETTER_FIXES = {
    "0": "O",
    "8": "B",
    "4": "A",
}
DIGIT_FIXES = {
    "O": "0",
    "Q": "0",
    "D": "0",
    "I": "1",
    "L": "1",
    "Z": "2",
    "S": "5",
    "B": "8",
}


def extract_plate_from_text(text: str) -> str | None:
    normalized = normalize_plate(text)
    match = PLATE_RE.search(normalized)
    if match:
        return match.group(0)
    for size in (9, 8):
        for start in range(max(len(normalized) - size + 1, 0)):
            plate = _coerce_plate_candidate(normalized[start : start + size])
            if plate and PLATE_RE.fullmatch(plate):
                return plate
    return None


def recognize_plate_from_image(path: Path) -> str | None:
    try:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps
        import pytesseract
    except Exception as exc:
        logging.warning("OCR dependencies are not available: %s", exc)
        return None

    try:
        image = Image.open(path)
        candidates = _image_candidates(image, ImageOps, ImageEnhance, ImageFilter)
        configs = [
            f"--oem 3 --psm 7 -c tessedit_char_whitelist={OCR_WHITELIST}",
            f"--oem 3 --psm 8 -c tessedit_char_whitelist={OCR_WHITELIST}",
        ]
        for candidate in candidates:
            for config in configs:
                try:
                    text = pytesseract.image_to_string(candidate, lang="rus+eng", config=config, timeout=1.2)
                except RuntimeError:
                    continue
                plate = extract_plate_from_text(text)
                if plate:
                    return plate
    except Exception as exc:
        logging.warning("OCR failed for %s: %s", path, exc)
        return None
    return None


def _coerce_plate_candidate(value: str) -> str | None:
    result = []
    for index, char in enumerate(value):
        if index in LETTER_POSITIONS:
            fixed = LETTER_FIXES.get(char, char)
            if fixed not in "ABEKMHOPCTYX":
                return None
            result.append(fixed)
        else:
            fixed = DIGIT_FIXES.get(char, char)
            if not fixed.isdigit():
                return None
            result.append(fixed)
    return "".join(result)


def _image_candidates(image, ImageOps, ImageEnhance, ImageFilter):
    width, height = image.size
    crops = [
        image.crop((0, int(height * 0.42), width, int(height * 0.88))),
        image.crop((int(width * 0.08), int(height * 0.48), int(width * 0.92), int(height * 0.82))),
    ]
    candidates = []
    for crop in crops:
        gray = ImageOps.grayscale(crop)
        scale = 2 if max(gray.size) < 1200 else 1
        if scale > 1:
            gray = gray.resize((gray.width * scale, gray.height * scale))
        enhanced = ImageEnhance.Contrast(ImageOps.autocontrast(gray)).enhance(2.2)
        sharpened = enhanced.filter(ImageFilter.SHARPEN)
        threshold = sharpened.point(lambda pixel: 255 if pixel > 145 else 0)
        candidates.extend([sharpened, threshold])
    return candidates
