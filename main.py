"""
Telegram Spam/Link Blocker Bot
--------------------------------
Kutubxona: aiogram 3.x
Vazifa: Guruhdagi xabarlarda link topilsa, avtomatik o'chiradi.
Admin va creator xabarlari o'chirilmaydi.

MUHIM: Botga guruhda "Delete Messages" (Xabarlarni o'chirish) ruxsati
berilishi SHART. Aks holda bot xabarlarni o'chira olmaydi.
Buning uchun: Guruh sozlamalari -> Administrators -> botni admin qiling
va "Delete messages" huquqini yoqing.
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType, ChatMemberStatus
from aiogram.types import Message
from aiogram.filters import Filter

# ---------------------------------------------------------------------------
# Sozlamalar
# ---------------------------------------------------------------------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN topilmadi! Iltimos .env faylida BOT_TOKEN=... qiymatini kiriting."
    )

CONFIG_PATH = Path(__file__).parent / "config.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("link_blocker_bot")

# ---------------------------------------------------------------------------
# JSON bilan ishlash (o'qish / yozish)
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "groups": [],
    "settings": {
        "delete_links": True
    },
    "whitelist": [],
    "logs": []
}


def load_config() -> dict:
    """config.json faylini o'qiydi. Agar fayl bo'lmasa yoki buzilgan bo'lsa,
    standart konfiguratsiyani yaratadi."""
    try:
        if not CONFIG_PATH.exists():
            save_config(DEFAULT_CONFIG)
            return json.loads(json.dumps(DEFAULT_CONFIG))

        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Kerakli kalitlar mavjudligini tekshirish
        for key, value in DEFAULT_CONFIG.items():
            if key not in data:
                data[key] = value

        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"config.json o'qishda xato: {e}. Standart config ishlatiladi.")
        return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(data: dict) -> None:
    """config.json fayliga yozadi."""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.error(f"config.json ga yozishda xato: {e}")


def add_log(event: str, chat_id: int, user_id: int | None = None, extra: str = "") -> None:
    """Log yozuvini config.json ga qo'shadi."""
    try:
        data = load_config()
        data["logs"].append({
            "time": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            "chat_id": chat_id,
            "user_id": user_id,
            "extra": extra,
        })
        # Loglar juda ko'payib ketmasligi uchun oxirgi 500 tasini saqlaymiz
        data["logs"] = data["logs"][-500:]
        save_config(data)
    except Exception as e:
        logger.error(f"Log yozishda xato: {e}")


def register_group(chat_id: int) -> None:
    """Yangi guruhni config.json ga qo'shadi (agar mavjud bo'lmasa)."""
    data = load_config()
    if chat_id not in data["groups"]:
        data["groups"].append(chat_id)
        save_config(data)
        logger.info(f"Yangi guruh ro'yxatga olindi: {chat_id}")


# ---------------------------------------------------------------------------
# Link aniqlash
# ---------------------------------------------------------------------------

