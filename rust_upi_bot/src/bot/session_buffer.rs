//! Buffer ghép nhiều chunks text thành 1 session JSON hoàn chỉnh.
//!
//! Telegram split message dài (> ~4096 chars) thành nhiều message liên tiếp.
//! Buffer chờ tối đa `ttl` kể từ chunk đầu tiên, try parse JSON sau mỗi chunk.
//! Khi parse OK → `Ready(raw)`. Pattern mượn từ `fork/.../rust-bot/session_buffer.rs`.

use std::collections::HashMap;
use std::time::{Duration, Instant};

pub struct SessionBuffer {
    entries: HashMap<i64, BufferEntry>,
    ttl: Duration,
}

struct BufferEntry {
    chunks: Vec<String>,
    first_at: Instant,
}

#[derive(Debug)]
pub enum AppendResult {
    /// JSON parse OK — trả raw string ghép từ tất cả chunks.
    Ready(String),
    /// Chưa parse được, đang chờ chunk tiếp (im lặng).
    Pending,
    /// Parse được nhưng không phải JSON object (primitive) → user nhầm.
    Invalid(String),
}

impl SessionBuffer {
    pub fn new(ttl_secs: u64) -> Self {
        Self {
            entries: HashMap::new(),
            ttl: Duration::from_secs(ttl_secs),
        }
    }

    pub fn append(&mut self, chat_id: i64, chunk: &str) -> AppendResult {
        let chunk = chunk
            .trim_start_matches('\u{FEFF}') // BOM
            .trim_start_matches('\u{200B}') // zero-width space
            .trim_start_matches('\u{200C}') // zero-width non-joiner
            .trim_start_matches('\u{200D}') // zero-width joiner
            .trim();
        if chunk.is_empty() {
            return AppendResult::Pending;
        }

        let now = Instant::now();
        if let Some(entry) = self.entries.get(&chat_id) {
            if now.duration_since(entry.first_at) > self.ttl {
                self.entries.remove(&chat_id);
            }
        }

        let entry = self.entries.entry(chat_id).or_insert_with(|| BufferEntry {
            chunks: Vec::new(),
            first_at: now,
        });
        entry.chunks.push(chunk.to_string());

        let joined: String = entry.chunks.join("");
        let trimmed = joined.trim();

        match serde_json::from_str::<serde_json::Value>(trimmed) {
            Ok(val) => {
                self.entries.remove(&chat_id);
                if val.is_object() {
                    AppendResult::Ready(trimmed.to_string())
                } else {
                    AppendResult::Invalid("text không phải JSON object".into())
                }
            }
            Err(_) => AppendResult::Pending,
        }
    }

    pub fn clear(&mut self, chat_id: i64) {
        self.entries.remove(&chat_id);
    }

    /// Vacuum entries quá hạn (gọi định kỳ).
    pub fn vacuum(&mut self) {
        let now = Instant::now();
        let ttl = self.ttl;
        self.entries
            .retain(|_, e| now.duration_since(e.first_at) <= ttl);
    }

    pub fn pending_count(&self) -> usize {
        self.entries.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn append_until_ready() {
        let mut buf = SessionBuffer::new(60);
        let part1 = r#"{"user":{"email":"a@b.c"},"#;
        let part2 = r#""accessToken":"xyz"}"#;
        match buf.append(1, part1) {
            AppendResult::Pending => {}
            other => panic!("expected Pending, got {:?}", other),
        }
        match buf.append(1, part2) {
            AppendResult::Ready(s) => {
                assert!(s.contains("accessToken"));
                let v: serde_json::Value = serde_json::from_str(&s).unwrap();
                assert_eq!(v["user"]["email"], "a@b.c");
                assert_eq!(v["accessToken"], "xyz");
            }
            other => panic!("expected Ready, got {:?}", other),
        }
        assert_eq!(buf.pending_count(), 0);
    }

    #[test]
    fn invalid_primitive() {
        let mut buf = SessionBuffer::new(60);
        match buf.append(1, "\"hello\"") {
            AppendResult::Invalid(_) => {}
            other => panic!("expected Invalid, got {:?}", other),
        }
    }

    #[test]
    fn ttl_expires_old_buffer() {
        let mut buf = SessionBuffer::new(0); // ttl 0 — expired ngay lần check sau
        buf.append(1, r#"{"a":1"#);
        assert_eq!(buf.pending_count(), 1);
        std::thread::sleep(Duration::from_millis(50));
        // Lần append kế tiếp: detect expired → clear, restart từ chunk này.
        let _ = buf.append(1, r#"more"#);
        // Vì chunk mới không phải JSON object → Pending, nhưng entries
        // vẫn 1 (chunk mới mở session mới).
        assert_eq!(buf.pending_count(), 1);
    }
}


impl SessionBuffer {
    /// Kiểm tra user có buffer chưa hoàn tất hay không. Dùng cho rate limiter
    /// để không đếm chunks tiếp theo của 1 paste dài như spam.
    pub fn has_pending(&self, chat_id: i64) -> bool {
        self.entries.contains_key(&chat_id)
    }
}
