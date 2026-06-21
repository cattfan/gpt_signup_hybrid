//! SQLite Settings Store — đồng bộ project rule (single source of truth).
//!
//! Ngoài bảng `settings` (key/value dot-namespace, schema giống Python
//! `db/repositories.py::SettingsRepository`), store còn giữ:
//!   - `bot_users`  — mọi user đã kết nối bot (nguồn cho broadcast `/notify`).
//!   - `bot_bans`   — danh sách ban theo `user_id` (định danh bền vững, user
//!                    đổi username vẫn dính ban).
//!
//! `Connection` của rusqlite là `Send` nhưng không `Sync`; bọc trong
//! `Mutex<Connection>` để `Arc<Settings>` chia sẻ được giữa các tokio task.
//! Mọi method DB sync + ngắn, không giữ guard qua `.await` → an toàn trong async.

use anyhow::Result;
use rusqlite::{params, Connection, OptionalExtension};
use std::path::Path;
use std::sync::Mutex;

/// Settings key cho login proxy global (admin-only). Dot-namespace theo rule.
const LOGIN_PROXY_KEY: &str = "proxy.login";

/// 1 dòng trong bảng `bot_bans`, dùng để render `/banlist`.
#[derive(Debug, Clone)]
pub struct BanRecord {
    pub user_id: i64,
    pub username: Option<String>,
    pub reason: Option<String>,
    #[allow(dead_code)]
    pub banned_at: i64,
}

pub struct Settings {
    conn: Mutex<Connection>,
}

impl Settings {
    pub fn open(path: &Path) -> Result<Self> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let conn = Connection::open(path)?;
        conn.execute_batch(
            r#"
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
            );