LINK_PATTERN = re.compile(
    r"""(
        (https?://)                 # http:// yoki https://
        |
        (www\.)                     # www.
        |
        (t\.me/)                    # t.me/
        |
        (telegram\.me/)             # telegram.me/
        |
        (bit\.ly/)                  # bit.ly/
        |
        ([a-zA-Z0-9-]+\.(com|net|org|uz|ru|io|xyz|info|biz|me|tv|shop|club|online)\b)  # oddiy domenlar
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def contains_link(text: str | None) -> bool:
    """Berilgan matnda link bor-yo'qligini tekshiradi."""
    if not text:
        return False
    return bool(LINK_PATTERN.search(text))


def extract_message_text(message: Message) -> str:
    """Xabar matnini yoki captionini oladi, entity linklarni ham hisobga oladi."""
    text = message.text or message.caption or ""

    # Yashirin (masked) linklarni ham tekshirish uchun entitylarni tekshiramiz
    entities = message.entities or message.caption_entities or []
    for entity in entities:
        if entity.type in ("url", "text_link", "mention"):
            if entity.type == "text_link" and entity.url:
                text += " " + entity.url

    return text


# ---------------------------------------------------------------------------
# Admin / Creator tekshiruvi
# ---------------------------------------------------------------------------

async def is_admin_or_creator(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Foydalanuvchi admin yoki creator ekanligini tekshiradi."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        )
    except Exception as e:
        logger.warning(f"Admin tekshiruvida xato (chat_id={chat_id}, user_id={user_id}): {e}")
        return False


def is_whitelisted(user_id: int, config: dict) -> bool:
    """Foydalanuvchi whitelistda bor-yo'qligini tekshiradi."""
    return user_id in config.get("whitelist", [])


# ---------------------------------------------------------------------------
# Faqat guruh filtri
# ---------------------------------------------------------------------------

class GroupOnlyFilter(Filter):
    """Bot faqat guruh va supergruppalarda ishlashi uchun filter."""

    async def __call__(self, message: Message) -> bool:
        return message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


# ---------------------------------------------------------------------------
# Bot va Dispatcher
# ---------------------------------------------------------------------------

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dp.message(GroupOnlyFilter(), F.text | F.caption)
async def handle_group_message(message: Message) -> None:
    """Guruhdagi har bir matnli xabarni tekshiradi va kerak bo'lsa o'chiradi."""
    try:
        chat_id = message.chat.id
        user_id = message.from_user.id if message.from_user else None

        config = load_config()

        # Yangi guruhni avtomatik ro'yxatga olish
        register_group(chat_id)

        # Link bloklash o'chirilgan bo'lsa, hech narsa qilmaymiz
        if not config.get("settings", {}).get("delete_links", True):
            return

        # Admin / Creator xabarlarini o'chirmaslik
        if user_id and await is_admin_or_creator(bot, chat_id, user_id):
            return

        # Whitelistdagi foydalanuvchilarni o'chirmaslik
        if user_id and is_whitelisted(user_id, config):
            return

        text_to_check = extract_message_text(message)

        if contains_link(text_to_check):
            await message.delete()
            logger.info(
                f"Link topildi va o'chirildi | chat_id={chat_id} | user_id={user_id}"
            )
            add_log(
                event="link_deleted",
                chat_id=chat_id,
                user_id=user_id,
                extra=text_to_check[:200],
            )

    except Exception as e:
        # Xabarni o'chirish huquqi bo'lmasa yoki boshqa xato yuz bersa
        logger.error(f"Xabarni qayta ishlashda xato: {e}")
        add_log(
            event="error",
            chat_id=message.chat.id if message.chat else 0,
            user_id=message.from_user.id if message.from_user else None,
            extra=str(e),
        )


# ---------------------------------------------------------------------------
# Soxta HTTP server (faqat Render "Web Service" bepul rejasi uchun kerak)
# ---------------------------------------------------------------------------
# Render Web Service turi doim biror portda HTTP javob kutadi, aks holda
# xizmatni "ishlamayapti" deb hisoblab qayta-qayta o'chirib-yoqishi mumkin.
# Botning o'zi HTTP so'rov qabul qilmaydi (u faqat Telegram bilan ishlaydi),
# shuning uchun shu yerda alohida, juda oddiy HTTP server ishga tushiramiz —
# u faqat "OK" deb javob beradi, Render shuni ko'rib xizmatni tirik deb biladi.

from aiohttp import web


async def start_fake_web_server() -> None:
    async def handle_ping(request):
        return web.Response(text="Bot ishlab turibdi.")

    app = web.Application()
    app.router.add_get("/", handle_ping)

    port = int(os.getenv("PORT", 10000))  # Render PORT ni o'zi beradi

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()

    logger.info(f"Soxta HTTP server {port}-portda ishga tushdi (Render health check uchun).")


# ---------------------------------------------------------------------------
# Botni ishga tushirish
# ---------------------------------------------------------------------------

async def main() -> None:
    load_config()  # config.json mavjudligini ta'minlash

    logger.info("Bot ishga tushmoqda...")
    logger.info(
        "ESLATMA: Botga guruhda 'Delete Messages' admin huquqi berilishi shart, "
        "aks holda u linkli xabarlarni o'chira olmaydi."
    )

    try:
        # MUHIM: Portni ENG BIRINCHI bo'lib, hech narsaga to'sqinlik qilmasdan
        # ochamiz. Shunda Render uni darhol ko'radi, hatto Telegram bilan
        # ulanishda vaqtinchalik muammo (masalan Conflict xatosi) bo'lsa ham.
        try:
            await start_fake_web_server()
        except Exception as e:
            logger.error(f"Soxta HTTP serverni ochishda xato: {e}")

        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    except Exception as e:
        logger.critical(f"Botni ishga tushirishda xato: {e}")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot to'xtatildi.")