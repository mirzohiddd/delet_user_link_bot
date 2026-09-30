"""Integration testlar: main.py dagi haqiqiy Dispatcher orqali update'lar.

Tarmoq ishlatilmaydi (FakeSession). Mavjud link-bloklash funksiyasi va
yangi funksiyalar birga to'g'ri ishlashi tekshiriladi.
"""

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["BOT_TOKEN"] = "42:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"   # soxta token, .env'dan ustun
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram.methods import (  # noqa: E402
    AnswerCallbackQuery, DeleteMessage, EditMessageText, SendMessage,
)
from aiogram.types import Chat  # noqa: E402

from database import StatsDatabase  # noqa: E402
from group_features import StatsCallback, on_startup_group_features, setup_group_features  # noqa: E402
from group_settings import load_group_settings  # noqa: E402
from tests.helpers import (  # noqa: E402
    FakeSession, callback, chat_member_update, group_message, private_start, user,
)

G1, G2, G3, OTHER = -1001111111111, -1002222222222, -1003333333333, -1009999999999
A1, A2, A3, MULTI, NOBODY = 111, 222, 333, 999, 555

ENV = {
    "GROUP_1_ID": str(G1), "GROUP_1_NAME": "1-guruh", "GROUP_1_ADMIN_IDS": f"{A1},{MULTI}",
    "GROUP_2_ID": str(G2), "GROUP_2_NAME": "2-guruh", "GROUP_2_ADMIN_IDS": f"{A2}",
    "GROUP_3_ID": str(G3), "GROUP_3_NAME": "3-guruh", "GROUP_3_ADMIN_IDS": f"{A3},{MULTI}",
}

main = importlib.import_module("main")
_tmp = tempfile.TemporaryDirectory()
main.CONFIG_PATH = Path(_tmp.name) / "config.json"      # haqiqiy config.json ga tegmaymiz
SETTINGS = load_group_settings(ENV)
setup_group_features(main.dp, SETTINGS, StatsDatabase(":memory:"))


def cb(action, gid=0):
    return StatsCallback(action=action, group_id=gid).pack()


class BotFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = FakeSession()
        main.bot.session = self.session
        self.bot = main.bot
        self.db = StatsDatabase(":memory:")
        self.db.open()
        await self.db.ensure_groups(SETTINGS.group_ids, 0)
        main.dp["stats_db"] = self.db
        self.session.member_count = {G1: 100, G2: 200, G3: 300}

    async def asyncTearDown(self):
        self.db.close()

    async def feed(self, update):
        return await main.dp.feed_update(self.bot, update)

    def sent_texts(self):
        return [r.text for r in self.session.of(SendMessage)] + \
               [r.text for r in self.session.of(EditMessageText)]

    # ---------------------------------------------------- mavjud funksiyalar

    async def test_existing_link_blocking_still_works(self):
        await self.feed(group_message(G1, user(1), "qarang https://spam.com"))
        await self.feed(group_message(OTHER, user(2), "t.me/spam"))   # sozlanmagan guruh ham
        self.assertEqual(len(self.session.of(DeleteMessage)), 2)

    async def test_existing_admin_link_not_deleted(self):
        self.session.admin_users.add(7)
        await self.feed(group_message(G1, user(7), "https://ok.com"))
        self.assertEqual(self.session.of(DeleteMessage), [])

    async def test_normal_message_not_deleted(self):
        await self.feed(group_message(G1, user(1), "salom hammaga"))
        self.assertEqual(self.session.of(DeleteMessage), [])

    # ------------------------------------------------- join/leave o'chirish

    async def test_join_service_message_deleted_and_recorded(self):
        await self.feed(group_message(G1, user(10), new_chat_members=[user(10), user(11)]))
        self.assertEqual(len(self.session.of(DeleteMessage)), 1)
        rows = self.db.conn.execute(
            "SELECT user_id, status FROM group_members WHERE group_id=? ORDER BY user_id", (G1,)).fetchall()
        self.assertEqual([tuple(r) for r in rows], [(10, "member"), (11, "member")])
        # activity sifatida hisoblanmaydi
        self.assertIsNone(self.db.conn.execute(
            "SELECT last_activity FROM group_members WHERE user_id=10").fetchone()[0])

    async def test_left_service_message_deleted_and_recorded(self):
        await self.feed(group_message(G1, user(10), left_chat_member=user(10)))
        self.assertEqual(len(self.session.of(DeleteMessage)), 1)
        self.assertEqual(self.db.conn.execute(
            "SELECT status FROM group_members WHERE user_id=10").fetchone()[0], "left")

    async def test_service_message_in_untracked_group_untouched(self):
        await self.feed(group_message(OTHER, user(10), new_chat_members=[user(10)]))
        self.assertEqual(self.session.of(DeleteMessage), [])

    async def test_delete_permission_error_logged(self):
        self.session.delete_fails = True
        with self.assertLogs("link_blocker_bot.group_features", "ERROR") as cm:
            await self.feed(group_message(G1, user(10), new_chat_members=[user(10)]))
        self.assertIn("Delete messages", "\n".join(cm.output))

    async def test_chat_member_updates_dedup_with_service_message(self):
        await self.feed(chat_member_update(G1, user(20), joined=True))
        await self.feed(group_message(G1, user(20), new_chat_members=[user(20)]))
        await self.feed(chat_member_update(G1, user(20), joined=False))
        events = self.db.conn.execute(
            "SELECT event_type, source FROM member_events ORDER BY id").fetchall()
        self.assertEqual([tuple(e) for e in events], [("join", "chat_member"), ("leave", "chat_member")])

    # ------------------------------------------------------------ activity

    async def test_activity_tracking(self):
        await self.feed(group_message(G1, user(1), "salom"))
        await self.feed(group_message(G1, user(1), "yana"))
        await self.feed(group_message(G1, user(2), None, sticker=None, caption=None, photo=None))
        await self.feed(group_message(G1, user(3, is_bot=True), "men bot"))
        await self.feed(group_message(OTHER, user(4), "boshqa guruh"))
        await self.feed(group_message(G1, user(5), "kanaldan",
                                      sender_chat=Chat(id=-100777, type="channel")))
        rows = dict(self.db.conn.execute(
            "SELECT user_id, message_count FROM group_members WHERE group_id=?", (G1,)).fetchall())
        self.assertEqual(rows, {1: 2, 2: 1})
        self.assertEqual(self.db.conn.execute(
            "SELECT COUNT(*) FROM group_members WHERE group_id=?", (OTHER,)).fetchone()[0], 0)

    # ----------------------------------------------------- /start va access

    async def test_security_1_group1_admin_start(self):
        await self.feed(group_message(G1, user(1), "salom"))
        await self.feed(chat_member_update(G1, user(2), joined=True))
        await self.feed(private_start(user(A1)))
        text = self.session.of(SendMessage)[-1].text
        self.assertIn("🏷 Guruh: 1-guruh", text)
        self.assertIn("👥 Jami a'zolar: 100", text)
        self.assertIn("🔥 Aktiv: 1", text)
        self.assertIn("😴 Aktiv emas: 99", text)
        self.assertIn("➕ Qo'shilgan: 1", text)
        kb = self.session.of(SendMessage)[-1].reply_markup.inline_keyboard
        self.assertEqual(kb[0][0].text, "🔄 Yangilash")
        self.assertEqual(len(kb), 1)   # bitta guruh -> "Guruhlar" tugmasi yo'q

    async def test_security_2_group1_admin_requests_group2(self):
        await self.feed(callback(user(A1), cb("show", G2)))
        await self.feed(callback(user(A1), cb("refresh", G2)))
        answers = self.session.of(AnswerCallbackQuery)
        self.assertEqual([a.text for a in answers], ["❌ ACCESS DENIED"] * 2)
        self.assertEqual(self.session.of(EditMessageText), [])

    async def test_security_3_group2_admin_requests_group1(self):
        await self.feed(callback(user(A2), cb("show", G1)))
        self.assertEqual(self.session.of(AnswerCallbackQuery)[-1].text, "❌ ACCESS DENIED")
        self.assertEqual(self.session.of(EditMessageText), [])

    async def test_security_4_regular_user_start(self):
        await self.feed(private_start(user(NOBODY)))
        self.assertEqual(self.session.of(SendMessage)[-1].text,
                         "❌ Sizda statistika ko‘rish uchun ruxsat yo‘q.")
        await self.feed(callback(user(NOBODY), cb("menu")))
        self.assertIn("❌ Sizda statistika ko‘rish uchun ruxsat yo‘q.", self.sent_texts()[-1])

    async def test_security_5_forged_callback_with_other_admin_id(self):
        # G1 admini G2 adminining ID'sini bilsa ham, callback'da faqat group_id bor,
        # tekshiruv esa callback.from_user.id bo'yicha.
        for forged in (f"gst:show:{G2}", f"gst:refresh:{G2}:{A2}", f"gst:show:{G3}"):
            await self.feed(callback(user(A1), forged))
        self.assertEqual(self.session.of(EditMessageText), [])
        self.assertNotIn("2-guruh", " ".join(self.sent_texts()))

    async def test_multi_group_admin_selection(self):
        await self.feed(private_start(user(MULTI)))
        msg = self.session.of(SendMessage)[-1]
        self.assertEqual(msg.text, "📊 Guruhni tanlang:")
        buttons = [row[0].text for row in msg.reply_markup.inline_keyboard]
        self.assertEqual(buttons, ["📊 1-guruh", "📊 3-guruh"])

        await self.feed(callback(user(MULTI), cb("show", G3)))
        edit = self.session.of(EditMessageText)[-1]
        self.assertIn("🏷 Guruh: 3-guruh", edit.text)
        self.assertIn("👥 Jami a'zolar: 300", edit.text)
        kb = [row[0].text for row in edit.reply_markup.inline_keyboard]
        self.assertEqual(kb, ["🔄 Yangilash", "⬅️ Guruhlar"])

        await self.feed(callback(user(MULTI), cb("show", G2)))
        self.assertEqual(self.session.of(AnswerCallbackQuery)[-1].text, "❌ ACCESS DENIED")

        await self.feed(callback(user(MULTI), cb("menu")))
        self.assertEqual(self.session.of(EditMessageText)[-1].text, "📊 Guruhni tanlang:")

    async def test_refresh_recalculates(self):
        await self.feed(private_start(user(A1)))
        self.assertIn("🔥 Aktiv: 0", self.session.of(SendMessage)[-1].text)
        await self.feed(group_message(G1, user(1), "salom"))
        await self.feed(group_message(G1, user(2), "salom"))
        self.session.member_count[G1] = 150
        await self.feed(callback(user(A1), cb("refresh", G1)))
        edit = self.session.of(EditMessageText)[-1].text
        self.assertIn("🔥 Aktiv: 2", edit)
        self.assertIn("👥 Jami a'zolar: 150", edit)
        self.assertIn("😴 Aktiv emas: 148", edit)
        self.assertEqual(self.session.of(AnswerCallbackQuery)[-1].text, "✅ Yangilandi")

    async def test_stats_isolation_between_groups(self):
        for uid in range(1, 21):
            await self.feed(group_message(G2, user(uid), "g2"))
        await self.feed(group_message(G1, user(500), "g1"))
        await self.feed(private_start(user(A1)))
        text = self.session.of(SendMessage)[-1].text
        self.assertIn("🔥 Aktiv: 1\n", text)
        self.assertNotIn("2-guruh", text)

    async def test_start_in_group_does_not_leak_stats(self):
        await self.feed(group_message(G1, user(A1), "/start"))
        self.assertEqual(self.session.of(SendMessage), [])

    async def test_callback_from_group_chat_denied(self):
        await self.feed(callback(user(A1), cb("show", G1), chat_type="supergroup"))
        self.assertEqual(self.session.of(AnswerCallbackQuery)[-1].text, "❌ ACCESS DENIED")
        self.assertEqual(self.session.of(EditMessageText), [])

    async def test_startup_permission_check_logs(self):
        self.session.admin_users.add(42)   # bot faqat G1..G3 da admin sifatida qaytadi
        with self.assertLogs("link_blocker_bot.group_features", "INFO") as cm:
            await on_startup_group_features(self.bot, SETTINGS, self.db)
        self.assertEqual(sum("huquqlar OK" in l for l in cm.output), 3)

        self.session.admin_users.clear()
        with self.assertLogs("link_blocker_bot.group_features", "ERROR") as cm:
            await on_startup_group_features(self.bot, SETTINGS, self.db)
        self.assertEqual(sum("ADMIN EMAS" in l for l in cm.output), 3)

    async def test_allowed_updates_include_chat_member(self):
        used = main.dp.resolve_used_update_types()
        for t in ("message", "chat_member", "callback_query"):
            self.assertIn(t, used)


if __name__ == "__main__":
    unittest.main()
