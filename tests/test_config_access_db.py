"""Unit testlar: .env parsing, admin access, database, statistika."""

import asyncio
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from admin_access import can_view_group, get_allowed_groups
from database import DEDUP_WINDOW_SECONDS, StatsDatabase
from group_settings import load_group_settings
from statistics_service import GroupStats, format_group_stats, period_bounds
from database import PeriodCounts

G1, G2, G3 = -1001111111111, -1002222222222, -1003333333333
A1, A1B, A2, A3, MULTI, NOBODY = 111, 112, 222, 333, 999, 555

ENV = {
    "GROUP_1_ID": str(G1), "GROUP_1_NAME": "1-guruh", "GROUP_1_ADMIN_IDS": f"{A1}, {A1B},{MULTI}",
    "GROUP_2_ID": str(G2), "GROUP_2_ADMIN_IDS": f"{A2}",
    "GROUP_3_ID": str(G3), "GROUP_3_NAME": "3-guruh", "GROUP_3_ADMIN_IDS": f"{A3};{MULTI}",
    "TIMEZONE": "Asia/Tashkent",
}
TZ = ZoneInfo("Asia/Tashkent")


class TestGroupSettings(unittest.TestCase):
    def test_parse(self):
        s = load_group_settings(ENV, base_dir=Path("/tmp"))
        self.assertEqual([g.group_id for g in s.groups], [G1, G2, G3])
        self.assertEqual(s.get_group(G1).admin_ids, {A1, A1B, MULTI})
        self.assertEqual(s.get_group(G2).name, "2-guruh")        # default nom
        self.assertEqual(s.get_group(G3).admin_ids, {A3, MULTI})  # ';' ham qabul qilinadi
        self.assertEqual(s.tz_name, "Asia/Tashkent")
        self.assertEqual(s.db_path, Path("/tmp/bot_stats.db"))

    def test_invalid_values_do_not_crash(self):
        with self.assertLogs("link_blocker_bot.group_settings", "ERROR"):
            s = load_group_settings({
                "GROUP_1_ID": "abc", "GROUP_1_ADMIN_IDS": "1",
                "GROUP_2_ID": "-100", "GROUP_2_ADMIN_IDS": "x,5,-3",
                "GROUP_3_ID": "-100", "GROUP_3_ADMIN_IDS": "7",   # takror ID
                "TIMEZONE": "Mars/Base",
            })
        self.assertEqual([g.group_id for g in s.groups], [-100])
        self.assertEqual(s.groups[0].admin_ids, {5})
        self.assertEqual(s.tz_name, "Asia/Tashkent")

    def test_defaults_without_groups(self):
        s = load_group_settings({})
        self.assertEqual(s.groups, ())
        self.assertEqual(s.tz_name, "Asia/Tashkent")


class TestAdminAccessSecurity(unittest.TestCase):
    def setUp(self):
        self.s = load_group_settings(ENV)

    def test_1_group1_admin_sees_group1(self):
        self.assertEqual([g.group_id for g in get_allowed_groups(self.s, A1)], [G1])
        self.assertTrue(can_view_group(self.s, A1, G1))

    def test_2_group1_admin_denied_group2(self):
        self.assertFalse(can_view_group(self.s, A1, G2))
        self.assertFalse(can_view_group(self.s, A1, G3))

    def test_3_group2_admin_denied_group1(self):
        self.assertFalse(can_view_group(self.s, A2, G1))
        self.assertFalse(can_view_group(self.s, A2, G3))

    def test_4_regular_user_has_nothing(self):
        self.assertEqual(get_allowed_groups(self.s, NOBODY), [])
        self.assertEqual(get_allowed_groups(self.s, None), [])
        for g in (G1, G2, G3):
            self.assertFalse(can_view_group(self.s, NOBODY, g))

    def test_5_knowing_other_admin_id_gives_nothing(self):
        # G1 admini A2 ID'sini bilsa ham, authorization faqat O'Z user_id'si bo'yicha
        self.assertFalse(can_view_group(self.s, A1, G2))

    def test_multi_group_admin(self):
        self.assertEqual([g.group_id for g in get_allowed_groups(self.s, MULTI)], [G1, G3])
        self.assertFalse(can_view_group(self.s, MULTI, G2))

    def test_unknown_group(self):
        self.assertFalse(can_view_group(self.s, A1, -1009999))


