//! Thẻ tiến trình thân thiện — dịch log kỹ thuật thô của runner thành 1 card
//! gọn (checklist 5 bước + thanh % + bộ đếm sống + lý do đời thường), song ngữ.
//!
//! Log thô vẫn còn nguyên trong logread router để debug; đây chỉ là lớp hiển
//! thị cho user Telegram.

use crate::bot::i18n::Lang;

/// 5 bước hiển thị cho user (gộp từ các step kỹ thuật của runner).
const PHASE_LOGIN: u8 = 0; // [1/6] login + [login] ...
const PHASE_PREPARE: u8 = 1; // [2/6] checkout, [3/6] init, [4/6] elements, [5a] token
const PHASE_CONFIRM: u8 = 2; // [5b] confirm, [5c] refresh
const PHASE_ACTIVATE: u8 = 3; // [6/6] approve loop + try N/M
const PHASE_QR: u8 = 4; // approve OK → xuất QR

#[derive(Clone, Copy, PartialEq, Eq, Default)]
enum Note {
    #[default]
    None,
    Blocked,
    Exception,
    Network,
}

/// Trạng thái rút gọn của 1 tiến trình, cập nhật theo từng dòng log.
pub struct ProcView {
    email: String,
    phase: u8,
    approve_cur: u32,
    approve_max: u32,
    note: Note,
}

impl ProcView {
    pub fn new(email: String) -> Self {
        Self {
            email,
            phase: PHASE_LOGIN,
            approve_cur: 0,
            approve_max: 0,
            note: Note::None,
        }
    }

    /// Cập nhật state từ 1 dòng log thô. Phase chỉ tiến, không lùi.
    pub fn update(&mut self, line: &str) {
        let detected = detect_phase(line);
        if let Some(p) = detected {
            if p > self.phase {
                self.phase = p;
            }
        }

        // Bộ đếm vòng kích hoạt: "      try 001/004  FAIL  http=200  blocked ..."
        if let Some((cur, max)) = parse_try(line) {
            self.phase = self.phase.max(PHASE_ACTIVATE);
            self.approve_cur = cur;
            self.approve_max = max;
        }

        // Lý do (chỉ áp dụng ở phase kích hoạt).
        if line.contains("try ") || line.contains("approve") {
            if line.contains("blocked") {
                self.note = Note::Blocked;
            } else if line.contains("exception") {
                self.note = Note::Exception;
            } else if line.contains("NetworkError") || line.contains("http=---") {
                self.note = Note::Network;
            }
        }
        // Outage detection của runner.
        if line.contains("[net]") && (line.contains("outage") || line.contains("waiting")) {
            self.note = Note::Network;
        }
    }

    fn pct(&self) -> u8 {
        match self.phase {
            PHASE_LOGIN => 15,
            PHASE_PREPARE => 50,
            PHASE_CONFIRM => 70,
            PHASE_ACTIVATE => {
                if self.approve_max > 0 {
                    let frac = self.approve_cur as f64 / self.approve_max as f64;
                    (75.0 + frac * 20.0).min(95.0) as u8
                } else {
                    75
                }
            }
            _ => 100,
        }
    }

    /// Render card "đang chạy".
    pub fn render_running(&self, lang: Lang, elapsed_s: f64) -> String {
        let bar = progress_bar(self.pct());
        let mut s = format!(
            "⚡ UPI QR · {}\n{} {}% · ⏱ {}\n\n",
            self.email,
            bar,
            self.pct(),
            fmt_elapsed(elapsed_s)
        );
        for p in 0..=PHASE_QR {
            let label = phase_label(lang, p);
            let icon = if p < self.phase {
                "✅"
            } else if p == self.phase {
                "🔄"
            } else {
                "⬜"
            };
            if p == PHASE_ACTIVATE && p == self.phase && self.approve_max > 0 {
                let lap = match lang {
                    Lang::Vi => "lần",
                    Lang::En => "try",
                };
                s.push_str(&format!(
                    "{} {} · {} {}/{}\n",
                    icon, label, lap, self.approve_cur, self.approve_max
                ));
            } else {
                s.push_str(&format!("{} {}\n", icon, label));
            }
        }
        if self.note != Note::None {
            s.push_str(&format!("\n💬 {}", note_text(lang, self.note)));
        }
        s
    }
}

