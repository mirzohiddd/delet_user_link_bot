"""Test yordamchilari: tarmoqsiz ishlaydigan soxta Telegram session."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, AsyncGenerator

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery, DeleteMessage, EditMessageText, GetChatMember,
    GetChatMemberCount, GetMe, SendMessage, TelegramMethod,
)
from aiogram.types import (
    CallbackQuery, Chat, ChatMemberAdministrator, ChatMemberLeft, ChatMemberMember,
    ChatMemberUpdated, Message, Update, User,
)

BOT_ID = 42


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod] = []
        self.member_count: dict[int, int] = {}
        self.admin_users: set[int] = set()
        self.delete_fails: bool = False

    def of(self, cls) -> list:
        return [r for r in self.requests if isinstance(r, cls)]

    async def close(self) -> None:
        pass

    async def stream_content(self, *a, **kw) -> AsyncGenerator[bytes, None]:  # pragma: no cover
        yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None) -> Any:
        self.requests.append(method)
        now = datetime.now(timezone.utc)
        if isinstance(method, GetMe):
            return User(id=BOT_ID, is_bot=True, first_name="Bot", username="test_bot")
        if isinstance(method, GetChatMember):
            u = User(id=method.user_id, is_bot=False, first_name="U")
            if method.user_id in self.admin_users:
                # aiogram versiyasiga bog'liq bo'lmasligi uchun barcha bool maydonlarni to'ldiramiz
                fields = {
                    name: True for name, f in ChatMemberAdministrator.model_fields.items()
                    if name.startswith("can_") or name == "is_anonymous"
                }
                fields["is_anonymous"] = False
                return ChatMemberAdministrator(user=u, **fields)
            return ChatMemberMember(user=u)
        if isinstance(method, GetChatMemberCount):
            return self.member_count.get(method.chat_id, 0)
        if isinstance(method, DeleteMessage):
            if self.delete_fails:
                raise TelegramBadRequest(method=method, message="Bad Request: message can't be deleted")
            return True
        if isinstance(method, SendMessage):
            return Message(message_id=1000, date=now, text=method.text,
                           chat=Chat(id=method.chat_id, type="private"))
        if isinstance(method, EditMessageText):
            return Message(message_id=method.message_id or 1, date=now, text=method.text,
                           chat=Chat(id=method.chat_id, type="private"))
        if isinstance(method, AnswerCallbackQuery):
            return True
        raise AssertionError(f"Kutilmagan method: {type(method).__name__}")


def user(uid: int, name: str = "User", is_bot: bool = False) -> User:
    return User(id=uid, is_bot=is_bot, first_name=name, username=f"u{uid}")


def group_chat(chat_id: int) -> Chat:
    return Chat(id=chat_id, type="supergroup", title="G")


_uid = iter(range(1, 10**9))


def upd(**kw) -> Update:
    return Update(update_id=next(_uid), **kw)


def group_message(chat_id: int, from_user: User, text: str | None = None,
                  date: datetime | None = None, **extra) -> Update:
    return upd(message=Message(
        message_id=next(_uid), date=date or datetime.now(timezone.utc),
        chat=group_chat(chat_id), from_user=from_user, text=text, **extra,
    ))


def private_start(from_user: User) -> Update:
    return upd(message=Message(
        message_id=next(_uid), date=datetime.now(timezone.utc),
        chat=Chat(id=from_user.id, type="private"), from_user=from_user, text="/start",
    ))


def callback(from_user: User, data: str, chat_type: str = "private") -> Update:
    chat = Chat(id=from_user.id, type=chat_type)
    msg = Message(message_id=500, date=datetime.now(timezone.utc), chat=chat,
                  from_user=User(id=BOT_ID, is_bot=True, first_name="Bot"), text="old")
    return upd(callback_query=CallbackQuery(
        id=str(next(_uid)), from_user=from_user, chat_instance="x", message=msg, data=data,
    ))


def chat_member_update(chat_id: int, u: User, joined: bool) -> Update:
    member = ChatMemberMember(user=u)
    left = ChatMemberLeft(user=u)
    return upd(chat_member=ChatMemberUpdated(
        chat=group_chat(chat_id), from_user=u, date=datetime.now(timezone.utc),
        old_chat_member=left if joined else member,
        new_chat_member=member if joined else left,
    ))
