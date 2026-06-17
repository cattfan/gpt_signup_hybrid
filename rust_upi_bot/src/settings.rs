//! SQLite Settings Store — đồng bộ project rule (single source of truth).
//!
//! Schema giống Python `db/repositories.py::SettingsRepository`. Hỗ trợ
//! get/set bằng key dot-namespace.

use anyhow::Result;
use rusqlite::{params, Connection};
use std::path::Path;

pub struct Settings {
    conn: Connection,
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
            "#,
        )?;
        Ok(Self { conn })
    }

    pub fn get(&self, key: &str) -> Option<String> {
        self.conn
            .query_row(
                "SELECT value FROM settings WHERE key = ?1",
                params![key],
                |r| r.get::<_, Option<String>>(0),
            )
            .ok()
            .flatten()
    }

    pub fn set(&self, key: &str, value: &str) -> Result<()> {
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES(?1, ?2)
             ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                            updated_at=CAST(strftime('%s','now') AS INTEGER)",
            params![key, value],
        )?;
        Ok(())
    }

    pub fn get_u32(&self, key: &str) -> Option<u32> {
        self.get(key).and_then(|s| s.parse().ok())
    }
}
