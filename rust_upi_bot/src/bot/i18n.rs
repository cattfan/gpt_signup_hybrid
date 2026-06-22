//! i18n — chuỗi giao diện người dùng Việt/Anh.
//!
//! Single source cho mọi text gửi user. Admin command vẫn tiếng Anh (operator).
//! `Lang` lưu trong Settings store theo user_id (bảng `user_lang`).

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Lang {
    Vi,
    En,
}

impl Lang {
    pub fn from_code(s: &str) -> Option<Lang> {
        match s.trim().to_lowercase().as_str() {
            "vi" => Some(Lang::Vi),
            "en" => Some(Lang::En),
            _ => None,
        }
    }
    pub fn code(self) -> &'static str {
        match self {
            Lang::Vi => "vi",
            Lang::En => "en",
        }
    }
}

/// Chọn theo lang: (vi, en).
fn pick(lang: Lang, vi: &str, en: &str) -> String {
    match lang {
        Lang::Vi => vi.to_string(),
        Lang::En => en.to_string(),
    }
}

// ─── Language picker ────────────────────────────────────────────────────

pub fn choose_language() -> &'static str {
    "🌐 Chọn ngôn ngữ / Choose your language:"
}

pub fn language_set(lang: Lang) -> String {
    pick(lang, "✅ Đã đặt ngôn ngữ: Tiếng Việt", "✅ Language set: English")
}

// ─── Welcome / help ───────────────────────────────────────────────────

pub fn welcome(lang: Lang) -> String {
    pick(
        lang,
        "👋 *UPI QR Bot*\n\n\
         Gửi cho bot 1 trong 2:\n\
         • File `session.json` lấy từ https://chatgpt.com/api/auth/session\n\
         • Hoặc tài khoản dạng `email|password|2fa_secret` (gửi nhiều dòng = nhiều tiến trình)\n\n\
         Bạn chạy được tối đa *10 tiến trình* cùng lúc. Mỗi tiến trình hiển thị ở 1 tin riêng kèm nút Dừng.\n\n\
         Chọn thao tác:",
        "👋 *UPI QR Bot*\n\n\
         Send the bot one of:\n\
         • A `session.json` file from https://chatgpt.com/api/auth/session\n\
         • Or accounts as `email|password|2fa_secret` (multiple lines = multiple processes)\n\n\
         You can run up to *10 processes* at once. Each process shows in its own message with a Stop button.\n\n\
         Pick an action:",
    )
}

// ─── Button labels ──────────────────────────────────────────────────────

pub fn btn_status(lang: Lang) -> String {
    pick(lang, "📊 Trạng thái", "📊 Status")
}
pub fn btn_stop_all(lang: Lang) -> String {
    pick(lang, "🛑 Dừng hết", "🛑 Stop all")
}
pub fn btn_help(lang: Lang) -> String {
    pick(lang, "❓ Trợ giúp", "❓ Help")
}
pub fn btn_settings(lang: Lang) -> String {
    pick(lang, "⚙️ Cài đặt", "⚙️ Settings")
}
pub fn btn_stop_this(lang: Lang) -> String {
    pick(lang, "🛑 Dừng tiến trình này", "🛑 Stop this process")
}
pub fn btn_language(lang: Lang) -> String {
    pick(lang, "🌐 Ngôn ngữ", "🌐 Language")
}
pub fn btn_board_refresh(lang: Lang) -> String {
    pick(lang, "🔄 Làm mới", "🔄 Refresh")
}
pub fn btn_board_stop(lang: Lang, email: &str) -> String {
    pick(lang, &format!("🛑 {}", email), &format!("🛑 {}", email))
}

// ─── Board ────────────────────────────────────────────────────────────

