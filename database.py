"""
Statistika uchun SQLite storage.

- Python'ning o'zidagi `sqlite3` ishlatiladi (yangi dependency yo'q).
- Har bir chaqiruv `asyncio.to_thread` orqali bajariladi, shuning uchun
  event loop (real-time bot) bloklanmaydi.
- WAL rejimi + indekslar: har bir xabarda bitta yengil UPSERT.
- Migrationlar `migrations/NNNN_*.sql` fayllaridan `PRAGMA user_version`
  asosida avtomatik qo'llanadi.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("link_blocker_bot.database")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_FILE = re.compile(r"^(\d{4})_.+\.sql$")

# Bir xil kirish/chiqish ikki manbadan (chat_member update va service message)
# kelishi mumkin. Shu oyna ichidagi takror event hisoblanmaydi.
DEDUP_WINDOW_SECONDS = 300


@dataclass(frozen=True)
class PeriodCounts:
    joined: int
    left: int
    active: int


@dataclass(frozen=True)
class RawGroupStats:
    today: PeriodCounts
    week: PeriodCounts
    month: PeriodCounts


class StatsDatabase:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ setup

    def open(self) -> None:
        if self._conn is not None:
            return
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        self._conn = conn
        self._apply_migrations()
        logger.info(f"Statistika database tayyor: {self.path}")

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _apply_migrations(self) -> None:
        assert self._conn is not None
        current = self._conn.execute("PRAGMA user_version").fetchone()[0]
        files = sorted(
            (int(m.group(1)), p)
            for p in MIGRATIONS_DIR.glob("*.sql")
            if (m := _MIGRATION_FILE.match(p.name))
        )
        for version, path in files:
            if version <= current:
                continue
            sql = path.read_text(encoding="utf-8")
            with self._lock:
                self._conn.executescript(
                    f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;"
                )
            logger.info(f"Migration qo'llandi: {path.name}")

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("StatsDatabase.open() chaqirilmagan.")
        return self._conn

    # ----------------------------------------------------------- sync (thread)

    def _ensure_groups(self, group_ids: Iterable[int], now: int) -> None:
        with self._lock, self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO tracked_groups (group_id, tracking_started_at) VALUES (?, ?)",
                [(gid, now) for gid in group_ids],
            )

    def _record_activity(
        self, group_id: int, user_id: int, username: str | None, first_name: str | None, ts: int
    ) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO group_members
                    (group_id, user_id, username, first_name, last_activity,
                     message_count, status, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, 'member', ?)
                ON CONFLICT (group_id, user_id) DO UPDATE SET
                    username      = excluded.username,
                    first_name    = excluded.first_name,
                    last_activity = excluded.last_activity,
                    message_count = group_members.message_count + 1,
                    status        = 'member',
                    updated_at    = excluded.updated_at
                """,
                (group_id, user_id, username, first_name, ts, ts),
            )

    def _record_membership(
        self,
        group_id: int,
        user_id: int,
        username: str | None,
        first_name: str | None,
        ts: int,
        joined: bool,
        source: str,
    ) -> bool:
        """Kirish (joined=True) yoki chiqishni yozadi. Yangi event yozilsa True."""
        new_status = "member" if joined else "left"
        time_col = "joined_at" if joined else "left_at"
        event_type = "join" if joined else "leave"

        with self._lock, self.conn:
            row = self.conn.execute(
                f"SELECT status, {time_col} AS t FROM group_members WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            ).fetchone()

            is_duplicate = (
                row is not None
                and row["status"] == new_status
                and row["t"] is not None
                and ts - row["t"] < DEDUP_WINDOW_SECONDS
            )

            if row is None:
                self.conn.execute(
                    f"""
                    INSERT INTO group_members
                        (group_id, user_id, username, first_name, {time_col}, status, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (group_id, user_id, username, first_name, ts, new_status, ts),
                )
            elif is_duplicate:
                self.conn.execute(
                    """
                    UPDATE group_members SET username = ?, first_name = ?, updated_at = ?
                    WHERE group_id = ? AND user_id = ?
                    """,
                    (username, first_name, ts, group_id, user_id),
                )
                return False
            else:
                self.conn.execute(
                    f"""
                    UPDATE group_members
                    SET username = ?, first_name = ?, {time_col} = ?, status = ?, updated_at = ?
                    WHERE group_id = ? AND user_id = ?
                    """,
                    (username, first_name, ts, new_status, ts, group_id, user_id),
                )

            self.conn.execute(
                """
                INSERT INTO member_events (group_id, user_id, event_type, source, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (group_id, user_id, event_type, source, ts),
            )
            return True

    def _get_raw_stats(self, group_id: int, today: int, week: int, month: int) -> RawGroupStats:
        since = min(today, week, month)
        with self._lock:
            ev = self.conn.execute(
                """
                SELECT
                    COUNT(DISTINCT CASE WHEN event_type='join'  AND created_at >= :t THEN user_id END) AS j_t,
                    COUNT(DISTINCT CASE WHEN event_type='leave' AND created_at >= :t THEN user_id END) AS l_t,
                    COUNT(DISTINCT CASE WHEN event_type='join'  AND created_at >= :w THEN user_id END) AS j_w,
                    COUNT(DISTINCT CASE WHEN event_type='leave' AND created_at >= :w THEN user_id END) AS l_w,
                    COUNT(DISTINCT CASE WHEN event_type='join'  AND created_at >= :m THEN user_id END) AS j_m,
                    COUNT(DISTINCT CASE WHEN event_type='leave' AND created_at >= :m THEN user_id END) AS l_m
                FROM member_events
                WHERE group_id = :g AND created_at >= :s
                """,
                {"g": group_id, "t": today, "w": week, "m": month, "s": since},
            ).fetchone()
            act = self.conn.execute(
                """
                SELECT
                    COALESCE(SUM(last_activity >= :t), 0) AS a_t,
                    COALESCE(SUM(last_activity >= :w), 0) AS a_w,
                    COALESCE(SUM(last_activity >= :m), 0) AS a_m
                FROM group_members
                WHERE group_id = :g AND last_activity >= :s
                """,
                {"g": group_id, "t": today, "w": week, "m": month, "s": since},
            ).fetchone()
        return RawGroupStats(
            today=PeriodCounts(ev["j_t"], ev["l_t"], act["a_t"]),
            week=PeriodCounts(ev["j_w"], ev["l_w"], act["a_w"]),
            month=PeriodCounts(ev["j_m"], ev["l_m"], act["a_m"]),
        )

    def _count_known_members(self, group_id: int) -> int:
        with self._lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM group_members WHERE group_id = ? AND status = 'member'",
                (group_id,),
            ).fetchone()[0]

    def _get_tracking_started(self, group_id: int) -> int | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT tracking_started_at FROM tracked_groups WHERE group_id = ?", (group_id,)
            ).fetchone()
        return row[0] if row else None

    # --------------------------------------------------------- async (public)

    async def ensure_groups(self, group_ids: Iterable[int], now: int) -> None:
        await asyncio.to_thread(self._ensure_groups, list(group_ids), now)

    async def record_activity(
        self, group_id: int, user_id: int, username: str | None, first_name: str | None, ts: int
    ) -> None:
        await asyncio.to_thread(self._record_activity, group_id, user_id, username, first_name, ts)

    async def record_join(
        self, group_id: int, user_id: int, username: str | None, first_name: str | None,
        ts: int, source: str,
    ) -> bool:
        return await asyncio.to_thread(
            self._record_membership, group_id, user_id, username, first_name, ts, True, source
        )

    async def record_leave(
        self, group_id: int, user_id: int, username: str | None, first_name: str | None,
        ts: int, source: str,
    ) -> bool:
        return await asyncio.to_thread(
            self._record_membership, group_id, user_id, username, first_name, ts, False, source
        )

    async def get_raw_stats(self, group_id: int, today: int, week: int, month: int) -> RawGroupStats:
        return await asyncio.to_thread(self._get_raw_stats, group_id, today, week, month)

    async def count_known_members(self, group_id: int) -> int:
        return await asyncio.to_thread(self._count_known_members, group_id)

    async def get_tracking_started(self, group_id: int) -> int | None:
        return await asyncio.to_thread(self._get_tracking_started, group_id)
