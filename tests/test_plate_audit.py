from datetime import datetime

from app.plate_audit import PlateAuditItem, build_plate_audit_report


def test_plate_audit_report_lists_mismatches_and_unrecognized():
    report = build_plate_audit_report(
        [
            PlateAuditItem(1, "О864ОО797", "О864ОО797", "match", "https://t.me/c/1/10"),
            PlateAuditItem(2, "Х480СХ797", "Х480СК797", "mismatch", "https://t.me/c/1/20"),
            PlateAuditItem(3, "С771СН761", None, "unrecognized", "https://t.me/c/1/30"),
        ],
        datetime(2026, 6, 1),
        datetime(2026, 6, 2),
    )

    assert "Проверено: 3" in report
    assert "Совпало: 1" in report
    assert "#2: указано Х480СХ797, на фото похоже Х480СК797" in report
    assert "Осмотр: https://t.me/c/1/20" in report
    assert "#3: С771СН761 — номер на фото не распознан" in report
    assert "Осмотр: https://t.me/c/1/30" in report