/// Header bảng tiến trình. `scope_all` = admin xem toàn hệ thống; false = user
/// chỉ xem tiến trình của chính mình.
pub fn board_header(lang: Lang, running: usize, queued: usize, scope_all: bool, clock: &str) -> String {
    let scope_vi = if scope_all { "toàn hệ thống" } else { "của bạn" };
    let scope_en = if scope_all { "system-wide" } else { "yours" };
    pick(
        lang,
        &format!("📊 Tiến trình {} — ▶️ {} chạy · ⏳ {} chờ · 🕒 {}", scope_vi, running, queued, clock),
        &format!("📊 Processes ({}) — ▶️ {} run · ⏳ {} queue · 🕒 {}", scope_en, running, queued, clock),
    )
}

pub fn board_empty(lang: Lang, scope_all: bool) -> String {
    if scope_all {
        pick(lang, "\n\nKhông có tiến trình nào đang chạy.", "\n\nNo processes running.")
    } else {
        pick(lang, "\n\nBạn chưa có tiến trình nào.", "\n\nYou have no processes running.")
    }
}

pub fn board_stopped_toast(lang: Lang, ok: bool) -> String {
    if ok {
        pick(lang, "🛑 Đã dừng tiến trình.", "🛑 Process stopped.")
    } else {
        pick(lang, "ℹ️ Tiến trình đã kết thúc / không thuộc về bạn.", "ℹ️ Process already finished / not yours.")
    }
}

// ─── Settings menu ────────────────────────────────────────────────────

pub fn settings_title(lang: Lang) -> String {
    pick(lang, "⚙️ Cài đặt", "⚙️ Settings")
}

// ─── Submit / validation ────────────────────────────────────────────────

pub fn invalid_session_json(lang: Lang) -> String {
    pick(
        lang,
        "❌ JSON không hợp lệ. Chỉ nhận file `session.json` lấy từ \
         https://chatgpt.com/api/auth/session (phải có `accessToken` + `user`), \
         hoặc combo `email|password|2fa_secret`.",
        "❌ Invalid JSON. Only accept `session.json` from \
         https://chatgpt.com/api/auth/session (must contain `accessToken` + `user`), \
         or combo `email|password|2fa_secret`.",
    )
}

pub fn invalid_combo(lang: Lang) -> String {
    pick(
        lang,
        "❌ Sai định dạng. Gửi 1 tài khoản mỗi lần dạng `email|password|2fa_secret`, \
         hoặc gửi file `session.json`.",
        "❌ Wrong format. Send one account at a time as `email|password|2fa_secret`, \
         or send a `session.json` file.",
    )
}

/// Header khi nhận batch nhiều dòng combo. `accepted` = số job sẽ tạo,
/// `invalid` = dòng sai bị bỏ, `dropped` = dòng vượt cap/user bị bỏ.
pub fn combo_batch_received(lang: Lang, accepted: usize, invalid: usize, dropped: usize) -> String {
    let mut vi = format!("📥 Nhận {} tài khoản — tạo tiến trình cho từng cái...", accepted);
    let mut en = format!("📥 Received {} accounts — creating a process for each...", accepted);
    if invalid > 0 {
        vi.push_str(&format!("\n⚠️ Bỏ {} dòng sai định dạng.", invalid));
        en.push_str(&format!("\n⚠️ Skipped {} invalid line(s).", invalid));
    }
    if dropped > 0 {
        vi.push_str(&format!("\n⚠️ Vượt giới hạn {} tiến trình/lần — bỏ {} dòng dư.", accepted, dropped));
        en.push_str(&format!("\n⚠️ Over the {} processes/batch limit — dropped {} extra line(s).", accepted, dropped));
    }
    pick(lang, &vi, &en)
}

pub fn need_input(lang: Lang) -> String {    pick(
        lang,
        "📄 Gửi file `session.json` hoặc dán combo `email|password|2fa`.",
        "📄 Send a `session.json` file or paste combo `email|password|2fa`.",
    )
}

pub fn session_no_email(lang: Lang) -> String {
    pick(
        lang,
        "❌ Session thiếu email. File `session.json` phải có `user.email` hợp lệ \
         (đây là định danh tài khoản). Lấy lại session từ \
         https://chatgpt.com/api/auth/session rồi gửi lại.",
        "❌ Session has no email. The `session.json` must contain a valid \
         `user.email` (account identifier). Re-fetch from \
         https://chatgpt.com/api/auth/session and resend.",
    )
}