// ─── Render các trạng thái terminal/queue ────────────────────────────────

pub fn render_queued(lang: Lang, email: &str, position: usize) -> String {
    match lang {
        Lang::Vi => format!(
            "⏳ ĐANG CHỜ · vị trí ~{}\n{}\nBot sẽ tự chạy khi tới lượt 🔔",
            position, email
        ),
        Lang::En => format!(
            "⏳ QUEUED · position ~{}\n{}\nThe bot will start automatically 🔔",
            position, email
        ),
    }
}

pub fn render_your_turn(lang: Lang, email: &str) -> String {
    match lang {
        Lang::Vi => format!("🔔 Tới lượt bạn! · {}\nĐang khởi động…", email),
        Lang::En => format!("🔔 Your turn! · {}\nStarting…", email),
    }
}

/// 1 dòng trạng thái proxy cho pre-flight card. `label` = "Login"/"User".
pub fn render_proxy_line(lang: Lang, label: &str, ok: bool, status: &str, detail: &str, latency_ms: u64) -> String {
    let icon = if ok { "✅" } else { "❌" };
    if ok {
        let ip = match lang {
            Lang::Vi => "IP",
            Lang::En => "IP",
        };
        format!("{} {}: {} · {} {} · {}ms", icon, label, status, ip, detail, latency_ms)
    } else {
        format!("{} {}: {} · {}", icon, label, status, detail)
    }
}

/// Card pre-flight khi 1 proxy chết → chặn chạy (fail-fast).
pub fn render_preflight_blocked(lang: Lang, email: &str, lines: &[String]) -> String {
    let body = lines.join("\n");
    match lang {
        Lang::Vi => format!(
            "⛔ KHÔNG THỂ CHẠY · {}\nProxy không khả dụng — đã chặn trước khi chạy.\n\n{}\n\n↳ Báo admin kiểm tra proxy login, hoặc /proxy_remove rồi thử lại.",
            email, body
        ),
        Lang::En => format!(
            "⛔ CANNOT RUN · {}\nProxy unavailable — blocked before start.\n\n{}\n\n↳ Ask admin to check the login proxy, or /proxy_remove and retry.",
            email, body
        ),
    }
}

/// Card pre-flight khi proxy OK → thông báo ngắn rồi vào hàng chờ.
pub fn render_preflight_ok(lang: Lang, email: &str, lines: &[String]) -> String {
    let body = lines.join("\n");
    match lang {
        Lang::Vi => format!("🌐 Kiểm tra proxy · {}\n{}", email, body),
        Lang::En => format!("🌐 Proxy check · {}\n{}", email, body),
    }
}

pub fn render_done_ok(lang: Lang, email: &str, elapsed_s: f64, expires: &str, attempts: usize) -> String {
    let bar = progress_bar(100);
    let activated = if attempts > 0 {
        match lang {
            Lang::Vi => format!(" · kích hoạt sau {} lần", attempts),
            Lang::En => format!(" · activated after {} tries", attempts),
        }
    } else {
        String::new()
    };
    match lang {
        Lang::Vi => format!(
            "✅ HOÀN TẤT · {}\n{} 100% · ⏱ {}\n\nMã QR UPI đã sẵn sàng 👇\nHết hạn: {}{}",
            email, bar, fmt_elapsed(elapsed_s), expires, activated
        ),
        Lang::En => format!(
            "✅ DONE · {}\n{} 100% · ⏱ {}\n\nYour UPI QR is ready 👇\nExpires: {}{}",
            email, bar, fmt_elapsed(elapsed_s), expires, activated
        ),
    }
}

