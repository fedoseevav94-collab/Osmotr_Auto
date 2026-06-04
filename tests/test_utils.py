from app.constants import SCENARIO_MARKERS, Scenario
from app.utils import display_plate, is_valid_plate, normalize_plate


def test_normalize_plate_supports_mixed_cyrillic_latin():
    assert normalize_plate("о917нх797") == "О917НХ797"
    assert normalize_plate("с771сн761") == "С771СН761"
    assert normalize_plate("т553нм797") == "Т553НМ797"
    assert display_plate("О917НХ797") == "О917НХ797"


def test_plate_validation_accepts_supported_formats():
    assert is_valid_plate("о917нх797")
    assert is_valid_plate("А123ВС77")
    assert is_valid_plate("АА77777")
    assert is_valid_plate("ВВ666177")


def test_plate_validation_rejects_random_text():
    assert not is_valid_plate("СКЕРЕНЕВСНООВРЕН")
    assert not is_valid_plate("123")
    assert not is_valid_plate("А777АА")


def test_transfer_marker_is_returned_as_surrendered():
    assert SCENARIO_MARKERS[Scenario.TRANSFER] == "сдал"