// ─── Admission ────────────────────────────────────────────────────────

pub fn duplicate_account(lang: Lang, email: &str) -> String {
    pick(
        lang,
        &format!(
            "⚠️ Tài khoản {} đang chạy/trong hàng chờ rồi. Đợi tiến trình hiện tại \
             xong (hoặc /stop) trước khi gửi lại tài khoản này.",
            email
        ),
        &format!(
            "⚠️ Account {} is already running/queued. Wait for the current process \
             to finish (or /stop) before resubmitting this account.",
            email
        ),
    )
}

pub fn max_concurrent(lang: Lang, max: u32) -> String {
    pick(
        lang,
        &format!("⚠️ Bạn đang chạy tối đa {} tiến trình. Đợi 1 tiến trình xong rồi gửi tiếp.", max),
        &format!("⚠️ You're already running {} processes. Wait for one to finish before sending more.", max),
    )
}

pub fn cooldown(lang: Lang, secs: u64) -> String {
    pick(
        lang,
        &format!("⏱ Chờ {}s rồi thử lại.", secs),
        &format!("⏱ Cooldown — wait {}s before retrying.", secs),
    )
}

pub fn queue_full(lang: Lang, pending: usize, capacity: usize) -> String {
    pick(
        lang,
        &format!("🚫 Hàng chờ đầy ({}/{}). Bot đang bận, thử lại sau ít phút.", pending, capacity),
        &format!("🚫 Queue full ({}/{}). Bot is busy, retry in a few minutes.", pending, capacity),
    )
}

pub fn queue_closed(lang: Lang) -> String {
    pick(lang, "🚫 Hàng chờ đã đóng.", "🚫 Queue closed.")
}

// ─── Job lifecycle ────────────────────────────────────────────────────

pub fn job_received(lang: Lang, email: &str) -> String {
    pick(
        lang,
        &format!("🚀 Đã nhận tài khoản\nEmail: {}\nĐang vào hàng chờ...", email),
        &format!("🚀 Account received\nEmail: {}\nQueueing...", email),
    )
}

pub fn qr_caption(lang: Lang, email: &str, expires: &str) -> String {
    pick(
        lang,
        &format!("✅ UPI QR\nEmail: {}\nHết hạn: {}", email, expires),
        &format!("✅ UPI QR\nEmail: {}\nExpires: {}", email, expires),
    )
}

/// Tin nhắn riêng kèm link thanh toán (gửi sau ảnh QR khi thành công).
pub fn payment_link_msg(lang: Lang, url: &str) -> String {
    pick(
        lang,
        &format!("💳 Link thanh toán:\n{}", url),
        &format!("💳 Payment link:\n{}", url),
    )
}

// ─── Stop ─────────────────────────────────────────────────────────────

pub fn stopped_this(lang: Lang) -> String {
    pick(lang, "🛑 Đã dừng tiến trình này.", "🛑 This process was stopped.")
}

pub fn stop_not_found(lang: Lang) -> String {
    pick(lang, "ℹ️ Tiến trình đã kết thúc hoặc không tồn tại.", "ℹ️ Process already finished or not found.")
}

pub fn stopped_all(lang: Lang, n: usize) -> String {
    if n == 0 {
        pick(lang, "ℹ️ Không có tiến trình nào để dừng.", "ℹ️ No running processes to stop.")
    } else {
        pick(
            lang,
            &format!("🛑 Đã dừng {} tiến trình của bạn.", n),
            &format!("🛑 Stopped {} of your processes.", n),
        )
    }
}

// ─── Access ───────────────────────────────────────────────────────────

pub fn banned(lang: Lang) -> String {
    pick(lang, "⛔ Bạn đã bị admin chặn.", "⛔ You have been blocked by the admin.")
}

pub fn not_whitelisted(lang: Lang) -> String {
    pick(lang, "⛔ Tài khoản chưa được cấp quyền. Liên hệ admin.", "⛔ Account not whitelisted. Contact the admin.")
}