            CREATE TABLE IF NOT EXISTS bot_users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                first_name TEXT,
                chat_id    INTEGER NOT NULL,
                first_seen INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                last_seen  INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
            );
            CREATE INDEX IF NOT EXISTS idx_bot_users_username
                ON bot_users(username COLLATE NOCASE);

            CREATE TABLE IF NOT EXISTS bot_bans (
                user_id   INTEGER PRIMARY KEY,
                username  TEXT,
                reason    TEXT,
                banned_by INTEGER,
                banned_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
            );

            -- Per-user proxy override. user_id PK → 1 user = 1 proxy line. Lưu
            -- raw line/template (host:port[:user[:pass]] hoặc scheme URL) — mọi
            -- consumer phải materialize trước khi feed reqwest. Proxy của user
            -- KHÔNG share sang user khác (private per-user store).
            CREATE TABLE IF NOT EXISTS user_proxies (
                user_id INTEGER PRIMARY KEY,
                raw     TEXT NOT NULL,
                set_at  INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
            );

            -- Ngôn ngữ giao diện theo user (vi/en). User phải chọn lần đầu.
            CREATE TABLE IF NOT EXISTS user_lang (
                user_id INTEGER PRIMARY KEY,
                lang    TEXT NOT NULL,
                set_at  INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
            );
            "#,
        )?;
        Ok(Self {
            conn: Mutex::new(conn),
        })
    }

    /// Lock helper. Với `panic = "abort"` (release profile) poisoning không thể
    /// xảy ra; `expect` giữ đúng tinh thần fail-fast nếu gặp ở debug.
    fn lock(&self) -> std::sync::MutexGuard<'_, Connection> {
        self.conn.lock().expect("settings mutex poisoned")
    }

    // ── settings key/value ────────────────────────────────────────────────
    pub fn get(&self, key: &str) -> Option<String> {
        self.lock()
            .query_row(
                "SELECT value FROM settings WHERE key = ?1",
                params![key],
                |r| r.get::<_, Option<String>>(0),
            )
            .ok()
            .flatten()
    }

    pub fn set(&self, key: &str, value: &str) -> Result<()> {
        self.lock().execute(
            "INSERT INTO settings(key, value) VALUES(?1, ?2)
             ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                            updated_at=CAST(strftime('%s','now') AS INTEGER)",
            params![key, value],
        )?;
        Ok(())
    }

    #[allow(dead_code)]
    pub fn get_u32(&self, key: &str) -> Option<u32> {
        self.get(key).and_then(|s| s.parse().ok())
    }

    // ── login proxy (admin-only, global) ──────────────────────────────────
    /// Raw login proxy line do admin set (key `proxy.login`). None nếu chưa set
    /// hoặc rỗng. Mọi consumer PHẢI materialize trước khi feed client.
    pub fn get_login_proxy(&self) -> Option<String> {
        self.get(LOGIN_PROXY_KEY)
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
    }

    /// Set login proxy raw line (caller PHẢI validate qua
    /// `proxy_format::validate_and_mask` trước).
    pub fn set_login_proxy(&self, raw: &str) -> Result<()> {
        self.set(LOGIN_PROXY_KEY, raw)
    }

    /// Xóa login proxy. Trả `true` nếu có row bị xóa.
    pub fn remove_login_proxy(&self) -> Result<bool> {
        let n = self.lock().execute(
            "DELETE FROM settings WHERE key = ?1",
            params![LOGIN_PROXY_KEY],
        )?;
        Ok(n > 0)
    }

    // ── bot_users ───────────────────────────────────────────────────────
    /// Upsert user mỗi lần nhận message. `username`/`first_name` cập nhật theo
    /// lần gần nhất; `user_id` là khóa bền vững. `first_seen` giữ nguyên.
    pub fn record_user(
        &self,
        user_id: i64,
        username: Option<&str>,
        first_name: Option<&str>,
        chat_id: i64,
    ) -> Result<()> {
        self.lock().execute(
            "INSERT INTO bot_users(user_id, username, first_name, chat_id)
             VALUES(?1, ?2, ?3, ?4)
             ON CONFLICT(user_id) DO UPDATE SET
                 username   = excluded.username,
                 first_name = excluded.first_name,
                 chat_id    = excluded.chat_id,
                 last_seen  = CAST(strftime('%s','now') AS INTEGER)",
            params![user_id, username, first_name, chat_id],
        )?;
        Ok(())
    }

    /// Resolve username (có/không có '@', case-insensitive) → user_id từ
    /// `bot_users`. None nếu user chưa từng kết nối bot.
    pub fn resolve_username(&self, username: &str) -> Result<Option<i64>> {
        let u = username.trim_start_matches('@');
        let id = self
            .lock()
            .query_row(
                "SELECT user_id FROM bot_users WHERE username = ?1 COLLATE NOCASE",
                params![u],
                |r| r.get::<_, i64>(0),
            )
            .optional()?;
        Ok(id)
    }

    /// Username gần nhất biết được của 1 user_id (cho hiển thị).
    pub fn known_username(&self, user_id: i64) -> Result<Option<String>> {
        let name = self
            .lock()
            .query_row(
                "SELECT username FROM bot_users WHERE user_id = ?1",
                params![user_id],
                |r| r.get::<_, Option<String>>(0),
            )
            .optional()?
            .flatten();
        Ok(name)
    }

    /// chat_id đã lưu của user (cho DM trực tiếp). None nếu user chưa từng
    /// kết nối bot. Với private chat, chat_id thường == user_id nhưng vẫn đọc
    /// từ store để chính xác (group/forwarded edge case).
    pub fn chat_id_for_user(&self, user_id: i64) -> Result<Option<i64>> {
        let cid = self
            .lock()
            .query_row(
                "SELECT chat_id FROM bot_users WHERE user_id = ?1",
                params![user_id],
                |r| r.get::<_, i64>(0),
            )
            .optional()?;
        Ok(cid)
    }

    /// (user_id, chat_id) của mọi user KHÔNG bị ban — danh sách đích broadcast.
    pub fn broadcast_targets(&self) -> Result<Vec<(i64, i64)>> {
        let conn = self.lock();
        let mut stmt = conn.prepare(
            "SELECT user_id, chat_id FROM bot_users
             WHERE user_id NOT IN (SELECT user_id FROM bot_bans)
             ORDER BY user_id",
        )?;
        let rows = stmt.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?)))?;
        let mut out = Vec::new();
        for r in rows {
            out.push(r?);
        }
        Ok(out)
    }

    /// Gỡ user khỏi `bot_users` — gọi khi broadcast phát hiện user đã block bot
    /// (Telegram trả "bot was blocked" / "user is deactivated").
    pub fn remove_user(&self, user_id: i64) -> Result<()> {
        self.lock()
            .execute("DELETE FROM bot_users WHERE user_id = ?1", params![user_id])?;
        Ok(())
    }

    // ── bot_bans ──────────────────────────────────────────────────────────
    pub fn is_banned(&self, user_id: i64) -> Result<bool> {
        let n: i64 = self.lock().query_row(
            "SELECT COUNT(*) FROM bot_bans WHERE user_id = ?1",
            params![user_id],
            |r| r.get(0),
        )?;
        Ok(n > 0)
    }

    /// Ban theo user_id (idempotent upsert). `username` chỉ lưu để hiển thị —
    /// định danh ban là user_id nên đổi username không thoát ban.
    pub fn ban(
        &self,
        user_id: i64,
        username: Option<&str>,
        reason: Option<&str>,
        banned_by: i64,
    ) -> Result<()> {
        self.lock().execute(
            "INSERT INTO bot_bans(user_id, username, reason, banned_by)
             VALUES(?1, ?2, ?3, ?4)
             ON CONFLICT(user_id) DO UPDATE SET
                 username  = excluded.username,
                 reason    = excluded.reason,
                 banned_by = excluded.banned_by,
                 banned_at = CAST(strftime('%s','now') AS INTEGER)",
            params![user_id, username, reason, banned_by],
        )?;
        Ok(())
    }

    /// Gỡ ban theo user_id. Trả true nếu có row bị xóa.
    pub fn unban(&self, user_id: i64) -> Result<bool> {
        let n = self
            .lock()
            .execute("DELETE FROM bot_bans WHERE user_id = ?1", params![user_id])?;
        Ok(n > 0)
    }

    /// Resolve username → user_id từ chính bảng ban (fallback cho `/unban` khi
    /// user đã rời `bot_users` hoặc đổi username sau khi bị ban).
    pub fn banned_user_id_by_username(&self, username: &str) -> Result<Option<i64>> {
        let u = username.trim_start_matches('@');
        let id = self
            .lock()
            .query_row(
                "SELECT user_id FROM bot_bans WHERE username = ?1 COLLATE NOCASE",
                params![u],
                |r| r.get::<_, i64>(0),
            )
            .optional()?;
        Ok(id)
    }

    pub fn list_bans(&self) -> Result<Vec<BanRecord>> {
        let conn = self.lock();
        let mut stmt = conn.prepare(
            "SELECT user_id, username, reason, banned_at
             FROM bot_bans ORDER BY banned_at DESC",
        )?;
        let rows = stmt.query_map([], |r| {
            Ok(BanRecord {
                user_id: r.get(0)?,
                username: r.get(1)?,
                reason: r.get(2)?,
                banned_at: r.get(3)?,
            })
        })?;
        let mut out = Vec::new();
        for r in rows {
            out.push(r?);
        }
        Ok(out)
    }

    // ── user_proxies ──────────────────────────────────────────────────────
    /// Set proxy raw line cho user (upsert). Caller PHẢI validate qua
    /// `proxy_format::validate_and_mask` trước. `raw` lưu nguyên (template/SID
    /// cho rotate sticky session ở consumer).
    pub fn set_user_proxy(&self, user_id: i64, raw: &str) -> Result<()> {
        self.lock().execute(
            "INSERT INTO user_proxies(user_id, raw) VALUES(?1, ?2)
             ON CONFLICT(user_id) DO UPDATE SET
                 raw    = excluded.raw,
                 set_at = CAST(strftime('%s','now') AS INTEGER)",
            params![user_id, raw],
        )?;
        Ok(())
    }

    /// Lấy raw proxy line của user (None nếu chưa set).
    pub fn get_user_proxy(&self, user_id: i64) -> Result<Option<String>> {
        let raw = self
            .lock()
            .query_row(
                "SELECT raw FROM user_proxies WHERE user_id = ?1",
                params![user_id],
                |r| r.get::<_, String>(0),
            )
            .optional()?;
        Ok(raw)
    }

    /// Xóa proxy của user. Trả `true` nếu có row bị xóa.
    pub fn remove_user_proxy(&self, user_id: i64) -> Result<bool> {
        let n = self
            .lock()
            .execute("DELETE FROM user_proxies WHERE user_id = ?1", params![user_id])?;
        Ok(n > 0)
    }

    // ── user_lang ─────────────────────────────────────────────────────────
    /// Ngôn ngữ đã chọn của user ("vi"/"en"). None nếu chưa chọn lần nào.
    pub fn get_user_lang(&self, user_id: i64) -> Option<String> {
        self.lock()
            .query_row(
                "SELECT lang FROM user_lang WHERE user_id = ?1",
                params![user_id],
                |r| r.get::<_, String>(0),
            )
            .optional()
            .ok()
            .flatten()
    }

    /// Đặt ngôn ngữ cho user (upsert).
    pub fn set_user_lang(&self, user_id: i64, lang: &str) -> Result<()> {
        self.lock().execute(
            "INSERT INTO user_lang(user_id, lang) VALUES(?1, ?2)
             ON CONFLICT(user_id) DO UPDATE SET
                 lang   = excluded.lang,
                 set_at = CAST(strftime('%s','now') AS INTEGER)",
            params![user_id, lang],
        )?;
        Ok(())
    }
}
