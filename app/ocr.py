from __future__ import annotations

import logging
import re
from pathlib import Path

from app.utils import normalize_plate

PLATE_RE = re.compile(r"[ABEKMHOPCTYX]\d{3}[ABEKMHOPCTYX]{2}\d{2,3}")
OCR_WHITELIST = "ABEKMHOPCTYXАВЕКМНОРСТУХabekmhopctyxавекмнорстух0123456789"


def extract_plate_from_text(text: str) -> str | None:
    normalized = normalize_plate(text)
    match = PLATE_RE.search(normalized)
    return match.group(0) if match else None


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
            f"--oem 3 --psm 6 -c tessedit_char_whitelist={OCR_WHITELIST}",
            f"--oem 3 --psm 11 -c tessedit_char_whitelist={OCR_WHITELIST}",
        ]
        for candidate in candidates:
            for config in configs:
                text = pytesseract.image_to_string(candidate, lang="rus+eng", config=config)
                plate = extract_plate_from_text(text)
                if plate:
                    return plate
    except Exception as exc:
        logging.warning("OCR failed for %s: %s", path, exc)
        return None
    return None


def _image_candidates(image, ImageOps, ImageEnhance, ImageFilter):
    width, height = image.size
    crops = [
        image,
        image.crop((0, int(height * 0.35), width, height)),
        image.crop((int(width * 0.12), int(height * 0.45), int(width * 0.88), int(height * 0.85))),
    ]
    candidates = []
    for crop in crops:
        gray = ImageOps.grayscale(crop)
        scale = 2 if max(gray.size) < 1800 else 1
        if scale > 1:
            gray = gray.resize((gray.width * scale, gray.height * scale))
        enhanced = ImageEnhance.Contrast(ImageOps.autocontrast(gray)).enhance(2.2)
        sharpened = enhanced.filter(ImageFilter.SHARPEN)
        threshold = sharpened.point(lambda pixel: 255 if pixel > 145 else 0)
        candidates.extend([crop, enhanced, sharpened, threshold])
    return candidates
