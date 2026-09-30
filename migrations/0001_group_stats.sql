-- Migration 0001: guruh a'zolari, kirish/chiqish eventlari va activity.
-- Barcha vaqtlar: UTC Unix timestamp (sekund). Timezone faqat hisoblashda qo'llanadi.

-- Kuzatuv qachon boshlangani (statistika shu sanadan beri yig'iladi)
CREATE TABLE IF NOT EXISTS tracked_groups (
    group_id            INTEGER PRIMARY KEY,
    tracking_started_at INTEGER NOT NULL
);

-- Har bir guruhdagi har bir user holati (group_id + user_id bo'yicha ajratilgan)
CREATE TABLE IF NOT EXISTS group_members (
    group_id       INTEGER NOT NULL,
    user_id        INTEGER NOT NULL,
    username       TEXT,
    first_name     TEXT,
    joined_at      INTEGER,
    left_at        INTEGER,
    last_activity  INTEGER,
    message_count  INTEGER NOT NULL DEFAULT 0,
    status         TEXT    NOT NULL DEFAULT 'member',   -- 'member' | 'left'
    updated_at     INTEGER NOT NULL,
    PRIMARY KEY (group_id, user_id)
);

-- "Aktiv" statistikasi uchun: WHERE group_id = ? AND last_activity >= ?
CREATE INDEX IF NOT EXISTS idx_members_group_activity
    ON group_members (group_id, last_activity);

-- Kirish/chiqish tarixi
CREATE TABLE IF NOT EXISTS member_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    event_type  TEXT    NOT NULL,                       -- 'join' | 'leave'
    source      TEXT    NOT NULL,                       -- 'chat_member' | 'service_message'
    created_at  INTEGER NOT NULL
);

-- "Qo'shilgan / Chiqib ketgan" statistikasi uchun
CREATE INDEX IF NOT EXISTS idx_events_group_time
    ON member_events (group_id, created_at, event_type);