pub fn render_done_fail(
    lang: Lang,
    email: &str,
    elapsed_s: f64,
    raw_error: &str,
    attempts: usize,
) -> String {
    let reason = friendly_reason(lang, raw_error);
    let tries = if attempts > 0 {
        match lang {
            Lang::Vi => format!(" · đã thử {} lần", attempts),
            Lang::En => format!(" · {} tries", attempts),
        }
    } else {
        String::new()
    };
    match lang {
        Lang::Vi => format!(
            "❌ KHÔNG THÀNH CÔNG · {}\n⏱ {}{}\n\nLý do: {}\n↳ Gửi lại để thử tài khoản khác.",
            email,
            fmt_elapsed(elapsed_s),
            tries,
            reason
        ),
        Lang::En => format!(
            "❌ FAILED · {}\n⏱ {}{}\n\nReason: {}\n↳ Send again to try another account.",
            email,
            fmt_elapsed(elapsed_s),
            tries,
            reason
        ),
    }
}

pub fn render_timeout(lang: Lang, email: &str, elapsed_s: f64) -> String {
    match lang {
        Lang::Vi => format!(
            "⏰ HẾT THỜI GIAN · {}\n⏱ {}\n\nĐã dừng để giải phóng tài nguyên. Bạn có thể gửi lại.",
            email,
            fmt_elapsed(elapsed_s)
        ),
        Lang::En => format!(
            "⏰ TIMED OUT · {}\n⏱ {}\n\nStopped to free resources. You can retry.",
            email,
            fmt_elapsed(elapsed_s)
        ),
    }
}

pub fn render_stopped(lang: Lang, email: &str, elapsed_s: f64) -> String {
    match lang {
        Lang::Vi => format!("🛑 ĐÃ DỪNG · {}\n⏱ {}", email, fmt_elapsed(elapsed_s)),
        Lang::En => format!("🛑 STOPPED · {}\n⏱ {}", email, fmt_elapsed(elapsed_s)),
    }
}

// ─── Helpers ─────────────────────────────────────────────────────────────

fn detect_phase(line: &str) -> Option<u8> {
    // Ưu tiên QR/approve trước (xuất hiện muộn nhất).
    if line.contains("[QR]") && line.contains("OK") {
        return Some(PHASE_QR);
    }
    if line.contains("approve     OK") || line.contains("approved at") {
        return Some(PHASE_QR);
    }
    if line.contains("[6/6]") || line.contains("try ") {
        return Some(PHASE_ACTIVATE);
    }
    if line.contains("[5b") || line.contains("[5c") {
        return Some(PHASE_CONFIRM);
    }
    if line.contains("[2/6")
        || line.contains("[3/6")
        || line.contains("[4/6")
        || line.contains("[5a]")
    {
        return Some(PHASE_PREPARE);
    }
    if line.contains("[1/6] login")
        || line.contains("[login]")
        || line.starts_with("Account:")
    {
        return Some(PHASE_LOGIN);
    }
    None
}

/// Parse "try 001/004 ..." → (cur, max).
fn parse_try(line: &str) -> Option<(u32, u32)> {
    let t = line.trim_start();
    let rest = t.strip_prefix("try ")?;
    let mut sp = rest.split('/');
    let cur: u32 = sp.next()?.trim().parse().ok()?;
    let max_part = sp.next()?;
    let max: u32 = max_part.split_whitespace().next()?.trim().parse().ok()?;
    Some((cur, max))
}

fn phase_label(lang: Lang, p: u8) -> &'static str {
    match (lang, p) {
        (Lang::Vi, PHASE_LOGIN) => "Đăng nhập",
        (Lang::Vi, PHASE_PREPARE) => "Chuẩn bị thanh toán",
        (Lang::Vi, PHASE_CONFIRM) => "Xác nhận UPI",
        (Lang::Vi, PHASE_ACTIVATE) => "Kích hoạt ưu đãi",
        (Lang::Vi, _) => "Xuất mã QR",
        (Lang::En, PHASE_LOGIN) => "Sign in",
        (Lang::En, PHASE_PREPARE) => "Preparing payment",
        (Lang::En, PHASE_CONFIRM) => "Confirming UPI",
        (Lang::En, PHASE_ACTIVATE) => "Activating offer",
        (Lang::En, _) => "Generating QR",
    }
}

