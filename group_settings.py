"""
Guruhlar, adminlar va timezone konfiguratsiyasi (.env orqali).

.env formati:
    GROUP_1_ID=-1001111111111
    GROUP_1_NAME=1-guruh              (ixtiyoriy)
    GROUP_1_ADMIN_IDS=123456789,987654321

    GROUP_2_ID=...
    ...

    TIMEZONE=Asia/Tashkent            (ixtiyoriy, default: Asia/Tashkent)
    STATS_DB_PATH=bot_stats.db        (ixtiyoriy)

Guruhlar soni cheklanmagan: GROUP_1, GROUP_2, ... GROUP_N.
Authorization FAQAT shu konfiguratsiya asosida ishlaydi.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import timedelta, timezone, tzinfo
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger("link_blocker_bot.group_settings")

DEFAULT_TIMEZONE = "Asia/Tashkent"
DEFAULT_DB_FILENAME = "bot_stats.db"

_GROUP_ID_KEY = re.compile(r"^GROUP_(\d+)_ID$")


@dataclass(frozen=True)
class GroupConfig:
    index: int                      # GROUP_<index>_...
    group_id: int                   # Telegram chat id
    name: str                       # Ko'rsatiladigan nom
    admin_ids: frozenset[int]       # Shu guruh statistikasini ko'ra oladiganlar


@dataclass(frozen=True)
class GroupSettings:
    groups: tuple[GroupConfig, ...] = ()
    tz: tzinfo = field(default_factory=lambda: timezone(timedelta(hours=5)))
    tz_name: str = DEFAULT_TIMEZONE
    db_path: Path = Path(DEFAULT_DB_FILENAME)

    # --- Qulay yordamchilar -------------------------------------------------

    @property
    def group_ids(self) -> frozenset[int]:
        return frozenset(g.group_id for g in self.groups)

    def is_tracked_group(self, chat_id: int) -> bool:
        return chat_id in self.group_ids

    def get_group(self, group_id: int) -> GroupConfig | None:
        for g in self.groups:
            if g.group_id == group_id:
                return g
        return None


def _parse_admin_ids(raw: str, key: str) -> frozenset[int]:
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            logger.error(f"{key}: noto'g'ri admin ID '{part}' — e'tiborsiz qoldirildi.")
            continue
        if value <= 0:
            logger.error(f"{key}: admin ID musbat bo'lishi kerak ('{part}') — e'tiborsiz qoldirildi.")
            continue
        ids.add(value)
    return frozenset(ids)


def _resolve_timezone(name: str) -> tuple[tzinfo, str]:
    try:
        return ZoneInfo(name), name
    except (ZoneInfoNotFoundError, ValueError) as e:
        logger.error(f"TIMEZONE='{name}' topilmadi ({e}). '{DEFAULT_TIMEZONE}' ishlatiladi.")
    try:
        return ZoneInfo(DEFAULT_TIMEZONE), DEFAULT_TIMEZONE
    except ZoneInfoNotFoundError:
        # Windows'da tzdata o'rnatilmagan bo'lsa. O'zbekistonda DST yo'q: UTC+5.
        logger.warning("tzdata topilmadi, UTC+05:00 ishlatiladi. `pip install tzdata` tavsiya etiladi.")
        return timezone(timedelta(hours=5), "UTC+05:00"), "UTC+05:00"


def load_group_settings(
    env: Mapping[str, str] | None = None,
    base_dir: Path | None = None,
) -> GroupSettings:
    """Muhit o'zgaruvchilaridan guruh sozlamalarini o'qiydi.

    Xato qiymatlar botni to'xtatmaydi: ular log qilinadi va o'sha guruh
    o'tkazib yuboriladi (mavjud link-bloklash funksiyasi ishlashda davom etadi).
    """
    env = os.environ if env is None else env
    base_dir = base_dir or Path(__file__).parent

    indexes = sorted(
        int(m.group(1)) for key in env if (m := _GROUP_ID_KEY.match(key))
    )

    groups: list[GroupConfig] = []
    seen_ids: set[int] = set()
    for idx in indexes:
        id_key = f"GROUP_{idx}_ID"
        raw_id = (env.get(id_key) or "").strip()
        if not raw_id:
            continue
        try:
            group_id = int(raw_id)
        except ValueError:
            logger.error(f"{id_key}='{raw_id}' butun son emas — guruh o'tkazib yuborildi.")
            continue
        if group_id in seen_ids:
            logger.error(f"{id_key}={group_id} boshqa GROUP_*_ID bilan takrorlangan — o'tkazib yuborildi.")
            continue

        admins_key = f"GROUP_{idx}_ADMIN_IDS"
        admin_ids = _parse_admin_ids(env.get(admins_key, ""), admins_key)
        if not admin_ids:
            logger.warning(f"{admins_key} bo'sh — {id_key} statistikasini hech kim ko'ra olmaydi.")

        name = (env.get(f"GROUP_{idx}_NAME") or "").strip() or f"{idx}-guruh"

        seen_ids.add(group_id)
        groups.append(GroupConfig(index=idx, group_id=group_id, name=name, admin_ids=admin_ids))

    tz, tz_name = _resolve_timezone((env.get("TIMEZONE") or DEFAULT_TIMEZONE).strip())

    db_raw = (env.get("STATS_DB_PATH") or "").strip()
    db_path = Path(db_raw) if db_raw else base_dir / DEFAULT_DB_FILENAME
    if not db_path.is_absolute():
        db_path = base_dir / db_path

    return GroupSettings(groups=tuple(groups), tz=tz, tz_name=tz_name, db_path=db_path)
