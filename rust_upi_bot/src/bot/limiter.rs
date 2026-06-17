//! Per-user state — anti-spam, cooldown, in-flight tracking.
//!
//! Chính sách:
//!   * 1 job active per user. Submission tiếp khi đang chạy → reject.
//!   * Cooldown N giây sau khi job xong (kể cả PASS hoặc FAIL) → chống spam.
//!   * Message rate limit: max M message/phút per user. Vượt → ignore tạm.
//!   * Counter tự reset sau 60s sliding window.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::Mutex;

#[derive(Debug, Default, Clone)]
struct UserState {
    in_flight: bool,
    last_done_at: Option<Instant>,
    /// Sliding window: timestamps các message gần đây (giữ trong 60s).
    recent_messages: Vec<Instant>,
}

#[derive(Clone)]
pub struct UserLimiter {
    inner: Arc<Mutex<HashMap<i64, UserState>>>,
    cooldown: Duration,
    msg_rate_per_min: u32,
}

#[derive(Debug, Clone)]
pub enum AdmitDecision {
    /// Cho phép submit job mới.
    Allow,
    /// Có job đang chạy — chưa cho submit job mới.
    JobInFlight,
    /// Đang trong cooldown — phải đợi N giây nữa.
    Cooldown { remaining_secs: u64 },
}

#[derive(Debug, Clone)]
pub enum MessageDecision {
    Allow,
    /// Vượt rate limit — bot bỏ qua, log warning.
    Drop { observed: u32, limit: u32 },
}

impl UserLimiter {
    pub fn new(cooldown: Duration, msg_rate_per_min: u32) -> Self {
        Self {
            inner: Arc::new(Mutex::new(HashMap::new())),
            cooldown,
            msg_rate_per_min,
        }
    }

    /// Đăng ký 1 message từ user → trả AdmitDecision cho message đó (anti-flood).
    pub async fn register_message(&self, user_id: i64) -> MessageDecision {
        let mut g = self.inner.lock().await;
        let st = g.entry(user_id).or_default();
        let now = Instant::now();
        // Drop entries cũ hơn 60s
        st.recent_messages.retain(|t| now.duration_since(*t) < Duration::from_secs(60));
        st.recent_messages.push(now);
        let observed = st.recent_messages.len() as u32;
        if observed > self.msg_rate_per_min {
            return MessageDecision::Drop {
                observed,
                limit: self.msg_rate_per_min,
            };
        }
        MessageDecision::Allow
    }

    /// Check trước khi submit job. KHÔNG đánh dấu in_flight (gọi `mark_in_flight` sau khi submit OK).
    pub async fn check_submit(&self, user_id: i64) -> AdmitDecision {
        let g = self.inner.lock().await;
        let Some(st) = g.get(&user_id) else { return AdmitDecision::Allow };
        if st.in_flight {
            return AdmitDecision::JobInFlight;
        }
        if let Some(last) = st.last_done_at {
            let elapsed = last.elapsed();
            if elapsed < self.cooldown {
                return AdmitDecision::Cooldown {
                    remaining_secs: (self.cooldown - elapsed).as_secs() + 1,
                };
            }
        }
        AdmitDecision::Allow
    }

    pub async fn mark_in_flight(&self, user_id: i64) {
        let mut g = self.inner.lock().await;
        let st = g.entry(user_id).or_default();
        st.in_flight = true;
    }

    pub async fn mark_done(&self, user_id: i64) {
        let mut g = self.inner.lock().await;
        let st = g.entry(user_id).or_default();
        st.in_flight = false;
        st.last_done_at = Some(Instant::now());
    }

    /// Maintenance — drop entries không hoạt động > 1h để tránh leak HashMap.
    pub async fn vacuum(&self) {
        let mut g = self.inner.lock().await;
        let now = Instant::now();
        g.retain(|_, st| {
            if st.in_flight {
                return true;
            }
            let recent_recent = st
                .recent_messages
                .last()
                .map(|t| now.duration_since(*t) < Duration::from_secs(3600))
                .unwrap_or(false);
            let recent_done = st
                .last_done_at
                .map(|t| now.duration_since(t) < Duration::from_secs(3600))
                .unwrap_or(false);
            recent_recent || recent_done
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn cooldown_blocks_then_allows() {
        let lim = UserLimiter::new(Duration::from_millis(200), 100);
        assert!(matches!(lim.check_submit(1).await, AdmitDecision::Allow));
        lim.mark_in_flight(1).await;
        assert!(matches!(
            lim.check_submit(1).await,
            AdmitDecision::JobInFlight
        ));
        lim.mark_done(1).await;
        // Ngay sau khi done → vẫn cooldown
        assert!(matches!(
            lim.check_submit(1).await,
            AdmitDecision::Cooldown { .. }
        ));
        tokio::time::sleep(Duration::from_millis(220)).await;
        assert!(matches!(lim.check_submit(1).await, AdmitDecision::Allow));
    }

    #[tokio::test]
    async fn rate_limit_drops_after_threshold() {
        let lim = UserLimiter::new(Duration::from_secs(0), 3);
        for _ in 0..3 {
            assert!(matches!(
                lim.register_message(7).await,
                MessageDecision::Allow
            ));
        }
        // Cái thứ 4 vượt
        assert!(matches!(
            lim.register_message(7).await,
            MessageDecision::Drop { .. }
        ));
    }
}