pub fn unknown_command(lang: Lang, cmd: &str) -> String {
    pick(
        lang,
        &format!("❓ Lệnh không hợp lệ: {}\n\nGõ /help để xem danh sách.", cmd),
        &format!("❓ Unknown command: {}\n\nType /help for the list.", cmd),
    )
}

pub fn status_online(lang: Lang) -> String {
    pick(lang, "✅ Bot đang hoạt động.", "✅ Bot online.")
}

// ─── Proxy: buttons ───────────────────────────────────────────────────

pub fn btn_proxy_check(lang: Lang) -> String {
    pick(lang, "🔍 Kiểm tra trạng thái", "🔍 Check live status")
}
pub fn btn_proxy_remove(lang: Lang) -> String {
    pick(lang, "🗑 Xóa proxy", "🗑 Remove proxy")
}

// ─── Proxy: /proxy_set ────────────────────────────────────────────────

pub fn proxy_show_current(lang: Lang, masked: &str) -> String {
    pick(
        lang,
        &format!(
            "🌐 Proxy của bạn:\n{}\n\nDùng các nút bên dưới để kiểm tra trạng thái hoặc xóa.\n\
             Đổi proxy: /proxy_set <line>",
            masked
        ),
        &format!(
            "🌐 Your proxy:\n{}\n\nUse the buttons below to check live status or remove it.\n\
             To change proxy: /proxy_set <line>",
            masked
        ),
    )
}

pub fn proxy_set_usage(lang: Lang) -> String {
    pick(
        lang,
        "ℹ️ Bạn chưa đặt proxy.\n\n\
         Cách dùng:\n\
         /proxy_set host:port\n\
         /proxy_set host:port:user:pass\n\
         /proxy_set http://user:pass@host:port\n\
         /proxy_set socks5://user:pass@host:1080\n\n\
         Hỗ trợ {SID} cho sticky session:\n\
         /proxy_set host:port:user-{SID}:pass",
        "ℹ️ You haven't set a proxy yet.\n\n\
         Usage:\n\
         /proxy_set host:port\n\
         /proxy_set host:port:user:pass\n\
         /proxy_set http://user:pass@host:port\n\
         /proxy_set socks5://user:pass@host:1080\n\n\
         Supports {SID} placeholder for sticky sessions:\n\
         /proxy_set host:port:user-{SID}:pass",
    )
}

pub fn proxy_empty_line(lang: Lang) -> String {
    pick(lang, "❌ Dòng proxy rỗng.", "❌ Empty proxy line.")
}

pub fn proxy_invalid_format(lang: Lang, err: &str) -> String {
    pick(
        lang,
        &format!(
            "❌ Định dạng proxy không hợp lệ: {}\n\n\
             Hỗ trợ:\n\
             • host:port\n\
             • host:port:user:pass\n\
             • scheme://user:pass@host:port",
            err
        ),
        &format!(
            "❌ Invalid proxy format: {}\n\n\
             Supported:\n\
             • host:port\n\
             • host:port:user:pass\n\
             • scheme://user:pass@host:port",
            err
        ),
    )
}

pub fn proxy_save_failed(lang: Lang, err: &str) -> String {
    pick(
        lang,
        &format!("❌ Lưu thất bại: {}", err),
        &format!("❌ Save failed: {}", err),
    )
}

pub fn proxy_set_ok(lang: Lang, masked: &str, from_step: u32) -> String {
    pick(
        lang,
        &format!(
            "✅ Đã đặt proxy riêng của bạn.\n{}\n\n\
             Job tiếp theo sẽ dùng proxy này (ghi đè pool chung từ step {} trở đi).\n\
             Dùng các nút bên dưới để kiểm tra trạng thái hoặc xóa.",
            masked, from_step
        ),
        &format!(
            "✅ Your private proxy has been set.\n{}\n\n\
             Your next job will use this proxy (overrides the global pool from step {} onward).\n\
             Use the buttons below to check live status or remove it.",
            masked, from_step
        ),
    )
}

// ─── Proxy: /proxy_remove + callbacks ─────────────────────────────────

