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
    in_flight: u32,
    last_done_at: Option<Instant>,
    /// Sliding window: timestamps các message gần đây (giữ trong 60s).
    recent_messages: Vec<Instant>,
}

#[derive(Clone)]
pub struct UserLimiter {
    inner: Arc<Mutex<HashMap<i64, UserState>>>,
    cooldown: Duration,
    msg_rate_per_min: u32,
    /// Số tiến trình tối đa 1 user chạy đồng thời.
    max_per_user: u32,
}

#[derive(Debug, Clone)]
pub enum AdmitDecision {
    /// Cho phép submit job mới.
    Allow,
    /// Đã đạt giới hạn tiến trình đồng thời của user.
    MaxConcurrent { max: u32 },
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
    pub fn new(cooldown: Duration, msg_rate_per_min: u32, max_per_user: u32) -> Self {
        Self {
            inner: Arc::new(Mutex::new(HashMap::new())),
            cooldown,
            msg_rate_per_min,
            max_per_user: max_per_user.max(1),
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

    /// Check + RESERVE slot atomic trong 1 lần lock. Nếu `Allow` thì đã tăng
    /// `in_flight` luôn → đóng khe TOCTOU giữa check và submit (preflight proxy
    /// await ở giữa không còn cho phép user vượt `max_per_user`). Caller PHẢI
    /// `release` nếu sau đó submit thất bại, hoặc để `mark_done` trả slot khi
    /// job hoàn tất.
    pub async fn try_admit(&self, user_id: i64) -> AdmitDecision {
        let mut g = self.inner.lock().await;
        let st = g.entry(user_id).or_default();
        if st.in_flight >= self.max_per_user {
            return AdmitDecision::MaxConcurrent { max: self.max_per_user };
        }
        if st.in_flight == 0 {
            if let Some(last) = st.last_done_at {
                let elapsed = last.elapsed();
                if elapsed < self.cooldown {
                    return AdmitDecision::Cooldown {
                        remaining_secs: (self.cooldown - elapsed).as_secs() + 1,
                    };
                }
            }
        }
        st.in_flight += 1; // reserve ngay
        AdmitDecision::Allow
    }

    /// Rollback 1 reservation khi job KHÔNG vào được queue (proxy chết, trùng
    /// tài khoản, queue đầy...). Chỉ giảm `in_flight`, KHÔNG set `last_done_at`
    /// → không áp cooldown oan cho job chưa từng chạy.
    pub async fn release(&self, user_id: i64) {
        let mut g = self.inner.lock().await;
        if let Some(st) = g.get_mut(&user_id) {
            st.in_flight = st.in_flight.saturating_sub(1);
        }
    }

    pub async fn mark_done(&self, user_id: i64) {
        let mut g = self.inner.lock().await;
        let st = g.entry(user_id).or_default();
        st.in_flight = st.in_flight.saturating_sub(1);
        st.last_done_at = Some(Instant::now());
    }

    /// Maintenance — drop entries không hoạt động > 1h để tránh leak HashMap.
    pub async fn vacuum(&self) {
        let mut g = self.inner.lock().await;
        let now = Instant::now();
        g.retain(|_, st| {
            if st.in_flight > 0 {
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
        let lim = UserLimiter::new(Duration::from_millis(200), 100, 2);
        // try_admit reserve ngay khi Allow.
        assert!(matches!(lim.try_admit(1).await, AdmitDecision::Allow));
        // Vẫn còn slot thứ 2 → Allow (reserve tiếp).
        assert!(matches!(lim.try_admit(1).await, AdmitDecision::Allow));
        // Đã đủ 2 → MaxConcurrent.
        assert!(matches!(
            lim.try_admit(1).await,
            AdmitDecision::MaxConcurrent { max: 2 }
        ));
        lim.mark_done(1).await;
        lim.mark_done(1).await;
        // Idle + ngay sau done → cooldown.
        assert!(matches!(
            lim.try_admit(1).await,
            AdmitDecision::Cooldown { .. }
        ));
        tokio::time::sleep(Duration::from_millis(220)).await;
        assert!(matches!(lim.try_admit(1).await, AdmitDecision::Allow));
    }

    #[tokio::test]
    async fn release_rolls_back_reservation_without_cooldown() {
        let lim = UserLimiter::new(Duration::from_secs(60), 1, 1);
        assert!(matches!(lim.try_admit(9).await, AdmitDecision::Allow));
        // Đã đầy slot.
        assert!(matches!(
            lim.try_admit(9).await,
            AdmitDecision::MaxConcurrent { max: 1 }
        ));
        // Rollback (submit fail) → slot trả lại, KHÔNG cooldown.
        lim.release(9).await;
        assert!(matches!(lim.try_admit(9).await, AdmitDecision::Allow));
    }

    #[tokio::test]
    async fn rate_limit_drops_after_threshold() {
        let lim = UserLimiter::new(Duration::from_secs(0), 3, 2);
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
