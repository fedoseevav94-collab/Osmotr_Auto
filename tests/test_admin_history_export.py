from datetime import datetime

from app.config import normalize_telegram_chat_id
from app.export import fp_link, period_bounds
from app.models import InspectionSession
from app.utils import is_supervisor, normalize_plate


def test_export_scores_supervisor_only():
    assert is_supervisor("Fedos_AV", "Fedos_AV")
    assert not is_supervisor("someone_else", "Fedos_AV")


def test_history_auto_normalizes_plate_before_lookup():
    assert normalize_plate("о917нх797") == "O917HX797"


def test_period_bounds_today():
    start, end = period_bounds("today", datetime(2026, 6, 1, 12, 0))
    assert start == datetime(2026, 6, 1)
    assert end == datetime(2026, 6, 2)


def test_fp_link_from_positive_chat_id():
    row = InspectionSession(fp_chat_id=1001905865504, fp_message_id=123)
    assert fp_link(row) == "https://t.me/c/1905865504/123"


def test_positive_supergroup_chat_id_is_normalized_for_bot_api():
    assert normalize_telegram_chat_id("1001905865504") == -1001905865504
    assert normalize_telegram_chat_id("-1001905865504") == -1001905865504
