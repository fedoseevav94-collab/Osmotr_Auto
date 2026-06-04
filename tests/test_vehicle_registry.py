from pathlib import Path

from openpyxl import Workbook

from app.vehicle_registry import read_vehicle_rows


def test_read_vehicle_registry_from_excel(tmp_path: Path):
    path = tmp_path / "cars.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["Марка", "Модель", "Номер", "Статус"])
    ws.append(["Kia", "Rio", "о917нх797", "Доступно"])
    wb.save(path)

    rows = read_vehicle_rows(path)

    assert rows == [
        {
            "plate_raw": "О917НХ797",
            "plate_normalized": "О917НХ797",
            "brand": "Kia",
            "model": "Rio",
            "status": "Доступно",
            "source": str(path),
        }
    ]
