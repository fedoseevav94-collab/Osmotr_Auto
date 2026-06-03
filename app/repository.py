from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Select, and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.constants import PROBLEM_SCORE_THRESHOLD, PhotoType, SessionStatus
from app.models import (
    BotUser,
    DamageControlCase,
    InspectionAction,
    InspectionPhoto,
    InspectionSession,
    KnownVehiclePlate,
    TireCheckCampaign,
    TireCheckCampaignPlate,
)


class InspectionRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_session(self, user_id: int, username: str | None, name: str | None) -> InspectionSession:
        inspection = InspectionSession(
            telegram_user_id=user_id,
            telegram_username=username,
            telegram_name=name,
            status=SessionStatus.DRAFT.value,
        )
        self.session.add(inspection)
        await self.session.flush()
        return inspection

    async def get(self, inspection_id: int) -> InspectionSession | None:
        return await self.session.get(
            InspectionSession,
            inspection_id,
            options=(selectinload(InspectionSession.photos),),
        )

    async def active_for_user(self, user_id: int) -> InspectionSession | None:
        query = (
            select(InspectionSession)
            .where(
                InspectionSession.telegram_user_id == user_id,
                InspectionSession.status == SessionStatus.DRAFT.value,
            )
            .order_by(desc(InspectionSession.updated_at), desc(InspectionSession.id))
            .options(selectinload(InspectionSession.photos))
        )
        return (await self.session.scalars(query)).first()

    async def drafts_for_user(self, user_id: int) -> list[InspectionSession]:
        query = (
            select(InspectionSession)
            .where(
                InspectionSession.telegram_user_id == user_id,
                InspectionSession.status == SessionStatus.DRAFT.value,
            )
            .order_by(desc(InspectionSession.updated_at), desc(InspectionSession.id))
        )
        return list(await self.session.scalars(query))

    async def add_photo(
        self,
        inspection: InspectionSession,
        photo_type: PhotoType,
        file_id: str,
        file_unique_id: str,
    ) -> InspectionPhoto:
        photo = InspectionPhoto(
            inspection_id=inspection.id,
            photo_type=photo_type.value,
            telegram_file_id=file_id,
            telegram_file_unique_id=file_unique_id,
        )
        self.session.add(photo)
        await self.session.flush()
        return photo

    async def log_action(
        self,
        inspection: InspectionSession,
        action_type: str,
        user_id: int,
        username: str | None,
        comment: str | None = None,
    ) -> None:
        self.session.add(
            InspectionAction(
                inspection_id=inspection.id,
                action_type=action_type,
                telegram_user_id=user_id,
                telegram_username=username,
                comment=comment,
            )
        )

    async def complete(self, inspection: InspectionSession) -> None:
        inspection.status = SessionStatus.COMPLETED.value
        inspection.completed_at = _utcnow_naive()

    async def cancel(self, inspection: InspectionSession) -> None:
        inspection.status = SessionStatus.CANCELLED.value
        inspection.cancelled_at = _utcnow_naive()

    async def score_rows(self, start: datetime, end: datetime) -> list[InspectionSession]:
        return await self._completed_between(start, end)

    async def history_for_plate(self, plate_normalized: str) -> list[InspectionSession]:
        query = (
            select(InspectionSession)
            .where(
                InspectionSession.plate_normalized == plate_normalized,
                InspectionSession.status == SessionStatus.COMPLETED.value,
            )
            .order_by(desc(InspectionSession.completed_at), desc(InspectionSession.id))
        )
        return list(await self.session.scalars(query))

    async def get_known_plate(self, plate_normalized: str) -> KnownVehiclePlate | None:
        query = select(KnownVehiclePlate).where(KnownVehiclePlate.plate_normalized == plate_normalized)
        return (await self.session.scalars(query)).first()

    async def list_known_plate_values(self) -> list[str]:
        query = select(KnownVehiclePlate.plate_normalized)
        return list(await self.session.scalars(query))

    async def search_known_plates_by_digits(self, digits: str, limit: int = 12) -> list[KnownVehiclePlate]:
        query = (
            select(KnownVehiclePlate)
            .where(
                or_(
                    KnownVehiclePlate.plate_normalized.like(f"_{digits}%"),
                    KnownVehiclePlate.plate_normalized.like(f"__{digits}%"),
                )
            )
            .order_by(KnownVehiclePlate.plate_normalized)
            .limit(limit)
        )
        return list(await self.session.scalars(query))

    async def latest_user_id_by_username(self, username: str) -> int | None:
        normalized = username.strip().lstrip("@").lower()
        if not normalized:
            return None
        user_query = (
            select(BotUser.telegram_user_id)
            .where(func.lower(BotUser.telegram_username) == normalized)
            .order_by(desc(BotUser.updated_at), desc(BotUser.id))
            .limit(1)
        )
        bot_user_id = await self.session.scalar(user_query)
        if bot_user_id is not None:
            return bot_user_id
        action_query = (
            select(InspectionAction.telegram_user_id)
            .where(func.lower(InspectionAction.telegram_username) == normalized)
            .order_by(desc(InspectionAction.created_at), desc(InspectionAction.id))
            .limit(1)
        )
        action_user_id = await self.session.scalar(action_query)
        if action_user_id is not None:
            return action_user_id
        inspection_query = (
            select(InspectionSession.telegram_user_id)
            .where(func.lower(InspectionSession.telegram_username) == normalized)
            .order_by(desc(InspectionSession.updated_at), desc(InspectionSession.id))
            .limit(1)
        )
        return await self.session.scalar(inspection_query)

    async def remember_bot_user(
        self,
        user_id: int,
        username: str | None,
        name: str | None,
    ) -> BotUser:
        user = await self.session.scalar(select(BotUser).where(BotUser.telegram_user_id == user_id))
        if user is None:
            user = BotUser(telegram_user_id=user_id)
            self.session.add(user)
        user.telegram_username = username.strip().lstrip("@") if username else None
        user.telegram_name = name
        await self.session.flush()
        return user

    async def upsert_known_plate(
        self,
        plate_raw: str,
        plate_normalized: str,
        brand: str | None = None,
        model: str | None = None,
        status: str | None = None,
        source: str | None = None,
    ) -> KnownVehiclePlate:
        known = await self.get_known_plate(plate_normalized)
        if known is None:
            known = KnownVehiclePlate(
                plate_raw=plate_raw,
                plate_normalized=plate_normalized,
                brand=brand,
                model=model,
                status=status,
                source=source,
            )
            self.session.add(known)
        else:
            known.plate_raw = plate_raw
            known.brand = brand
            known.model = model
            known.status = status
            known.source = source
        await self.session.flush()
        return known

    async def create_tire_campaign(
        self,
        applies_to_all: bool,
        user_id: int,
        username: str | None,
        expires_at: datetime | None = None,
    ) -> TireCheckCampaign:
        await self.finish_active_tire_campaign()
        campaign = TireCheckCampaign(
            status="ACTIVE",
            applies_to_all=applies_to_all,
            created_by_user_id=user_id,
            created_by_username=username,
            expires_at=expires_at,
        )
        self.session.add(campaign)
        await self.session.flush()
        return campaign

    async def finish_active_tire_campaign(self) -> int:
        campaign = await self.active_tire_campaign()
        if campaign is None:
            return 0
        campaign.status = "FINISHED"
        campaign.finished_at = _utcnow_naive()
        await self.session.flush()
        return campaign.id

    async def active_tire_campaign(self) -> TireCheckCampaign | None:
        await self.expire_tire_campaigns()
        query = (
            select(TireCheckCampaign)
            .where(TireCheckCampaign.status == "ACTIVE")
            .order_by(desc(TireCheckCampaign.created_at), desc(TireCheckCampaign.id))
        )
        return (await self.session.scalars(query)).first()

    async def expire_tire_campaigns(self) -> int:
        now = _utcnow_naive()
        query = select(TireCheckCampaign).where(
            TireCheckCampaign.status == "ACTIVE",
            TireCheckCampaign.expires_at.is_not(None),
            TireCheckCampaign.expires_at <= now,
        )
        campaigns = list(await self.session.scalars(query))
        for campaign in campaigns:
            campaign.status = "EXPIRED"
            campaign.finished_at = now
        if campaigns:
            await self.session.flush()
        return len(campaigns)

    async def add_tire_campaign_plate(
        self,
        campaign: TireCheckCampaign,
        plate_raw: str | None,
        plate_normalized: str,
    ) -> TireCheckCampaignPlate:
        plate = TireCheckCampaignPlate(
            campaign_id=campaign.id,
            plate_raw=plate_raw,
            plate_normalized=plate_normalized,
        )
        self.session.add(plate)
        await self.session.flush()
        return plate

    async def tire_campaign_applies_to_plate(self, plate_normalized: str | None) -> bool:
        if not plate_normalized:
            return False
        campaign = await self.active_tire_campaign()
        if campaign is None:
            return False
        if campaign.applies_to_all:
            return True
        query = select(TireCheckCampaignPlate.id).where(
            TireCheckCampaignPlate.campaign_id == campaign.id,
            TireCheckCampaignPlate.plate_normalized == plate_normalized,
        )
        return (await self.session.scalars(query)).first() is not None

    async def mark_tire_campaign_done_for_inspection(self, inspection: InspectionSession) -> None:
        if not inspection.plate_normalized or inspection.tire_score is None:
            return
        campaign = await self.active_tire_campaign()
        if campaign is None or campaign.applies_to_all:
            return
        query = select(TireCheckCampaignPlate).where(
            TireCheckCampaignPlate.campaign_id == campaign.id,
            TireCheckCampaignPlate.plate_normalized == inspection.plate_normalized,
            TireCheckCampaignPlate.completed_at.is_(None),
        )
        plate = (await self.session.scalars(query)).first()
        if plate is None:
            return
        plate.completed_at = _utcnow_naive()
        plate.inspection_id = inspection.id
        remaining = await self.session.scalar(
            select(func.count())
            .select_from(TireCheckCampaignPlate)
            .where(
                TireCheckCampaignPlate.campaign_id == campaign.id,
                TireCheckCampaignPlate.completed_at.is_(None),
            )
        )
        if remaining == 0:
            campaign.status = "FINISHED"
            campaign.finished_at = _utcnow_naive()
        await self.session.flush()

    async def tire_campaign_progress(self) -> dict[str, object] | None:
        campaign = await self.active_tire_campaign()
        if campaign is None:
            return None
        total = done = None
        if not campaign.applies_to_all:
            total = await self.session.scalar(
                select(func.count())
                .select_from(TireCheckCampaignPlate)
                .where(TireCheckCampaignPlate.campaign_id == campaign.id)
            )
            done = await self.session.scalar(
                select(func.count())
                .select_from(TireCheckCampaignPlate)
                .where(
                    TireCheckCampaignPlate.campaign_id == campaign.id,
                    TireCheckCampaignPlate.completed_at.is_not(None),
                )
            )
        return {"campaign": campaign, "total": total, "done": done}

    async def problem_rows(self, start: datetime, end: datetime) -> list[InspectionSession]:
        query = (
            select(InspectionSession)
            .where(
                InspectionSession.status == SessionStatus.COMPLETED.value,
                InspectionSession.completed_at >= start,
                InspectionSession.completed_at < end,
                or_(
                    InspectionSession.has_damage.is_(True),
                    InspectionSession.body_score < PROBLEM_SCORE_THRESHOLD,
                    InspectionSession.tech_score < PROBLEM_SCORE_THRESHOLD,
                    InspectionSession.wrap_score < PROBLEM_SCORE_THRESHOLD,
                    InspectionSession.tire_score < PROBLEM_SCORE_THRESHOLD,
                ),
            )
            .order_by(desc(InspectionSession.completed_at), desc(InspectionSession.id))
        )
        return list(await self.session.scalars(query))

    async def damage_control_rows(self, start: datetime, end: datetime) -> list[DamageControlCase]:
        query = (
            select(DamageControlCase)
            .join(InspectionSession, DamageControlCase.inspection_id == InspectionSession.id)
            .where(
                InspectionSession.status == SessionStatus.COMPLETED.value,
                InspectionSession.completed_at >= start,
                InspectionSession.completed_at < end,
                DamageControlCase.payment_amount.is_not(None),
            )
            .order_by(desc(InspectionSession.completed_at), desc(DamageControlCase.id))
            .options(selectinload(DamageControlCase.inspection))
        )
        return list(await self.session.scalars(query))

    async def open_damage_control_cases(self, final_statuses: set[str]) -> list[DamageControlCase]:
        query = (
            select(DamageControlCase)
            .where(DamageControlCase.status.not_in(final_statuses))
            .order_by(desc(DamageControlCase.created_at), desc(DamageControlCase.id))
            .options(selectinload(DamageControlCase.inspection))
        )
        return list(await self.session.scalars(query))

    async def waiting_service_amount_cases(self, final_statuses: set[str]) -> list[DamageControlCase]:
        query = (
            select(DamageControlCase)
            .where(
                DamageControlCase.status.not_in(final_statuses),
                DamageControlCase.service_requested_at.is_not(None),
                DamageControlCase.service_received_at.is_(None),
            )
            .order_by(desc(DamageControlCase.service_requested_at), desc(DamageControlCase.id))
            .options(selectinload(DamageControlCase.inspection))
        )
        return list(await self.session.scalars(query))

    async def completed_with_photos_between(self, start: datetime, end: datetime) -> list[InspectionSession]:
        query = (
            select(InspectionSession)
            .where(
                InspectionSession.status == SessionStatus.COMPLETED.value,
                InspectionSession.completed_at >= start,
                InspectionSession.completed_at < end,
            )
            .order_by(desc(InspectionSession.completed_at), desc(InspectionSession.id))
            .options(selectinload(InspectionSession.photos))
        )
        return list(await self.session.scalars(query))

    async def stats_between(self, start: datetime, end: datetime) -> dict[str, int]:
        rows = await self._completed_between(start, end)
        return {
            "total": len(rows),
            "with_damage": sum(1 for row in rows if row.has_damage),
            "with_tire": sum(1 for row in rows if row.tire_score is not None),
            "low_scores": sum(
                1
                for row in rows
                if any(
                    score is not None and score < PROBLEM_SCORE_THRESHOLD
                    for score in (row.body_score, row.tech_score, row.wrap_score, row.tire_score)
                )
            ),
        }

    async def _completed_between(self, start: datetime, end: datetime) -> list[InspectionSession]:
        completed_filter = and_(
            InspectionSession.status == SessionStatus.COMPLETED.value,
            InspectionSession.completed_at >= start,
            InspectionSession.completed_at < end,
        )
        query: Select[tuple[InspectionSession]] = (
            select(InspectionSession)
            .where(completed_filter)
            .order_by(desc(InspectionSession.completed_at), desc(InspectionSession.id))
        )
        return list(await self.session.scalars(query))


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