pub fn proxy_removed_global(lang: Lang) -> String {
    pick(
        lang,
        "🧹 Đã xóa proxy của bạn. Job tiếp theo sẽ dùng pool chung của admin (hoặc DIRECT).",
        "🧹 Your proxy has been removed. Your next job will use the admin's global pool (or DIRECT).",
    )
}

pub fn proxy_removed_direct(lang: Lang) -> String {
    pick(
        lang,
        "🧹 Đã xóa proxy của bạn. Job tiếp theo sẽ chạy DIRECT (hoặc dùng pool chung của admin).",
        "🧹 Your proxy has been removed. Your next job will run DIRECT (or use the admin's global pool).",
    )
}

pub fn proxy_none_to_remove(lang: Lang) -> String {
    pick(
        lang,
        "ℹ️ Bạn chưa đặt proxy nào để xóa.",
        "ℹ️ You don't have a proxy set to remove.",
    )
}

pub fn proxy_not_set(lang: Lang) -> String {
    pick(lang, "ℹ️ Bạn chưa đặt proxy.", "ℹ️ You haven't set a proxy yet.")
}

pub fn proxy_remove_failed(lang: Lang, err: &str) -> String {
    pick(
        lang,
        &format!("❌ Xóa thất bại: {}", err),
        &format!("❌ Remove failed: {}", err),
    )
}

pub fn db_error(lang: Lang, err: &str) -> String {
    pick(
        lang,
        &format!("❌ Lỗi DB: {}", err),
        &format!("❌ DB error: {}", err),
    )
}

// ─── Proxy: probe result card ─────────────────────────────────────────

pub fn proxy_probe_card(
    lang: Lang,
    ok: bool,
    status: &str,
    masked_line: &str,
    latency_ms: u64,
    detail_label_value: &str,
    endpoint: &str,
) -> String {
    let icon = if ok { "✅" } else { "❌" };
    pick(
        lang,
        &format!(
            "{} Kiểm tra proxy: {}\n\
             Dòng: {}\n\
             Độ trễ: {} ms\n\
             {}\n\
             Endpoint: {}\n",
            icon, status, masked_line, latency_ms, detail_label_value, endpoint
        ),
        &format!(
            "{} Proxy probe: {}\n\
             Line: {}\n\
             Latency: {} ms\n\
             {}\n\
             Endpoint: {}\n",
            icon, status, masked_line, latency_ms, detail_label_value, endpoint
        ),
    )
}

/// Nhãn dòng detail trong probe card (Exit IP khi OK, Detail khi lỗi).
pub fn proxy_probe_detail(lang: Lang, ok: bool, value: &str) -> String {
    if ok {
        pick(lang, &format!("IP ra: {}", value), &format!("Exit IP: {}", value))
    } else {
        pick(lang, &format!("Chi tiết: {}", value), &format!("Detail: {}", value))
    }
}

// ─── Short toasts (answerCallbackQuery) ───────────────────────────────

pub fn toast_blocked(lang: Lang) -> String {
    pick(lang, "Bạn đã bị chặn", "You are blocked")
}
pub fn toast_not_whitelisted(lang: Lang) -> String {
    pick(lang, "Chưa được cấp quyền", "Not whitelisted")
}
pub fn toast_probing(lang: Lang) -> String {
    pick(lang, "Đang kiểm tra...", "Probing...")
}
pub fn toast_removed(lang: Lang) -> String {
    pick(lang, "Đã xóa", "Removed")
}
pub fn toast_nothing_to_remove(lang: Lang) -> String {
    pick(lang, "Không có gì để xóa", "Nothing to remove")
}
pub fn toast_db_error(lang: Lang) -> String {
    pick(lang, "Lỗi DB", "DB error")
}
pub fn toast_unknown_action(lang: Lang) -> String {
    pick(lang, "Hành động không xác định", "Unknown action")
}

pub fn admin_only(lang: Lang) -> String {
    pick(lang, "⛔ Lệnh chỉ dành cho admin.", "⛔ Admin-only command.")
}