fn note_text(lang: Lang, note: Note) -> &'static str {
    match (lang, note) {
        (Lang::Vi, Note::Blocked) => "Máy chủ đang từ chối — bot tự thử lại",
        (Lang::Vi, Note::Exception) => "Lỗi tạm thời — bot tự thử lại",
        (Lang::Vi, Note::Network) => "Mạng chậm — bot tự thử lại",
        (Lang::En, Note::Blocked) => "Server is rejecting — retrying",
        (Lang::En, Note::Exception) => "Temporary error — retrying",
        (Lang::En, Note::Network) => "Network slow — retrying",
        _ => "",
    }
}

/// Dịch lỗi thô của runner sang câu đời thường.
fn friendly_reason(lang: Lang, raw: &str) -> String {
    let r = raw.to_lowercase();
    let vi = matches!(lang, Lang::Vi);
    let pick = |v: &str, e: &str| -> String { if vi { v.into() } else { e.into() } };

    if r.contains("passwordless otp") || r.contains("email-verification") || r.contains("mailbox") {
        return pick(
            "Tài khoản đăng nhập bằng mã gửi vào email (cần hộp thư) — hãy gửi session.json thay vì combo.",
            "This account signs in via an email code (needs a mailbox) — send session.json instead of a combo.",
        );
    }
    if r.contains("login fail") || r.contains("mfa") || r.contains("password verify") {
        return pick(
            "Đăng nhập thất bại (sai mật khẩu/2FA, hoặc bị chặn).",
            "Sign-in failed (wrong password/2FA, or blocked).",
        );
    }
    if r.contains("billing country") {
        return pick(
            "Cần proxy đúng quốc gia (Ấn Độ) cho tài khoản này.",
            "This account needs a matching-country (India) proxy.",
        );
    }
    if r.contains("no free offer") || r.contains("promo") || r.contains("amount") {
        return pick(
            "Tài khoản chưa đủ điều kiện nhận ưu đãi UPI miễn phí.",
            "This account isn't eligible for the free UPI offer.",
        );
    }
    if r.contains("blocked") || r.contains("approve failed") || r.contains("not approved") {
        return pick(
            "Chưa đủ điều kiện nhận ưu đãi UPI (máy chủ từ chối).",
            "Not eligible for the UPI offer (server rejected).",
        );
    }
    if r.contains("no upi") || r.contains("qr image") || r.contains("qr ") {
        return pick(
            "Không lấy được mã QR từ máy chủ.",
            "Couldn't obtain the QR code from the server.",
        );
    }
    if r.contains("network") || r.contains("outage") || r.contains("timeout") || r.contains("checkout http") {
        return pick(
            "Mạng/máy chủ không ổn định, vui lòng thử lại.",
            "Network/server unstable, please try again.",
        );
    }
    // Fallback: cắt gọn raw error.
    let short: String = raw.chars().take(120).collect();
    if vi {
        format!("Không hoàn tất ({}).", short)
    } else {
        format!("Could not complete ({}).", short)
    }
}

fn progress_bar(pct: u8) -> String {
    let cells = 10usize;
    let filled = ((pct as f64 / 100.0) * cells as f64).round() as usize;
    let filled = filled.min(cells);
    let mut s = String::with_capacity(cells * 3);
    for _ in 0..filled {
        s.push('▓');
    }
    for _ in filled..cells {
        s.push('░');
    }
    s
}

