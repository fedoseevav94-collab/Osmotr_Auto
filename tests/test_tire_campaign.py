import pytest

from app.db import init_db, make_engine, make_sessionmaker, session_scope
from app.models import InspectionSession
from app.repository import InspectionRepository
from app.utils import is_supervisor


def test_supervisor_username_is_case_insensitive():
    assert is_supervisor("Fedos_AV", "Fedos_AV")
    assert is_supervisor("fedos_av", "Fedos_AV")
    assert is_supervisor("@Fedos_AV", "fedos_av")


@pytest.mark.asyncio
async def test_tire_campaign_can_apply_to_all_or_specific_plate():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    maker = make_sessionmaker(engine)
    await init_db(engine)

    async with session_scope(maker) as session:
        repo = InspectionRepository(session)
        await repo.create_tire_campaign(True, 1, "Fedos_AV")
        assert await repo.tire_campaign_applies_to_plate("O917HX797")

    async with session_scope(maker) as session:
        repo = InspectionRepository(session)
        campaign = await repo.create_tire_campaign(False, 1, "Fedos_AV")
        await repo.add_tire_campaign_plate(campaign, "о917нх797", "O917HX797")
        assert await repo.tire_campaign_applies_to_plate("O917HX797")
        assert not await repo.tire_campaign_applies_to_plate("C771CH761")


@pytest.mark.asyncio
async def test_tire_campaign_list_auto_finishes_when_all_plates_done():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    maker = make_sessionmaker(engine)
    await init_db(engine)

    async with session_scope(maker) as session:
        repo = InspectionRepository(session)
        campaign = await repo.create_tire_campaign(False, 1, "Fedos_AV")
        await repo.add_tire_campaign_plate(campaign, "о917нх797", "O917HX797")
        inspection = InspectionSession(
            id=10,
            telegram_user_id=2,
            status="COMPLETED",
            plate_normalized="O917HX797",
            tire_score=5,
        )
        session.add(inspection)
        await session.flush()
        await repo.mark_tire_campaign_done_for_inspection(inspection)
        progress = await repo.tire_campaign_progress()
        assert progress is None
