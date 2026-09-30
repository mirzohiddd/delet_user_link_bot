"""
Yangi funksiyalar (mavjud link-bloklash logikasiga tegmagan holda):

1. Kirish/chiqish service xabarlarini o'chirish ("joined", "left", "added").
2. Kirish/chiqishni database'ga yozish (chat_member update + service message).
3. Har bir xabarda last_activity yangilash (outer middleware, yengil UPSERT).
4. Admin /start -> faqat ruxsat berilgan guruh statistikasi, inline tugmalar.

Faqat .env'da GROUP_N_ID sifatida ko'rsatilgan guruhlar kuzatiladi.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import (
    JOIN_TRANSITION,
    LEAVE_TRANSITION,
    ChatMemberUpdatedFilter,
    CommandStart,
    Filter,
)
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
    User,
)

from admin_access import can_view_group, get_allowed_groups
from database import StatsDatabase
from group_settings import GroupConfig, GroupSettings
from statistics_service import build_group_stats, format_group_stats

logger = logging.getLogger("link_blocker_bot.group_features")

router = Router(name="group_stats")

NO_PERMISSION_TEXT = "❌ Sizda statistika ko‘rish uchun ruxsat yo‘q."
ACCESS_DENIED_TEXT = "❌ ACCESS DENIED"
CHOOSE_GROUP_TEXT = "📊 Guruhni tanlang:"


class StatsCallback(CallbackData, prefix="gst"):
    action: str        # "show" | "refresh" | "menu"
    group_id: int = 0


# ---------------------------------------------------------------------------
# Filterlar
# ---------------------------------------------------------------------------

class TrackedGroupFilter(Filter):
    """Event .env'da ko'rsatilgan guruhlardan biridan kelganini tekshiradi."""

    async def __call__(self, event: TelegramObject, group_settings: GroupSettings) -> bool:
        chat = getattr(event, "chat", None)
        return chat is not None and group_settings.is_tracked_group(chat.id)


def _is_trackable_user(user: User | None) -> bool:
    return user is not None and not user.is_bot


# ---------------------------------------------------------------------------
# Activity middleware (har bir guruh xabari uchun)
# ---------------------------------------------------------------------------

class ActivityMiddleware(BaseMiddleware):
    """Kuzatiladigan guruhdagi har bir real user xabari uchun last_activity'ni
    yangilaydi. Avval asosiy handler (link bloklash) ishlaydi, keyin bitta
    yengil UPSERT bajariladi. Xato bo'lsa ham bot ishlashda davom etadi."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        finally:
            await self._track(event, data)

    @staticmethod
    async def _track(event: TelegramObject, data: dict[str, Any]) -> None:
        try:
            if not isinstance(event, Message):
                return
            settings: GroupSettings | None = data.get("group_settings")
            db: StatsDatabase | None = data.get("stats_db")
            if settings is None or db is None or not settings.is_tracked_group(event.chat.id):
                return
            user = event.from_user
            if (
                not _is_trackable_user(user)
                or event.sender_chat is not None      # kanal / anonim admin nomidan
                or event.new_chat_members             # kirish service xabari
                or event.left_chat_member             # chiqish service xabari
            ):
                return
            await db.record_activity(
                event.chat.id, user.id, user.username, user.first_name, int(event.date.timestamp())
            )
        except Exception as e:
            logger.error(f"Activity yozishda xato: {e}")


# ---------------------------------------------------------------------------
# Kirish / chiqish: service xabarlar (o'chirish + yozish)
# ---------------------------------------------------------------------------

