from datetime import datetime

import pytest

from app.db import init_db, make_engine, make_sessionmaker, session_scope
from app.models import InspectionSession
from app.repository import InspectionRepository


@pytest.mark.asyncio
async def test_problem_rows_include_damage_and_low_tire_score():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    maker = make_sessionmaker(engine)
    await init_db(engine)

    async with session_scope(maker) as session:
        session.add_all(
            [
                InspectionSession(
                    telegram_user_id=1,
                    status="COMPLETED",
                    plate_normalized="A111AA797",
                    completed_at=datetime(2026, 6, 2, 10),
                    has_damage=True,
                ),
                InspectionSession(
                    telegram_user_id=1,
                    status="COMPLETED",
                    plate_normalized="B222BB797",
                    completed_at=datetime(2026, 6, 2, 11),
                    has_damage=False,
                    tire_score=3,
                ),
                InspectionSession(
                    telegram_user_id=1,
                    status="COMPLETED",
                    plate_normalized="C333CC797",
                    completed_at=datetime(2026, 6, 2, 12),
                    has_damage=False,
                    body_score=5,
                    tech_score=5,
                    wrap_score=5,
                    tire_score=5,
                ),
            ]
        )

    async with session_scope(maker) as session:
        repo = InspectionRepository(session)
        rows = await repo.problem_rows(datetime(2026, 6, 2), datetime(2026, 6, 3))
        assert {row.plate_normalized for row in rows} == {"A111AA797", "B222BB797"}
