from __future__ import annotations

from app.constants import (
    ALWAYS_SCORE_SCENARIO_FIELDS,
    PhotoType,
    SCORE_FIELD_TITLES,
    SCORE_FIELDS,
    SURRENDER_SCENARIOS,
    TIRE_REQUIRED_SCENARIOS,
    TIRE_TYPES,
    Scenario,
)
from app.models import InspectionSession


def has_photo(inspection: InspectionSession, photo_type: PhotoType) -> bool:
    return any(photo.photo_type == photo_type.value for photo in inspection.photos)


def validate_completion(inspection: InspectionSession) -> list[str]:
    errors: list[str] = []
    scenario = Scenario(inspection.scenario) if inspection.scenario else None

    if not inspection.plate_normalized:
        errors.append("нельзя завершить осмотр без госномера")
    if not has_photo(inspection, PhotoType.PLATE):
        errors.append("нужно фото госномера")
    if scenario in SURRENDER_SCENARIOS and not has_photo(inspection, PhotoType.DASHBOARD):
        errors.append("для сдачи/пересадки нужно фото приборной панели")
    if scenario == Scenario.TIRES:
        if inspection.tire_type not in TIRE_TYPES:
            errors.append("нужно выбрать тип резины")
        if inspection.tire_score not in {1, 2, 3, 4, 5}:
            errors.append("нужна оценка состояния резины")
        if inspection.tire_score is not None and inspection.tire_score < 4 and not inspection.tire_comment:
            errors.append("нужен комментарий к оценке резины")
        if not has_photo(inspection, PhotoType.TIRE):
            errors.append("нужно фото резины")
        return errors

    if inspection.has_damage is None:
        errors.append("нужно указать, есть ли повреждения")
    if inspection.has_damage:
        if not has_photo(inspection, PhotoType.DAMAGE):
            errors.append("нужно фото повреждений")
        if not inspection.damage_description:
            errors.append("нужно описание повреждений")
    for prefix in ALWAYS_SCORE_SCENARIO_FIELDS.get(scenario, ()):
        title = SCORE_FIELD_TITLES[prefix]
        score = getattr(inspection, f"{prefix}_score")
        if score not in {1, 2, 3, 4, 5}:
            errors.append(f"нужна оценка: {title}")
    for prefix, title in SCORE_FIELDS:
        score = getattr(inspection, f"{prefix}_score")
        comment = getattr(inspection, f"{prefix}_comment")
        if score is not None and score < 4 and not comment:
            errors.append(f"нужен комментарий к оценке: {title}")
    if scenario in SURRENDER_SCENARIOS:
        if inspection.driver_has_remarks is None:
            errors.append("нужно указать, есть ли замечания у водителя")
        if inspection.driver_has_remarks and not inspection.driver_remarks_comment:
            errors.append("нужно описание замечаний водителя")
    if scenario in TIRE_REQUIRED_SCENARIOS or inspection.tire_type is not None or inspection.tire_score is not None:
        if inspection.tire_type not in TIRE_TYPES:
            errors.append("нужно выбрать тип резины")
        if inspection.tire_score not in {1, 2, 3, 4, 5}:
            errors.append("нужна оценка состояния резины")
        if inspection.tire_score is not None and inspection.tire_score < 4 and not inspection.tire_comment:
            errors.append("нужен комментарий к оценке резины")
        if not has_photo(inspection, PhotoType.TIRE):
            errors.append("нужно фото резины")
    if scenario == Scenario.ACCIDENT and not inspection.dtp_driver_guilty:
        errors.append("нужно указать виновность водителя при ДТП")
    return errors
