from __future__ import annotations

from enum import StrEnum


class Scenario(StrEnum):
    RETURN = "Сдача"
    TRANSFER = "Пересадка"
    ISSUE = "Выдача авто"
    PLANNED = "Плановый осмотр"
    ACCIDENT = "Осмотр после ДТП"
    TIRES = "Проверка резины"


class PhotoType(StrEnum):
    PLATE = "PLATE"
    DASHBOARD = "DASHBOARD"
    DAMAGE = "DAMAGE"
    TIRE = "TIRE"


class SessionStatus(StrEnum):
    DRAFT = "DRAFT"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


SCENARIO_MARKERS = {
    Scenario.RETURN: "сдал",
    Scenario.TRANSFER: "сдал",
    Scenario.ISSUE: "выдача",
    Scenario.PLANNED: "осмотр",
    Scenario.ACCIDENT: "осмотр ДТП",
    Scenario.TIRES: "резина",
}

STANDARD_SCENARIOS = (
    Scenario.RETURN,
    Scenario.TRANSFER,
    Scenario.ISSUE,
    Scenario.PLANNED,
    Scenario.ACCIDENT,
)
SURRENDER_SCENARIOS = {Scenario.RETURN, Scenario.TRANSFER}
SCORE_SCENARIOS = {Scenario.RETURN, Scenario.TRANSFER, Scenario.PLANNED}
TIRE_REQUIRED_SCENARIOS: set[Scenario] = set()

DTP_LABELS = {
    "guilty": "водитель виноват",
    "not_guilty": "водитель не виноват",
    "unknown": "требуется уточнение",
}

SCORE_FIELDS = (
    ("body", "Кузовные элементы"),
    ("tech", "Техническое состояние"),
    ("wrap", "Оклейка"),
)

TIRE_TYPES = {
    "winter": "зимняя",
    "summer": "летняя",
}

PROBLEM_SCORE_THRESHOLD = 4
