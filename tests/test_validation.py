from app.constants import PhotoType, Scenario
from app.models import InspectionPhoto, InspectionSession
from app.validation import validate_completion


def make_inspection(scenario: Scenario = Scenario.RETURN) -> InspectionSession:
    return InspectionSession(
        id=1,
        telegram_user_id=1,
        scenario=scenario.value,
        status="DRAFT",
        plate_raw="о917нх797",
        plate_normalized="О917НХ797",
        has_damage=False,
        body_score=4,
        tech_score=5,
        wrap_score=4,
        driver_has_remarks=False,
        photos=[
            InspectionPhoto(photo_type=PhotoType.PLATE.value, telegram_file_id="p", telegram_file_unique_id="pu"),
            InspectionPhoto(photo_type=PhotoType.DASHBOARD.value, telegram_file_id="d", telegram_file_unique_id="du"),
        ],
    )


def test_return_requires_plate_and_dashboard_photos():
    inspection = make_inspection()
    inspection.photos = []
    errors = validate_completion(inspection)
    assert "нужно фото госномера" in errors
    assert "для сдачи/пересадки нужно фото приборной панели" in errors


def test_damage_requires_photo_and_description():
    inspection = make_inspection()
    inspection.has_damage = True
    inspection.damage_description = None
    errors = validate_completion(inspection)
    assert "нужно фото повреждений" in errors
    assert "нужно описание повреждений" in errors


def test_damage_without_description_does_not_complete():
    inspection = make_inspection()
    inspection.has_damage = True
    inspection.photos.append(
        InspectionPhoto(photo_type=PhotoType.DAMAGE.value, telegram_file_id="x", telegram_file_unique_id="xu")
    )
    errors = validate_completion(inspection)
    assert "нужно описание повреждений" in errors


def test_low_score_requires_comment():
    inspection = make_inspection(Scenario.PLANNED)
    inspection.body_score = 3
    inspection.body_comment = None
    errors = validate_completion(inspection)
    assert "нужен комментарий к оценке: Кузовные элементы" in errors


def test_high_score_does_not_require_comment():
    inspection = make_inspection(Scenario.PLANNED)
    inspection.body_score = 4
    inspection.body_comment = None
    assert "нужен комментарий к оценке: Кузовные элементы" not in validate_completion(inspection)


def test_tire_score_below_four_requires_comment():
    inspection = make_inspection(Scenario.PLANNED)
    inspection.tire_type = "winter"
    inspection.tire_score = 3
    inspection.tire_comment = None
    errors = validate_completion(inspection)
    assert "нужен комментарий к оценке резины" in errors


def test_tire_score_four_does_not_require_comment():
    inspection = make_inspection(Scenario.PLANNED)
    inspection.tire_type = "summer"
    inspection.tire_score = 4
    inspection.tire_comment = None
    assert "нужен комментарий к оценке резины" not in validate_completion(inspection)


def test_issue_does_not_require_tire_check_without_campaign():
    inspection = make_inspection(Scenario.ISSUE)
    inspection.photos = [
        InspectionPhoto(photo_type=PhotoType.PLATE.value, telegram_file_id="p", telegram_file_unique_id="pu"),
    ]
    inspection.tire_type = None
    inspection.tire_score = None

    errors = validate_completion(inspection)

    assert "нужно выбрать тип резины" not in errors
    assert "нужна оценка состояния резины" not in errors
    assert "нужно фото резины" not in errors


def test_return_requires_driver_remarks_answer():
    inspection = make_inspection()
    inspection.driver_has_remarks = None
    errors = validate_completion(inspection)
    assert "нужно указать, есть ли замечания у водителя" in errors


def test_driver_remarks_yes_requires_comment():
    inspection = make_inspection()
    inspection.driver_has_remarks = True
    inspection.driver_remarks_comment = None
    errors = validate_completion(inspection)
    assert "нужно описание замечаний водителя" in errors


def test_driver_remarks_already_is_accepted():
    inspection = make_inspection()
    inspection.driver_has_remarks = True
    inspection.driver_remarks_comment = "Указал ранее"
    assert "нужно описание замечаний водителя" not in validate_completion(inspection)