@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.new_chat_members | F.left_chat_member,
    TrackedGroupFilter(),
)
async def handle_join_leave_service_message(
    message: Message, stats_db: StatsDatabase
) -> None:
    chat_id = message.chat.id
    ts = int(message.date.timestamp())

    try:
        for user in message.new_chat_members or []:
            if _is_trackable_user(user):
                await stats_db.record_join(
                    chat_id, user.id, user.username, user.first_name, ts, "service_message"
                )
        left = message.left_chat_member
        if _is_trackable_user(left):
            await stats_db.record_leave(
                chat_id, left.id, left.username, left.first_name, ts, "service_message"
            )
    except Exception as e:
        logger.error(f"Kirish/chiqishni DB'ga yozishda xato (chat_id={chat_id}): {e}")

    kind = "joined" if message.new_chat_members else "left"
    try:
        await message.delete()
        logger.info(f"'{kind}' service xabari o'chirildi | chat_id={chat_id} | message_id={message.message_id}")
    except TelegramForbiddenError as e:
        logger.error(
            f"'{kind}' service xabarini o'chirib bo'lmadi: bot guruhda admin emas yoki guruhdan "
            f"chiqarilgan | chat_id={chat_id} | {e}"
        )
    except TelegramBadRequest as e:
        logger.error(
            f"'{kind}' service xabarini o'chirib bo'lmadi: botda 'Delete messages' huquqi yo'q "
            f"yoki xabar allaqachon o'chirilgan | chat_id={chat_id} | {e}"
        )
    except Exception as e:
        logger.error(f"'{kind}' service xabarini o'chirishda kutilmagan xato | chat_id={chat_id} | {e}")


# ---------------------------------------------------------------------------
# Kirish / chiqish: chat_member update (asosiy, ishonchli manba)
# ---------------------------------------------------------------------------

@router.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION), TrackedGroupFilter())
async def handle_member_joined(event: ChatMemberUpdated, stats_db: StatsDatabase) -> None:
    user = event.new_chat_member.user
    if not _is_trackable_user(user):
        return
    try:
        created = await stats_db.record_join(
            event.chat.id, user.id, user.username, user.first_name,
            int(event.date.timestamp()), "chat_member",
        )
        if created:
            logger.info(f"User kirdi | chat_id={event.chat.id} | user_id={user.id}")
    except Exception as e:
        logger.error(f"Join event yozishda xato (chat_id={event.chat.id}): {e}")


@router.chat_member(ChatMemberUpdatedFilter(LEAVE_TRANSITION), TrackedGroupFilter())
async def handle_member_left(event: ChatMemberUpdated, stats_db: StatsDatabase) -> None:
    user = event.old_chat_member.user
    if not _is_trackable_user(user):
        return
    try:
        created = await stats_db.record_leave(
            event.chat.id, user.id, user.username, user.first_name,
            int(event.date.timestamp()), "chat_member",
        )
        if created:
            logger.info(f"User chiqdi | chat_id={event.chat.id} | user_id={user.id}")
    except Exception as e:
        logger.error(f"Leave event yozishda xato (chat_id={event.chat.id}): {e}")


# ---------------------------------------------------------------------------
# Admin interfeysi: /start va inline tugmalar (faqat private chat)
# ---------------------------------------------------------------------------

def groups_keyboard(groups: list[GroupConfig]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"📊 {g.name}",
            callback_data=StatsCallback(action="show", group_id=g.group_id).pack(),
        )]
        for g in groups
    ])


