from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class InspectionSession(Base):
    __tablename__ = "inspection_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    telegram_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scenario: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    plate_raw: Mapped[str | None] = mapped_column(String(32), nullable=True)
    plate_normalized: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    dtp_driver_guilty: Mapped[str | None] = mapped_column(String(32), nullable=True)
    has_damage: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    damage_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    body_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    tech_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tech_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    wrap_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wrap_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    tire_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tire_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tire_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    driver_has_remarks: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    driver_remarks_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    fp_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fp_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    photos: Mapped[list["InspectionPhoto"]] = relationship(
        back_populates="inspection", cascade="all, delete-orphan"
    )
    actions: Mapped[list["InspectionAction"]] = relationship(
        back_populates="inspection", cascade="all, delete-orphan"
    )
    damage_control_case: Mapped["DamageControlCase | None"] = relationship(
        back_populates="inspection", cascade="all, delete-orphan"
    )


class InspectionPhoto(Base):
    __tablename__ = "inspection_photos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inspection_id: Mapped[int] = mapped_column(ForeignKey("inspection_sessions.id"), index=True)
    photo_type: Mapped[str] = mapped_column(String(32), index=True)
    telegram_file_id: Mapped[str] = mapped_column(String(512))
    telegram_file_unique_id: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    inspection: Mapped[InspectionSession] = relationship(back_populates="photos")


class InspectionAction(Base):
    __tablename__ = "inspection_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inspection_id: Mapped[int] = mapped_column(ForeignKey("inspection_sessions.id"), index=True)
    action_type: Mapped[str] = mapped_column(String(64))
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    telegram_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    inspection: Mapped[InspectionSession] = relationship(back_populates="actions")


class KnownVehiclePlate(Base):
    __tablename__ = "known_vehicle_plates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plate_raw: Mapped[str] = mapped_column(String(32))
    plate_normalized: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    brand: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class BotUser(Base):
    __tablename__ = "bot_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    telegram_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class TireCheckCampaign(Base):
    __tablename__ = "tire_check_campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    applies_to_all: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by_user_id: Mapped[int] = mapped_column(BigInteger)
    created_by_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    plates: Mapped[list["TireCheckCampaignPlate"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )


class TireCheckCampaignPlate(Base):
    __tablename__ = "tire_check_campaign_plates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("tire_check_campaigns.id"), index=True)
    plate_raw: Mapped[str | None] = mapped_column(String(32), nullable=True)
    plate_normalized: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    inspection_id: Mapped[int | None] = mapped_column(ForeignKey("inspection_sessions.id"), nullable=True)

    campaign: Mapped[TireCheckCampaign] = relationship(back_populates="plates")


class DamageControlCase(Base):
    __tablename__ = "damage_control_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inspection_id: Mapped[int] = mapped_column(ForeignKey("inspection_sessions.id"), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    plate_normalized: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    fp_chat_id: Mapped[int] = mapped_column(BigInteger)
    fp_message_id: Mapped[int] = mapped_column(Integer)
    damage_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    reminders_sent: Mapped[int] = mapped_column(Integer, default=0)
    first_reminder_due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_reminder_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    service_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    service_request_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    service_request_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    service_received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    service_reminder_due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    waiting_comment_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    waiting_comment_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    close_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    close_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payment_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payment_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    inspection: Mapped[InspectionSession] = relationship(back_populates="damage_control_case")
