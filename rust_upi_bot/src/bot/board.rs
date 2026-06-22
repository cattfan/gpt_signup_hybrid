//! Live job board — snapshot trung tâm của mọi job đang trong hệ thống, phục
//! vụ bảng trạng thái realtime cho admin (`/board`).
//!
//! Khác `JobRegistry` (chỉ giữ cancel token để `/stop`), board giữ thêm trạng
//! thái hiển thị: user, email (đã mask), state, step (log cuối), mốc thời gian.
//!
//! Vòng đời 1 entry (key = `job_id` u64 từ `JobRegistry::register`):
//!   1. submit thành công  → `insert_queued`
//!   2. worker pickup       → `mark_running`
//!   3. mỗi log line        → `set_step`
//!   4. Done/Timeout/Cancel → `remove`
//!
//! Map sync + ngắn, không giữ guard qua `.await` (tokio `Mutex` vẫn dùng để
//! await an toàn nếu cần). Render là pure function trên snapshot đã clone.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Instant;

use tokio::sync::Mutex;

/// Trạng thái 1 job trong board.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JobState {
    Queued,
    Running,
}

impl JobState {
    pub fn icon(self) -> &'static str {
        match self {
            JobState::Queued => "⏳",
            JobState::Running => "▶️",
        }
    }
}

#[derive(Debug, Clone)]
pub struct JobStatus {
    pub user_id: i64,
    pub username: Option<String>,
    /// Email đã mask sẵn (không lưu plaintext).
    pub email_masked: String,
    pub state: JobState,
    /// Log line gần nhất — hiển thị cột "step". Rỗng khi chưa có log.
    pub step: String,
    /// Mốc tính tuổi: thời điểm submit (Queued) hoặc thời điểm chạy (Running).
    pub since: Instant,
}

#[derive(Clone)]
pub struct JobBoard {
    inner: Arc<Mutex<HashMap<u64, JobStatus>>>,
}

impl JobBoard {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    /// Thêm job vừa submit vào board (state = Queued).
    pub async fn insert_queued(
        &self,
        job_id: u64,
        user_id: i64,
        username: Option<String>,
        email_masked: String,
    ) {
        let mut g = self.inner.lock().await;
        g.insert(
            job_id,
            JobStatus {
                user_id,
                username,
                email_masked,
                state: JobState::Queued,
                step: String::new(),
                since: Instant::now(),
            },
        );
    }

    /// Worker pickup → chuyển sang Running, reset mốc tuổi về thời điểm chạy.
    pub async fn mark_running(&self, job_id: u64) {
        let mut g = self.inner.lock().await;
        if let Some(s) = g.get_mut(&job_id) {
            s.state = JobState::Running;
            s.since = Instant::now();
        }
    }

    /// Cập nhật log line gần nhất cho job.
    pub async fn set_step(&self, job_id: u64, step: String) {
        let mut g = self.inner.lock().await;
        if let Some(s) = g.get_mut(&job_id) {
            s.step = step;
        }
    }

    /// Job kết thúc (Done/Timeout/Cancelled) → bỏ khỏi board.
    pub async fn remove(&self, job_id: u64) {
        self.inner.lock().await.remove(&job_id);
    }

    /// Xóa SẠCH board (admin `/flushall`). Trả số entry đã xóa.
    pub async fn clear_all(&self) -> usize {
        let mut g = self.inner.lock().await;
        let n = g.len();
        g.clear();
        n
    }

    /// Snapshot kèm `job_id`, sắp xếp ổn định: Running trước Queued, rồi theo
    /// tuổi giảm dần. Clone để render không giữ lock.
    pub async fn snapshot_entries(&self) -> Vec<(u64, JobStatus)> {
        let g = self.inner.lock().await;
        let mut out: Vec<(u64, JobStatus)> = g.iter().map(|(k, v)| (*k, v.clone())).collect();
        sort_entries(&mut out);
        out
    }

    /// Như `snapshot_entries` nhưng CHỈ job của `user_id` — dùng cho `/board`
    /// của user thường (bảo mật: không lộ tiến trình của người khác).
    pub async fn snapshot_entries_for_user(&self, user_id: i64) -> Vec<(u64, JobStatus)> {
        let g = self.inner.lock().await;
        let mut out: Vec<(u64, JobStatus)> = g
            .iter()
            .filter(|(_, v)| v.user_id == user_id)
            .map(|(k, v)| (*k, v.clone()))
            .collect();
        sort_entries(&mut out);
        out
    }

    /// Chủ sở hữu (user_id) của 1 job — dùng để verify quyền khi cần.
    #[allow(dead_code)]
    pub async fn owner_of(&self, job_id: u64) -> Option<i64> {
        self.inner.lock().await.get(&job_id).map(|s| s.user_id)
    }
}

/// Sắp xếp entries: Running trước Queued, rồi job lâu nhất lên đầu.
fn sort_entries(out: &mut [(u64, JobStatus)]) {
    out.sort_by(|a, b| match (a.1.state, b.1.state) {
        (JobState::Running, JobState::Queued) => std::cmp::Ordering::Less,
        (JobState::Queued, JobState::Running) => std::cmp::Ordering::Greater,
        _ => b.1.since.cmp(&a.1.since).reverse(),
    });
}

/// Cắt chuỗi theo số ký tự (char-safe), thêm '…' nếu bị cắt.
pub fn truncate_chars(s: &str, max: usize) -> String {
    let trimmed = s.trim();
    if trimmed.chars().count() <= max {
        return trimmed.to_string();
    }
    let mut out: String = trimmed.chars().take(max.saturating_sub(1)).collect();
    out.push('…');
    out
}

/// Định dạng tuổi job dạng mm:ss (hoặc h:mm:ss khi >= 1 giờ).
pub fn fmt_age(secs: u64) -> String {
    let h = secs / 3600;
    let m = (secs % 3600) / 60;
    let s = secs % 60;
    if h > 0 {
        format!("{}:{:02}:{:02}", h, m, s)
    } else {
        format!("{:02}:{:02}", m, s)
    }
}