def stats_keyboard(group_id: int, show_back: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text="🔄 Yangilash",
        callback_data=StatsCallback(action="refresh", group_id=group_id).pack(),
    )]]
    if show_back:
        rows.append([InlineKeyboardButton(
            text="⬅️ Guruhlar",
            callback_data=StatsCallback(action="menu").pack(),
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def render_stats(
    bot: Bot, db: StatsDatabase, settings: GroupSettings, group: GroupConfig, user_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    stats = await build_group_stats(bot, db, group, settings.tz)
    show_back = len(get_allowed_groups(settings, user_id)) > 1
    return format_group_stats(stats, settings.tz), stats_keyboard(group.group_id, show_back)


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def handle_start(
    message: Message, bot: Bot, group_settings: GroupSettings, stats_db: StatsDatabase
) -> None:
    user_id = message.from_user.id if message.from_user else None
    allowed = get_allowed_groups(group_settings, user_id)

    if not allowed:
        await message.answer(NO_PERMISSION_TEXT)
        return

    if len(allowed) == 1:
        try:
            text, kb = await render_stats(bot, stats_db, group_settings, allowed[0], user_id)
        except Exception as e:
            logger.error(f"Statistika hisoblashda xato (group_id={allowed[0].group_id}): {e}")
            await message.answer("⚠️ Statistikani hisoblashda xato yuz berdi. Keyinroq urinib ko'ring.")
            return
        await message.answer(text, reply_markup=kb)
        return

    await message.answer(CHOOSE_GROUP_TEXT, reply_markup=groups_keyboard(allowed))


@router.callback_query(StatsCallback.filter())
async def handle_stats_callback(
    callback: CallbackQuery,
    callback_data: StatsCallback,
    bot: Bot,
    group_settings: GroupSettings,
    stats_db: StatsDatabase,
) -> None:
    user_id = callback.from_user.id
    msg = callback.message

    # Faqat private chat (statistika hech qachon guruhga chiqmasligi uchun)
    if not isinstance(msg, Message) or msg.chat.type != ChatType.PRIVATE:
        await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
        return

    if callback_data.action == "menu":
        allowed = get_allowed_groups(group_settings, user_id)
        if not allowed:
            await _safe_edit(msg, NO_PERMISSION_TEXT, None)
        else:
            await _safe_edit(msg, CHOOSE_GROUP_TEXT, groups_keyboard(allowed))
        await callback.answer()
        return

    if callback_data.action not in ("show", "refresh"):
        await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
        return

    # MUHIM: callback data'ga ishonmaymiz — har safar .env bo'yicha tekshiramiz
    if not can_view_group(group_settings, user_id, callback_data.group_id):
        logger.warning(
            f"ACCESS DENIED: user_id={user_id} group_id={callback_data.group_id} statistikasini so'radi"
        )
        await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
        return

    group = group_settings.get_group(callback_data.group_id)
    try:
        text, kb = await render_stats(bot, stats_db, group_settings, group, user_id)
    except Exception as e:
        logger.error(f"Statistika hisoblashda xato (group_id={group.group_id}): {e}")
        await callback.answer("⚠️ Xato yuz berdi, keyinroq urinib ko'ring.", show_alert=True)
        return

    await _safe_edit(msg, text, kb)
    await callback.answer("✅ Yangilandi" if callback_data.action == "refresh" else None)


async def _safe_edit(msg: Message, text: str, kb: InlineKeyboardMarkup | None) -> None:
    try:
        await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise


# ---------------------------------------------------------------------------
# Ishga tushirish
# ---------------------------------------------------------------------------

def setup_group_features(dp: Dispatcher, settings: GroupSettings, db: StatsDatabase) -> None:
    """Dispatcher'ga yangi funksiyalarni ulaydi. Mavjud handlerlar o'zgarmaydi."""
    dp["group_settings"] = settings
    dp["stats_db"] = db
    dp.message.outer_middleware(ActivityMiddleware())
    dp.include_router(router)


async def on_startup_group_features(bot: Bot, settings: GroupSettings, db: StatsDatabase) -> None:
    """DB'da guruhlarni ro'yxatga oladi va bot huquqlarini tekshirib, logga yozadi."""
    if not settings.groups:
        logger.warning(
            "Hech qanday GROUP_N_ID sozlanmagan — statistika va join/leave o'chirish ishlamaydi. "
            "Link bloklash avvalgidek ishlayveradi."
        )
        return

    await db.ensure_groups(settings.group_ids, int(time.time()))
    logger.info(f"Timezone: {settings.tz_name} | Kuzatiladigan guruhlar: {len(settings.groups)}")

    try:
        me = await bot.get_me()
    except Exception as e:
        logger.error(f"get_me xatosi: {e}")
        return

    for g in settings.groups:
        try:
            member = await bot.get_chat_member(g.group_id, me.id)
        except Exception as e:
            logger.error(
                f"[{g.name}] group_id={g.group_id}: bot guruhga kira olmadi (ID noto'g'ri yoki bot "
                f"guruhda emas): {e}"
            )
            continue

        if member.status == ChatMemberStatus.CREATOR:
            logger.info(f"[{g.name}] group_id={g.group_id}: huquqlar OK")
        elif member.status == ChatMemberStatus.ADMINISTRATOR:
            if getattr(member, "can_delete_messages", False):
                logger.info(f"[{g.name}] group_id={g.group_id}: huquqlar OK")
            else:
                logger.error(
                    f"[{g.name}] group_id={g.group_id}: botda 'Delete messages' huquqi YO'Q — "
                    f"joined/left xabarlari o'chirilmaydi."
                )
        else:
            logger.error(
                f"[{g.name}] group_id={g.group_id}: bot ADMIN EMAS (status={member.status}) — "
                f"joined/left xabarlari o'chirilmaydi va kirish/chiqish updatelari kelmaydi."
            )