class TestDatabase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = StatsDatabase(":memory:")
        self.db.open()

    async def asyncTearDown(self):
        self.db.close()

    def ts(self, y, mo, d, h=12, mi=0):
        return int(datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp())

    async def test_migration_version_and_idempotent(self):
        self.assertEqual(self.db.conn.execute("PRAGMA user_version").fetchone()[0], 1)
        self.db._apply_migrations()  # qayta chaqirish xavfsiz
        tables = {r[0] for r in self.db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"group_members", "member_events", "tracked_groups"} <= tables)
        cols = {r[1] for r in self.db.conn.execute("PRAGMA table_info(group_members)")}
        self.assertTrue({"group_id", "user_id", "username", "first_name", "joined_at",
                         "left_at", "last_activity", "status"} <= cols)

    async def test_join_leave_activity_fields(self):
        t = self.ts(2026, 9, 30)
        self.assertTrue(await self.db.record_join(G1, 1, "a", "A", t, "chat_member"))
        await self.db.record_activity(G1, 1, "a2", "A2", t + 10)
        await self.db.record_activity(G1, 1, "a2", "A2", t + 20)
        self.assertTrue(await self.db.record_leave(G1, 1, "a2", "A2", t + 30, "chat_member"))
        row = self.db.conn.execute("SELECT * FROM group_members WHERE group_id=? AND user_id=1", (G1,)).fetchone()
        self.assertEqual((row["joined_at"], row["last_activity"], row["left_at"]), (t, t + 20, t + 30))
        self.assertEqual((row["status"], row["message_count"], row["username"]), ("left", 2, "a2"))

    async def test_dedup_two_sources(self):
        t = self.ts(2026, 9, 30)
        self.assertTrue(await self.db.record_join(G1, 1, "a", "A", t, "chat_member"))
        self.assertFalse(await self.db.record_join(G1, 1, "a", "A", t + 1, "service_message"))
        self.assertTrue(await self.db.record_leave(G1, 1, "a", "A", t + 5, "service_message"))
        self.assertFalse(await self.db.record_leave(G1, 1, "a", "A", t + 6, "chat_member"))
        # Oynadan keyin qayta kirish — yangi event
        self.assertTrue(await self.db.record_join(G1, 1, "a", "A", t + DEDUP_WINDOW_SECONDS + 10, "chat_member"))
        n = self.db.conn.execute("SELECT COUNT(*) FROM member_events").fetchone()[0]
        self.assertEqual(n, 3)

    async def test_period_stats_and_group_isolation(self):
        # Chorshanba 30.09.2026. Hafta: 28.09 (dushanba). Oy: 01.09.
        today, week, month = self.ts(2026, 9, 30, 0), self.ts(2026, 9, 28, 0), self.ts(2026, 9, 1, 0)
        # G1
        await self.db.record_join(G1, 1, None, "a", self.ts(2026, 9, 30, 9), "chat_member")   # bugun
        await self.db.record_join(G1, 2, None, "b", self.ts(2026, 9, 29, 9), "chat_member")   # hafta
        await self.db.record_join(G1, 3, None, "c", self.ts(2026, 9, 10, 9), "chat_member")   # oy
        await self.db.record_join(G1, 4, None, "d", self.ts(2026, 8, 31, 23), "chat_member")  # o'tgan oy
        await self.db.record_leave(G1, 3, None, "c", self.ts(2026, 9, 30, 1), "chat_member")  # bugun
        await self.db.record_activity(G1, 1, None, "a", self.ts(2026, 9, 30, 10))
        await self.db.record_activity(G1, 2, None, "b", self.ts(2026, 9, 28, 0, 1))
        await self.db.record_activity(G1, 5, None, "e", self.ts(2026, 9, 15))
        await self.db.record_activity(G1, 6, None, "f", self.ts(2026, 8, 20))
        await self.db.record_activity(G1, 1, None, "a", self.ts(2026, 9, 30, 11))  # takror -> unique
        # G2 — ko'p userlar, G1 statistikasiga ta'sir qilmasligi kerak
        for uid in range(100, 150):
            await self.db.record_join(G2, uid, None, "x", self.ts(2026, 9, 30, 9), "chat_member")
            await self.db.record_activity(G2, uid, None, "x", self.ts(2026, 9, 30, 9))

        s1 = await self.db.get_raw_stats(G1, today, week, month)
        self.assertEqual((s1.today.joined, s1.today.left, s1.today.active), (1, 1, 1))
        self.assertEqual((s1.week.joined, s1.week.left, s1.week.active), (2, 1, 2))
        self.assertEqual((s1.month.joined, s1.month.left, s1.month.active), (3, 1, 3))

        s2 = await self.db.get_raw_stats(G2, today, week, month)
        self.assertEqual((s2.today.joined, s2.today.active), (50, 50))
        s3 = await self.db.get_raw_stats(G3, today, week, month)
        self.assertEqual((s3.month.joined, s3.month.left, s3.month.active), (0, 0, 0))

    async def test_week_crossing_month_boundary(self):
        # 02.09.2026 chorshanba: hafta 31.08 dan boshlanadi (oydan oldin)
        now = datetime(2026, 9, 2, 15, tzinfo=TZ)
        b = period_bounds(TZ, now)
        self.assertLess(b.week, b.month)
        await self.db.record_join(G1, 1, None, "a", self.ts(2026, 8, 31, 10), "chat_member")
        s = await self.db.get_raw_stats(G1, b.today, b.week, b.month)
        self.assertEqual((s.week.joined, s.month.joined), (1, 0))

    async def test_indexes_used(self):
        plan = " ".join(r[3] for r in self.db.conn.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM group_members WHERE group_id=? AND last_activity>=?", (G1, 0)))
        self.assertIn("idx_members_group_activity", plan)
        plan = " ".join(r[3] for r in self.db.conn.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM member_events WHERE group_id=? AND created_at>=?", (G1, 0)))
        self.assertIn("idx_events_group_time", plan)

    async def test_file_db_persists(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "s.db"
            db = StatsDatabase(p); db.open()
            await db.record_activity(G1, 1, None, "a", 100); db.close()
            db = StatsDatabase(p); db.open()
            self.assertEqual(await db.count_known_members(G1), 1); db.close()


class TestStatistics(unittest.TestCase):
    def test_period_bounds_tashkent(self):
        # UTC 30.09 20:30 = Toshkent 01.10 01:30 (yangi kun, yangi oy)
        b = period_bounds(TZ, datetime(2026, 9, 30, 20, 30, tzinfo=ZoneInfo("UTC")))
        self.assertEqual(datetime.fromtimestamp(b.today, TZ), datetime(2026, 10, 1, tzinfo=TZ))
        self.assertEqual(datetime.fromtimestamp(b.month, TZ), datetime(2026, 10, 1, tzinfo=TZ))
        self.assertEqual(datetime.fromtimestamp(b.week, TZ), datetime(2026, 9, 28, tzinfo=TZ))
        self.assertEqual(datetime.fromtimestamp(b.week, TZ).weekday(), 0)

    def test_format(self):
        from group_settings import GroupConfig
        g = GroupConfig(1, G1, "1-guruh", frozenset({A1}))
        st = GroupStats(g, 10482, False, PeriodCounts(127, 43, 2481), PeriodCounts(684, 211, 5932),
                        PeriodCounts(2341, 891, 8124), None, 1_790_000_000)
        text = format_group_stats(st, TZ)
        for line in ["📊 GURUH STATISTIKASI", "🏷 Guruh: 1-guruh", "👥 Jami a'zolar: 10,482",
                     "➕ Qo'shilgan: 127", "➖ Chiqib ketgan: 43", "🔥 Aktiv: 2,481",
                     "😴 Aktiv emas: 8,001", "📅 Shu hafta", "🔥 Aktiv: 5,932", "📅 Shu oy",
                     "➕ Qo'shilgan: 2,341", "🔥 Aktiv: 8,124"]:
            self.assertIn(line, text)

    def test_inactive_never_negative(self):
        from group_settings import GroupConfig
        g = GroupConfig(1, G1, "x", frozenset())
        st = GroupStats(g, 5, True, PeriodCounts(0, 0, 9), PeriodCounts(0, 0, 9),
                        PeriodCounts(0, 0, 9), None, 0)
        text = format_group_stats(st, TZ)
        self.assertIn("😴 Aktiv emas: 0", text)
        self.assertIn("(taxminiy)", text)


if __name__ == "__main__":
    unittest.main()
