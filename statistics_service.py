"""
Guruh statistikasini hisoblash va formatlash.

Davrlar (TIMEZONE bo'yicha, default Asia/Tashkent):
  - Bugun     : bugun 00:00 dan
  - Shu hafta : dushanba 00:00 dan (ISO hafta, O'zbekistonda qabul qilingan)
  - Shu oy    : oyning 1-kuni 00:00 dan

Ta'riflar:
  - Jami a'zolar : Telegram API `getChatMemberCount` (real, joriy son)
  - Qo'shilgan   : davr ichida guruhga kirgan unique userlar (DB eventlari)
  - Chiqib ketgan: davr ichida guruhdan chiqqan unique userlar (DB eventlari)
  - Aktiv        : davr ichida kamida 1 ta xabar yozgan unique userlar
  - Aktiv emas   : Jami a'zolar - Aktiv (0 dan kichik bo'lmaydi)

Telegram Bot API guruhning barcha a'zolari ro'yxatini bermaydi, shuning uchun
"Aktiv emas" = jami son (API) - aktivlar (DB) ko'rinishida hisoblanadi.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

from aiogram import Bot

from database import PeriodCounts, StatsDatabase
from group_settings import GroupConfig

logger = logging.getLogger("link_blocker_bot.statistics")


@dataclass(frozen=True)
class PeriodBounds:
    today: int
    week: int
    month: int


@dataclass(frozen=True)
class GroupStats:
    group: GroupConfig
    total_members: int
    total_is_estimate: bool       # True -> API ishlamadi, DB'dagi taxminiy son
    today: PeriodCounts
    week: PeriodCounts
    month: PeriodCounts
    tracking_started_at: int | None
    generated_at: int


def period_bounds(tz: tzinfo, now: datetime | None = None) -> PeriodBounds:
    now = now.astimezone(tz) if now else datetime.now(tz)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = today - timedelta(days=today.weekday())  # Dushanba
    month = today.replace(day=1)
    return PeriodBounds(int(today.timestamp()), int(week.timestamp()), int(month.timestamp()))


async def fetch_member_count(bot: Bot, group_id: int) -> int | None:
    try:
        return await bot.get_chat_member_count(group_id)
    except Exception as e:
        logger.error(f"getChatMemberCount xatosi (group_id={group_id}): {e}")
        return None


async def build_group_stats(
    bot: Bot, db: StatsDatabase, group: GroupConfig, tz: tzinfo, now: datetime | None = None
) -> GroupStats:
    bounds = period_bounds(tz, now)
    raw = await db.get_raw_stats(group.group_id, bounds.today, bounds.week, bounds.month)

    total = await fetch_member_count(bot, group.group_id)
    is_estimate = total is None
    if total is None:
        total = await db.count_known_members(group.group_id)

    return GroupStats(
        group=group,
        total_members=total,
        total_is_estimate=is_estimate,
        today=raw.today,
        week=raw.week,
        month=raw.month,
        tracking_started_at=await db.get_tracking_started(group.group_id),
        generated_at=int(now.timestamp()) if now else int(time.time()),
    )


def _fmt(n: int) -> str:
    return f"{n:,}"


def _inactive(total: int, active: int) -> int:
    return max(total - active, 0)


def format_group_stats(stats: GroupStats, tz: tzinfo) -> str:
    t, w, m = stats.today, stats.week, stats.month
    total = stats.total_members
    total_line = f"👥 Jami a'zolar: {_fmt(total)}"
    if stats.total_is_estimate:
        total_line += " (taxminiy)"

    lines = [
        "📊 GURUH STATISTIKASI",
        "",
        f"🏷 Guruh: {stats.group.name}",
        "",
        total_line,
        "",
        "📅 Bugun",
        f"➕ Qo'shilgan: {_fmt(t.joined)}",
        f"➖ Chiqib ketgan: {_fmt(t.left)}",
        f"🔥 Aktiv: {_fmt(t.active)}",
        f"😴 Aktiv emas: {_fmt(_inactive(total, t.active))}",
        "",
        "📅 Shu hafta",
        f"➕ Qo'shilgan: {_fmt(w.joined)}",
        f"➖ Chiqib ketgan: {_fmt(w.left)}",
        f"🔥 Aktiv: {_fmt(w.active)}",
        f"😴 Aktiv emas: {_fmt(_inactive(total, w.active))}",
        "",
        "📅 Shu oy",
        f"➕ Qo'shilgan: {_fmt(m.joined)}",
        f"➖ Chiqib ketgan: {_fmt(m.left)}",
        f"🔥 Aktiv: {_fmt(m.active)}",
        f"😴 Aktiv emas: {_fmt(_inactive(total, m.active))}",
        "",
        f"🕒 Yangilangan: {datetime.fromtimestamp(stats.generated_at, tz):%d.%m.%Y %H:%M:%S}",
    ]
    if stats.tracking_started_at:
        started = datetime.fromtimestamp(stats.tracking_started_at, tz)
        lines.append(f"ℹ️ Ma'lumotlar {started:%d.%m.%Y %H:%M} dan beri yig'ilmoqda")
    return "\n".join(lines)
