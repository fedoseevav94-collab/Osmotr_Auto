from app.constants import SCENARIO_MARKERS, Scenario
from app.utils import normalize_plate


def test_normalize_plate_supports_mixed_cyrillic_latin():
    assert normalize_plate("о917нх797") == "O917HX797"
    assert normalize_plate("с771сн761") == "C771CH761"
    assert normalize_plate("т553нм797") == "T553HM797"


def test_transfer_marker_is_returned_as_surrendered():
    assert SCENARIO_MARKERS[Scenario.TRANSFER] == "сдал"

