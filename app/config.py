from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def normalize_telegram_chat_id(value: str | int) -> int:
    chat_id = int(value)
    text = str(chat_id)
    if chat_id > 0 and text.startswith("100"):
        return -chat_id
    return chat_id


def optional_int_env(name: str) -> int | None:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return None
    try:
        return int(raw_value)
    except ValueError:
        return None


@dataclass(frozen=True)
class Settings:
    bot_token: str
    fp_chat_id: int
    database_url: str
    supervisor_username: str
    inspection_staff_usernames: set[str]
    data_dir: Path
    vehicle_plates_xlsx: Path | None
    plate_audit_enabled: bool = True
    plate_audit_hour: int = 4
    supervisor_telegram_id: int | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        data_dir = Path(os.getenv("DATA_DIR", "/app/data"))
        plates_path = os.getenv("VEHICLE_PLATES_XLSX", "").strip()
        staff = {
            username.strip().lstrip("@").lower()
            for username in os.getenv("INSPECTION_STAFF_USERNAMES", "").split(",")
            if username.strip()
        }
        return cls(
            bot_token=os.getenv("BOT_TOKEN", ""),
            fp_chat_id=normalize_telegram_chat_id(os.getenv("FP_CHAT_ID", "-1001905865504")),
            database_url=os.getenv("DATABASE_URL", "sqlite+aiosqlite:///bot.db"),
            supervisor_username=os.getenv("SUPERVISOR_USERNAME", "Fedos_AV").lstrip("@").lower(),
            inspection_staff_usernames=staff,
            data_dir=data_dir,
            vehicle_plates_xlsx=Path(plates_path) if plates_path else None,
            plate_audit_enabled=os.getenv("PLATE_AUDIT_ENABLED", "true").strip().lower()
            not in {"0", "false", "no", "off"},
            plate_audit_hour=int(os.getenv("PLATE_AUDIT_HOUR", "4")),
            supervisor_telegram_id=optional_int_env("SUPERVISOR_TELEGRAM_ID"),
        )
