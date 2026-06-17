//! Per-user job registry — track tokens để `/stop` chỉ cancel job của
//! đúng user đó, không đụng đến người khác.
//!
//! Mỗi job 1 `CancellationToken`. Khi user gửi `/stop`:
//!   1. Lấy tất cả token của user → `cancel()` từng cái
//!   2. Remove khỏi map → memory không grow
//!
//! Khi job tự kết thúc (Done/Timeout/Cancel) → caller phải gọi `unregister`
//! để dọn entry. Vacuum định kỳ làm thêm 1 lớp safety.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use tokio::sync::Mutex;
use tokio_util::sync::CancellationToken;

#[derive(Clone)]
pub struct JobRegistry {
    inner: Arc<Inner>,
}

struct Inner {
    map: Mutex<HashMap<i64, Vec<Entry>>>,
    next_id: AtomicU64,
}

struct Entry {
    id: u64,
    token: CancellationToken,
}

impl JobRegistry {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Inner {
                map: Mutex::new(HashMap::new()),
                next_id: AtomicU64::new(1),
            }),
        }
    }

    /// Đăng ký 1 job mới của user. Trả `(job_id, cancel_token)`.
    /// Worker phải `tokio::select!` cùng `token.cancelled()` để stop sớm
    /// khi user gửi `/stop`.
    pub async fn register(&self, user_id: i64) -> (u64, CancellationToken) {
        let id = self.inner.next_id.fetch_add(1, Ordering::Relaxed);
        let token = CancellationToken::new();
        let mut g = self.inner.map.lock().await;
        g.entry(user_id).or_default().push(Entry {
            id,
            token: token.clone(),
        });
        (id, token)
    }

    /// Gọi khi job tự kết thúc (Done/Timeout/Cancel) — dọn entry.
    /// No-op nếu user đã /stop trước đó (entry đã được clear).
    pub async fn unregister(&self, user_id: i64, job_id: u64) {
        let mut g = self.inner.map.lock().await;
        if let Some(v) = g.get_mut(&user_id) {
            v.retain(|e| e.id != job_id);
            if v.is_empty() {
                g.remove(&user_id);
            }
        }
    }

    /// Cancel TẤT CẢ job đang chạy/queue của user. Trả số job bị cancel.
    /// Không đụng job của user khác.
    pub async fn stop_user(&self, user_id: i64) -> usize {
        let mut g = self.inner.map.lock().await;
        let Some(entries) = g.remove(&user_id) else {
            return 0;
        };
        let n = entries.len();
        for e in entries {
            e.token.cancel();
        }
        n
    }

    /// Số user còn entry (memory metric).
    pub async fn user_count(&self) -> usize {
        self.inner.map.lock().await.len()
    }

    /// Vacuum entries có token đã cancelled (safety cleanup nếu unregister
    /// quên gọi). Gọi định kỳ.
    pub async fn vacuum(&self) {
        let mut g = self.inner.map.lock().await;
        for (_, v) in g.iter_mut() {
            v.retain(|e| !e.token.is_cancelled());
        }
        g.retain(|_, v| !v.is_empty());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn stop_user_only_cancels_own_jobs() {
        let reg = JobRegistry::new();
        let (id_a1, token_a1) = reg.register(100).await;
        let (id_a2, token_a2) = reg.register(100).await;
        let (id_b1, token_b1) = reg.register(200).await;
        assert_eq!(reg.user_count().await, 2);

        let stopped = reg.stop_user(100).await;
        assert_eq!(stopped, 2);
        assert!(token_a1.is_cancelled());
        assert!(token_a2.is_cancelled());
        // User 200 KHÔNG bị cancel
        assert!(!token_b1.is_cancelled());

        // Map cleaned for user 100, retained for 200
        assert_eq!(reg.user_count().await, 1);
        let _ = (id_a1, id_a2, id_b1);
    }

    #[tokio::test]
    async fn unregister_cleans_entry() {
        let reg = JobRegistry::new();
        let (id, _token) = reg.register(7).await;
        assert_eq!(reg.user_count().await, 1);
        reg.unregister(7, id).await;
        assert_eq!(reg.user_count().await, 0);
    }

    #[tokio::test]
    async fn stop_user_returns_zero_when_no_jobs() {
        let reg = JobRegistry::new();
        assert_eq!(reg.stop_user(999).await, 0);
    }

    #[tokio::test]
    async fn vacuum_drops_cancelled_entries() {
        let reg = JobRegistry::new();
        let (_, token) = reg.register(42).await;
        token.cancel();
        // Entry vẫn còn vì chưa unregister
        assert_eq!(reg.user_count().await, 1);
        reg.vacuum().await;
        assert_eq!(reg.user_count().await, 0);
    }
}
