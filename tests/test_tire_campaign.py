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
        assert await repo.tire_campaign_applies_to_plate("О917НХ797")

    async with session_scope(maker) as session:
        repo = InspectionRepository(session)
        campaign = await repo.create_tire_campaign(False, 1, "Fedos_AV")
        await repo.add_tire_campaign_plate(campaign, "О917НХ797", "О917НХ797")
        assert await repo.tire_campaign_applies_to_plate("О917НХ797")
        assert not await repo.tire_campaign_applies_to_plate("С771СН761")


@pytest.mark.asyncio
async def test_tire_campaign_list_auto_finishes_when_all_plates_done():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    maker = make_sessionmaker(engine)
    await init_db(engine)

    async with session_scope(maker) as session:
        repo = InspectionRepository(session)
        campaign = await repo.create_tire_campaign(False, 1, "Fedos_AV")
        await repo.add_tire_campaign_plate(campaign, "О917НХ797", "О917НХ797")
        inspection = InspectionSession(
            id=10,
            telegram_user_id=2,
            status="COMPLETED",
            plate_normalized="О917НХ797",
            tire_score=5,
        )
        session.add(inspection)
        await session.flush()
        await repo.mark_tire_campaign_done_for_inspection(inspection)
        progress = await repo.tire_campaign_progress()
        assert progress is None


@pytest.mark.asyncio
async def test_completed_tire_campaign_plate_no_longer_applies():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    maker = make_sessionmaker(engine)
    await init_db(engine)

    async with session_scope(maker) as session:
        repo = InspectionRepository(session)
        campaign = await repo.create_tire_campaign(False, 1, "Fedos_AV")
        await repo.add_tire_campaign_plate(campaign, "О917НХ797", "О917НХ797")
        await repo.add_tire_campaign_plate(campaign, "С771СН761", "С771СН761")
        inspection = InspectionSession(
            id=10,
            telegram_user_id=2,
            status="COMPLETED",
            plate_normalized="О917НХ797",
            tire_score=5,
        )
        session.add(inspection)
        await session.flush()

        assert await repo.tire_campaign_applies_to_plate("О917НХ797")
        assert await repo.mark_tire_campaign_done_for_inspection(inspection) is None
        assert not await repo.tire_campaign_applies_to_plate("О917НХ797")
        assert await repo.tire_campaign_applies_to_plate("С771СН761")


@pytest.mark.asyncio
async def test_tire_check_is_needed_only_until_plate_has_completed_tire_score():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    maker = make_sessionmaker(engine)
    await init_db(engine)

    async with session_scope(maker) as session:
        repo = InspectionRepository(session)
        assert not await repo.has_tire_check_for_plate("О917НХ797")
        inspection = InspectionSession(
            id=10,
            telegram_user_id=2,
            status="COMPLETED",
            plate_normalized="О917НХ797",
            tire_score=5,
        )
        session.add(inspection)
        await session.flush()

        assert await repo.has_tire_check_for_plate("О917НХ797")
        assert not await repo.has_tire_check_for_plate("О917НХ797", exclude_inspection_id=10)
