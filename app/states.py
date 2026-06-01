from aiogram.fsm.state import State, StatesGroup


class InspectionFlow(StatesGroup):
    choosing_scenario = State()
    accident_guilt = State()
    plate_photo = State()
    plate_confirm = State()
    plate_text = State()
    dashboard_photo = State()
    damage_question = State()
    damage_photos = State()
    damage_description = State()
    score = State()
    score_comment = State()
    driver_remarks = State()
    driver_remarks_comment = State()
    tire_type = State()
    tire_photo = State()
    tire_score = State()
    tire_comment = State()
    publishing = State()


class ExportFlow(StatesGroup):
    custom_period = State()
    problem_custom_period = State()


class TireCampaignFlow(StatesGroup):
    waiting_list_file = State()