fn fmt_elapsed(secs: f64) -> String {
    let s = secs.max(0.0) as u64;
    if s < 60 {
        format!("{}s", s)
    } else {
        format!("{}m{:02}s", s / 60, s % 60)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_try_works() {
        assert_eq!(parse_try("      try 001/004  FAIL  http=200  blocked"), Some((1, 4)));
        assert_eq!(parse_try("try 36/100 ok"), Some((36, 100)));
        assert_eq!(parse_try("no try here"), None);
    }

    #[test]
    fn phase_progresses_and_renders() {
        let mut v = ProcView::new("a***b@x.com".into());
        v.update("Account: a***b@x.com");
        assert_eq!(v.phase, PHASE_LOGIN);
        v.update("[2/6 [p1]] checkout   OK   cs=cs_live_x");
        assert_eq!(v.phase, PHASE_PREPARE);
        v.update("[5b [p1]] confirm    OK      variant=qr_code http=200");
        assert_eq!(v.phase, PHASE_CONFIRM);
        v.update("[6/6] approve loop start  retries=100");
        v.update("      try 006/100  FAIL  http=200  blocked    proxy=direct");
        assert_eq!(v.phase, PHASE_ACTIVATE);
        assert_eq!((v.approve_cur, v.approve_max), (6, 100));
        let card = v.render_running(Lang::Vi, 42.0);
        assert!(card.contains("Kích hoạt ưu đãi"));
        assert!(card.contains("6/100"));
        assert!(card.contains("Máy chủ đang từ chối"));
    }

    /// Preview thẻ tiến trình từ chuỗi log THẬT (chạy: cargo test preview -- --ignored --nocapture).
    #[test]
    #[ignore]
    fn preview_cards() {
        let email = "igl***02@hotmail.com";
        let real_log = [
            "Account: igl***02@hotmail.com",
            "[1/6] login   →    HTTP login (email|pass|2fa) proxy=direct",
            "[login] [9/9] GET /api/auth/session",
            "[1/6] login   OK   session acquired (cookies=30)",
            "[2/6 [p1]] checkout   OK   cs=cs_live_a1veg7… ui=custom",
            "[3/6 [p1]] init       OK   amount=0 ppage=ppage_1TkUY7…",
            "[4/6 [p1]] elements   OK   session=elements_sessi…",
            "[5a]   token-cfg  OK   shift=11 rv=e96dd269…",
            "[5b [p1]] confirm    OK      variant=qr_code http=200",
            "[5c [p1]] refresh    OK      http=200",
            "[6/6] approve loop start  retries=100 delay=3.0s batch=3",
            "      try 036/100  FAIL  http=200  blocked    proxy=direct",
        ];
        for lang in [Lang::Vi, Lang::En] {
            let mut v = ProcView::new(email.into());
            // Mốc 1: sau login
            for l in &real_log[..4] {
                v.update(l);
            }
            println!("\n===== [{:?}] ĐANG CHẠY (sau login) =====\n{}", lang, v.render_running(lang, 6.0));
            // Mốc 2: vòng kích hoạt
            for l in &real_log[4..] {
                v.update(l);
            }
            println!("\n===== [{:?}] ĐANG CHẠY (kích hoạt) =====\n{}", lang, v.render_running(lang, 162.0));
            // Terminal states
            println!("\n===== [{:?}] QUEUED =====\n{}", lang, render_queued(lang, email, 3));
            println!("\n===== [{:?}] THÀNH CÔNG =====\n{}", lang, render_done_ok(lang, email, 52.0, "21/06 14:30 (UTC+7)", 18));
            println!("\n===== [{:?}] FAIL: blocked =====\n{}", lang, render_done_fail(lang, email, 312.0, "approve failed after 100 attempts (retries=100)", 100));
            println!("\n===== [{:?}] FAIL: billing country =====\n{}", lang, render_done_fail(lang, email, 8.0, "checkout HTTP 400: {\"detail\":\"Billing country must match request country.\"}", 0));
            println!("\n===== [{:?}] FAIL: email-OTP =====\n{}", lang, render_done_fail(lang, email, 5.0, "login fail: account dùng passwordless OTP (cần mailbox)", 0));
            println!("\n===== [{:?}] TIMEOUT =====\n{}", lang, render_timeout(lang, email, 1800.0));
            println!("\n===== [{:?}] STOPPED =====\n{}", lang, render_stopped(lang, email, 30.0));
        }
    }
}
