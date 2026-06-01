from app.constants import SCENARIO_MARKERS, Scenario
from app.ocr import extract_plate_from_text
from app.utils import normalize_plate


def test_normalize_plate_supports_mixed_cyrillic_latin():
    assert normalize_plate("о917нх797") == "O917HX797"
    assert normalize_plate("с771сн761") == "C771CH761"
    assert normalize_plate("т553нм797") == "T553HM797"


def test_transfer_marker_is_returned_as_surrendered():
    assert SCENARIO_MARKERS[Scenario.TRANSFER] == "сдал"


def test_extract_plate_from_ocr_text_with_noise():
    assert extract_plate_from_text("Яндекс Go\n- Х480СХ797 RUS\nSTAX.RU") == "X480CX797"
