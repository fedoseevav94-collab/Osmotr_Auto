from app.constants import SCENARIO_MARKERS, Scenario
from app.ocr import extract_plate_from_text
from app.utils import is_valid_plate, normalize_plate


def test_normalize_plate_supports_mixed_cyrillic_latin():
    assert normalize_plate("о917нх797") == "O917HX797"
    assert normalize_plate("с771сн761") == "C771CH761"
    assert normalize_plate("т553нм797") == "T553HM797"


def test_plate_validation_accepts_supported_formats():
    assert is_valid_plate("о917нх797")
    assert is_valid_plate("A123BC77")
    assert is_valid_plate("АА77777")
    assert is_valid_plate("ВВ666177")


def test_plate_validation_rejects_random_text():
    assert not is_valid_plate("СКЕРЕНЕВСНООВРЕН")
    assert not is_valid_plate("123")
    assert not is_valid_plate("А777АА")


def test_transfer_marker_is_returned_as_surrendered():
    assert SCENARIO_MARKERS[Scenario.TRANSFER] == "сдал"


def test_extract_plate_from_ocr_text_with_noise():
    assert extract_plate_from_text("Яндекс Go\n- Х480СХ797 RUS\nSTAX.RU") == "X480CX797"


def test_extract_plate_from_ocr_text_with_zero_letter_confusion():
    assert extract_plate_from_text("086400797") == "O864OO797"


def test_extract_plate_from_ocr_text_supports_taxi_plate_format():
    assert extract_plate_from_text("такси АА77777 RUS") == "AA77777"
    assert extract_plate_from_text("ВВ666177") == "BB666177"
