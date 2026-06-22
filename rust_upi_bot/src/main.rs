//! UPI QR Bot — Rust binary cho OpenWrt aarch64.
//!
//! Pipeline:
//!   1. User /start → bot greeting.
//!   2. User upload session.json (Telegram document) → parse access_token + cookies.
//!   3. Job vào FIFO queue. Worker pool (max-concurrent) pickup khi có slot.
//!   4. Worker chạy UPI flow (steps 2-6) → render QR PNG → gửi cho user.
//!   5. Realtime log progress qua editMessageText (rate-limited).

mod bot;
mod http;
mod proxy_format;
mod random_profile;
mod settings;
mod stripe;
mod stripe_token;
mod upi;
mod user_agent;
mod auth;

use anyhow::{anyhow, Context, Result};
use clap::Parser;
use serde_json::Value;
use std::collections::HashSet;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::mpsc;
use tracing::{error, info, warn};

use crate::bot::board::{JobBoard, JobStatus};
use crate::bot::i18n::{self, Lang};
use crate::bot::limiter::{AdmitDecision, MessageDecision, UserLimiter};
use crate::bot::queue::{spawn_workers, Job, JobEvent, JobQueue, SubmitError, WorkerConfig};
use crate::bot::registry::JobRegistry;
use crate::bot::session_buffer::{AppendResult, SessionBuffer};
use crate::bot::telegram::{CallbackQuery, Message, TelegramClient};
use crate::http::HttpClient;
use crate::upi::runner::{AuthSource, UpiJobConfig};

#[derive(Parser, Debug, Clone)]
#[command(name = "upi-qr-bot", version)]
struct Cli {
    /// Sub-command. Empty = chạy bot (default).
    #[command(subcommand)]
    cmd: Option<SubCmd>,

    /// Telegram bot token (from @BotFather)
    #[arg(long, env = "TELEGRAM_TOKEN", default_value = "", global = true)]
    telegram_token: String,

    /// Comma-separated whitelist của user_id Telegram được phép. Empty = ai cũng dùng.
    #[arg(long, env = "ALLOWED_USERS", default_value = "", global = true)]
    allowed_users: String,

    /// Số worker chạy đồng thời. Job mới sẽ vào queue khi đầy.
    #[arg(long, env = "MAX_CONCURRENT", default_value = "100", global = true)]
    max_concurrent: usize,

    /// Số tiến trình tối đa 1 user chạy đồng thời.
    #[arg(long, env = "MAX_PER_USER", default_value = "10", global = true)]
    max_per_user: u32,

    /// Hard cap số job pending trong queue. Khi đầy, job mới bị reject. Bảo vệ RAM.
    #[arg(long, env = "QUEUE_CAPACITY", default_value = "50", global = true)]
    queue_capacity: usize,

    /// Số lần retry approve (per job).
    #[arg(long, env = "APPROVE_RETRIES", default_value = "110", global = true)]
    approve_retries: u32,

    /// Restart threshold — số `result=exception` LIÊN TIẾP trước khi restart checkout. 0 = disabled.
    #[arg(long, env = "RESTART_THRESHOLD", default_value = "20", global = true)]
    restart_threshold: u32,

    /// Số lần restart tối đa trong 1 job. 0 = disabled.
    #[arg(long, env = "MAX_RESTARTS", default_value = "3", global = true)]
    max_restarts: u32,

    /// Comma-separated proxy URLs. VD: "http://u:p@h1:8080,http://u:p@h2:8080".
    #[arg(long, env = "PROXY_POOL", default_value = "", global = true)]
    proxy_pool: String,

    /// 1-6, step bắt đầu áp proxy. Default 3 (login + checkout DIRECT).
    #[arg(long, env = "PROXY_FROM_STEP", default_value = "3", global = true)]
    proxy_from_step: u32,

    /// Cooldown (seconds) giữa 2 job cùng 1 user. Chống spam.
    #[arg(long, env = "USER_COOLDOWN_SECONDS", default_value = "10", global = true)]
    user_cooldown_seconds: u64,

    /// Max message tiếp nhận từ 1 user trong cửa sổ 60s. Vượt → tạm bỏ qua.
    /// Default 60 để cho paste JSON dài bị Telegram split thành nhiều chunks
    /// vẫn không bị drop.
    #[arg(long, env = "USER_MSG_RATE_PER_MIN", default_value = "60", global = true)]
    user_msg_rate_per_min: u32,

    /// Job hard timeout (seconds). Quá → kill job, free worker slot.
    #[arg(long, env = "JOB_TIMEOUT_SECONDS", default_value = "1800", global = true)]
    job_timeout_seconds: u64,

    /// TTL (seconds) cho buffer ghép multi-message text từ Telegram. Telegram
    /// chia tin nhắn dài thành nhiều chunks; bot ghép lại trong cửa sổ này.
    #[arg(long, env = "SESSION_BUFFER_TTL_SECONDS", default_value = "30", global = true)]
    session_buffer_ttl_seconds: u64,

    /// Watermark text vẽ phía dưới QR PNG (không đè lên QR). Empty = không vẽ.
    #[arg(long, env = "QR_WATERMARK", default_value = "@prr9293", global = true)]
    qr_watermark: String,

    /// Telegram user ID để nhận thông báo khi user KHÁC tạo QR thành công.
    /// 0 = disabled. Job của chính admin (cùng id) sẽ KHÔNG gửi notification.
    #[arg(long, env = "ADMIN_CHAT_ID", default_value = "0", global = true)]
    admin_chat_id: i64,

    /// SQLite Settings Store path.
    #[arg(long, env = "DB_PATH", default_value = "/overlay/upi-bot/state.db", global = true)]
    db_path: PathBuf,

    /// Output directory cho QR PNG.
    #[arg(long, env = "QR_OUT_DIR", default_value = "/tmp/upi-qr", global = true)]
    qr_out_dir: PathBuf,

    /// Cache directory cho Stripe bundles.
    #[arg(long, env = "BUNDLES_CACHE_DIR", default_value = "/tmp/upi-bot-bundles", global = true)]
    bundles_cache_dir: PathBuf,

    /// HTTP timeout (seconds).
    #[arg(long, env = "HTTP_TIMEOUT", default_value = "60", global = true)]
    http_timeout: u64,
}

#[derive(clap::Subcommand, Debug, Clone)]
enum SubCmd {
    /// Live probe: fetch Stripe bundles + extract token config. Verify
    /// TLS + token extraction trước khi run bot.
    StripeProbe,
    /// Run UPI flow 1 lần với session.json file local — không cần Telegram.
    /// Output JSON kết quả + path tới QR PNG.
    /// Run UPI flow 1 lần — nhận session.json HOẶC combo email|pass|2fa.
    /// Không cần Telegram. Output JSON kết quả + path tới QR PNG.
    RunOnce {
        /// Path tới session.json (dùng session có sẵn).
        #[arg(long)]
        session_json: Option<PathBuf>,
        /// Combo "email|password|totp_secret" (login HTTP lấy session).
        #[arg(long, default_value = "")]
        combo: String,
        /// Path xuất QR PNG (nếu có).
        #[arg(long, default_value = "/tmp/upi-runonce.png")]
        qr_out: PathBuf,
    },
    /// Test login HTTP từ combo `email|password|2fa` → in kết quả session.
    /// Không cần Telegram. Dùng để verify flow login JA3 (wreq/BoringSSL).
    LoginOnce {
        /// Combo "email|password|totp_secret"
        #[arg(long)]
        combo: String,
        /// Proxy URL cho login (optional). Empty = direct.
        #[arg(long, default_value = "")]
        proxy: String,
    },
}

fn parse_allowed_users(s: &str) -> HashSet<i64> {
    s.split(',')
        .filter_map(|t| t.trim().parse::<i64>().ok())
        .collect()
}

fn parse_proxy_pool(s: &str) -> Vec<String> {
    s.split(',')
        .map(|t| t.trim().to_string())
        .filter(|t| !t.is_empty())
        .collect()
}

/// Ngôn ngữ user (None nếu chưa chọn lần nào).
fn user_lang(store: &settings::Settings, user_id: i64) -> Option<Lang> {
    store.get_user_lang(user_id).and_then(|s| Lang::from_code(&s))
}

/// Fallback khi chưa rõ (chỉ dùng ở chỗ hiếm); mặc định tiếng Việt.
fn lang_or_default(store: &settings::Settings, user_id: i64) -> Lang {
    user_lang(store, user_id).unwrap_or(Lang::Vi)
}

/// Keyboard chọn ngôn ngữ (song ngữ).
fn language_keyboard() -> Value {
    serde_json::json!([[
        {"text": "🇻🇳 Tiếng Việt", "callback_data": "setlang:vi"},
        {"text": "🇬🇧 English", "callback_data": "setlang:en"}
    ]])
}

/// Keyboard nút Stop cho đúng 1 tiến trình (job_id).
fn stop_job_keyboard(lang: Lang, job_id: u64) -> Value {
    serde_json::json!([[
        {"text": i18n::btn_stop_this(lang), "callback_data": format!("stopjob:{}", job_id)}
    ]])
}

/// Keyboard menu cài đặt.
fn settings_keyboard(lang: Lang) -> Value {
    serde_json::json!([[
        {"text": i18n::btn_language(lang), "callback_data": "cmd:language"}
    ]])
}

/// Danh sách lệnh menu Telegram localized (set per-chat sau khi chọn ngôn ngữ).
/// Đầy đủ lệnh user; nếu là admin thì kèm nhóm lệnh admin.
fn localized_commands(lang: Lang, is_admin: bool) -> Vec<(&'static str, &'static str)> {
    let mut v = match lang {
        Lang::Vi => vec![
            ("start", "Bắt đầu / hướng dẫn"),
            ("status", "Trạng thái bot + hàng chờ"),
            ("stop", "Dừng tất cả tiến trình của bạn"),
            ("board", "Bảng tiến trình + nút Dừng"),
            ("cancel", "Xóa bộ đệm văn bản đang chờ"),
            ("proxy_set", "Đặt proxy riêng của bạn"),
            ("proxy_remove", "Xóa proxy của bạn"),
            ("settings", "Cài đặt"),
            ("language", "Đổi ngôn ngữ"),
            ("help", "Trợ giúp"),
        ],
        Lang::En => vec![
            ("start", "Start / instructions"),
            ("status", "Bot status + queue"),
            ("stop", "Stop all your processes"),
            ("board", "Process board + Stop buttons"),
            ("cancel", "Clear pending text buffer"),
            ("proxy_set", "Set your own proxy"),
            ("proxy_remove", "Remove your proxy"),
            ("settings", "Settings"),
            ("language", "Change language"),
            ("help", "Help"),
        ],
    };
    if is_admin {
        let admin = match lang {
            Lang::Vi => vec![
                ("notify", "Gửi thông báo tới mọi user (admin)"),
                ("chat", "Nhắn riêng 1 user (admin)"),
                ("ban", "Cấm user (admin)"),
                ("unban", "Gỡ cấm user (admin)"),
                ("banlist", "Danh sách user bị cấm (admin)"),
                ("stopall", "Dừng TẤT CẢ tiến trình mọi user (admin)"),
                ("flushall", "Xóa sạch hàng chờ + board (admin)"),
                ("proxy_login_set", "Đặt login proxy chung (admin)"),
                ("proxy_login_remove", "Xóa login proxy chung (admin)"),
            ],
            Lang::En => vec![
                ("notify", "Broadcast to all users (admin)"),
                ("chat", "Direct message a user (admin)"),
                ("ban", "Ban a user (admin)"),
                ("unban", "Unban a user (admin)"),
                ("banlist", "List banned users (admin)"),
                ("stopall", "Stop ALL processes of all users (admin)"),
                ("flushall", "Clear queue + board (admin)"),
                ("proxy_login_set", "Set shared login proxy (admin)"),
                ("proxy_login_remove", "Remove shared login proxy (admin)"),
            ],
        };
        v.extend(admin);
    }
    v
}

/// Validate JSON đúng shape của https://chatgpt.com/api/auth/session
/// (phải có `accessToken` non-empty + object `user`).
fn is_auth_session_json(v: &Value) -> bool {
    let has_token = v
        .get("accessToken")
        .and_then(|t| t.as_str())
        .map(|s| !s.is_empty())
        .unwrap_or(false);
    let has_user = v.get("user").map(|u| u.is_object()).unwrap_or(false);
    has_token && has_user
}

#[tokio::main(flavor = "multi_thread", worker_threads = 4)]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into()),
        )
        .with_target(false)
        .compact()
        .init();

    let cli = Cli::parse();
    info!(
        "starting upi-qr-bot — max_concurrent={} approve_retries={} restart={}/{} proxy_pool={} proxy_from_step={}",
        cli.max_concurrent,
        cli.approve_retries,
        cli.restart_threshold,
        cli.max_restarts,
        cli.proxy_pool.split(',').filter(|s| !s.trim().is_empty()).count(),
        cli.proxy_from_step,
    );

    let allowed_users = parse_allowed_users(&cli.allowed_users);
    let proxy_pool = parse_proxy_pool(&cli.proxy_pool);

    // Nâng giới hạn file descriptor cho nhiều tiến trình đồng thời (mỗi job mở
    // socket UPI + login). Chạy root nên set được cả hard limit (tới fs.nr_open).
    let want_nofile: u64 = (cli.max_concurrent as u64 * 64).clamp(8192, 262144);
    if let Err(e) = rlimit::Resource::NOFILE.set(want_nofile, want_nofile) {
        // Fallback: ít nhất nâng soft tới hard hiện có.
        let _ = rlimit::increase_nofile_limit(want_nofile);
        warn!("set nofile hard limit fail: {} (đã nâng soft tới hard)", e);
    }
    if let Ok((soft, hard)) = rlimit::Resource::NOFILE.get() {
        info!("nofile limit: soft={} hard={}", soft, hard);
    }

    std::fs::create_dir_all(&cli.qr_out_dir).ok();
    std::fs::create_dir_all(&cli.bundles_cache_dir).ok();
    if let Some(p) = cli.db_path.parent() {
        std::fs::create_dir_all(p).ok();
    }

    // Open Settings Store — giữ sống suốt runtime: chứa settings + danh sách
    // user kết nối (broadcast) + danh sách ban (theo user_id).
    let store = Arc::new(
        settings::Settings::open(&cli.db_path).context("failed to open SQLite settings store")?,
    );
    info!("settings store opened: {}", cli.db_path.display());

    let client = HttpClient::new(cli.http_timeout)?;

    // Sub-commands
    if let Some(cmd) = cli.cmd.clone() {
        match cmd {
            SubCmd::StripeProbe => return run_stripe_probe(client, cli.bundles_cache_dir.clone()).await,
            SubCmd::RunOnce { session_json, combo, qr_out } => {
                return run_once(client, &cli, &proxy_pool, session_json.as_deref(), &combo, &qr_out).await;
            }
            SubCmd::LoginOnce { combo, proxy } => {
                return run_login_once(&combo, &proxy).await;
            }
        }
    }

    if cli.telegram_token.is_empty() {
        return Err(anyhow!(
            "--telegram-token is required in bot mode (or use sub-command stripe-probe / run-once)"
        ));
    }
    let tg = Arc::new(TelegramClient::new(&cli.telegram_token)?);

    let (queue, queue_rx) = JobQueue::new(cli.queue_capacity);
    let queue = Arc::new(queue);
    let limiter = UserLimiter::new(
        Duration::from_secs(cli.user_cooldown_seconds),
        cli.user_msg_rate_per_min,
        cli.max_per_user,
    );
    let session_buffer = Arc::new(tokio::sync::Mutex::new(SessionBuffer::new(
        cli.session_buffer_ttl_seconds,
    )));
    let registry = JobRegistry::new();
    let board = JobBoard::new();
    let limiter_for_done = limiter.clone();
    let registry_for_done = registry.clone();
    let on_done: Arc<dyn Fn(i64, u64) + Send + Sync> = Arc::new(move |user_id: i64, job_id: u64| {
        let lim = limiter_for_done.clone();
        let reg = registry_for_done.clone();
        tokio::spawn(async move {
            lim.mark_done(user_id).await;
            reg.unregister(user_id, job_id).await;
        });
    });
    spawn_workers(
        client.clone(),
        queue_rx,
        on_done,
        WorkerConfig {
            max_concurrent: cli.max_concurrent,
            job_timeout: Duration::from_secs(cli.job_timeout_seconds),
        },
    );

    // Vacuum limiter + session buffer + registry mỗi 10 phút.
    {
        let lim = limiter.clone();
        let buf = session_buffer.clone();
        let reg = registry.clone();
        tokio::spawn(async move {
            let mut interval = tokio::time::interval(Duration::from_secs(600));
            interval.tick().await;
            loop {
                interval.tick().await;
                lim.vacuum().await;
                buf.lock().await.vacuum();
                reg.vacuum().await;
            }
        });
    }

    // Set bot commands menu (mặc định cho mọi user)
    let bot_commands: &[(&str, &str)] = &[
        ("start", "Open menu / Mở menu"),
        ("status", "Bot status"),
        ("stop", "Stop my processes"),
        ("board", "Process board / Bảng tiến trình"),
        ("settings", "Settings / Cài đặt"),
        ("language", "Language / Ngôn ngữ"),
        ("proxy_set", "Set your own proxy"),
        ("proxy_remove", "Remove your proxy"),
        ("help", "Show help"),
    ];
    if let Err(e) = tg.set_my_commands(bot_commands).await {
        warn!("setMyCommands warn: {}", e);
    }

    // Menu lệnh admin (scope theo chat admin) — chỉ admin thấy /notify, /ban...
    if cli.admin_chat_id != 0 {
        let admin_commands: &[(&str, &str)] = &[
            ("start", "Open menu"),
            ("status", "Bot status"),
            ("stop", "Cancel my running jobs"),
            ("cancel", "Clear pending text buffer"),
            ("proxy_set", "Set your own proxy"),
            ("proxy_remove", "Remove your proxy"),
            ("help", "Show help"),
            ("notify", "Broadcast to all users (admin)"),
            ("chat", "Direct message a user (admin)"),
            ("ban", "Ban user by @username/id (admin)"),
            ("unban", "Unban by @username/id (admin)"),
            ("banlist", "List banned users (admin)"),
            ("board", "Process board + Stop buttons"),
            ("stopall", "Stop ALL processes of all users (admin)"),
            ("flushall", "Clear queue + board (admin)"),
            ("proxy_login_set", "Set shared login proxy (admin)"),
            ("proxy_login_remove", "Remove shared login proxy (admin)"),
        ];
        if let Err(e) = tg
            .set_my_commands_for_chat(cli.admin_chat_id, admin_commands)
            .await
        {
            warn!("setMyCommands(admin) warn: {}", e);
        }
    }

    info!("bot ready, polling Telegram getUpdates...");
    let mut offset: i64 = 0;
    loop {
        match tg.get_updates(offset, 25).await {
            Ok(updates) => {
                for u in updates {
                    offset = u.update_id + 1;
                    if let Some(msg) = u.message {
                        let tg = tg.clone();
                        let queue = queue.clone();
                        let limiter = limiter.clone();
                        let session_buffer = session_buffer.clone();
                        let registry = registry.clone();
                        let store = store.clone();
                        let allowed = allowed_users.clone();
                        let proxy_pool = proxy_pool.clone();
                        let cli = cli.clone();
                        let board = board.clone();
                        tokio::spawn(async move {
                            if let Err(e) = handle_message(
                                tg,
                                queue,
                                limiter,
                                session_buffer,
                                registry,
                                store,
                                msg,
                                &allowed,
                                &proxy_pool,
                                &cli,
                                board,
                            )
                            .await
                            {
                                error!("handle_message error: {}", e);
                            }
                        });
                    } else if let Some(cb) = u.callback_query {
                        let tg = tg.clone();
                        let registry = registry.clone();
                        let session_buffer = session_buffer.clone();
                        let store = store.clone();
                        let allowed = allowed_users.clone();
                        let admin_chat_id = cli.admin_chat_id;
                        let board = board.clone();
                        tokio::spawn(async move {
                            if let Err(e) = handle_callback(
                                tg,
                                registry,
                                session_buffer,
                                store,
                                cb,
                                &allowed,
                                admin_chat_id,
                                board,
                            )
                            .await
                            {
                                error!("handle_callback error: {}", e);
                            }
                        });
                    }
                }
            }
            Err(e) => {
                warn!("getUpdates error: {} — sleeping 5s", e);
                tokio::time::sleep(Duration::from_secs(5)).await;
            }
        }
    }
}

async fn handle_message(
    tg: Arc<TelegramClient>,
    queue: Arc<JobQueue>,
    limiter: UserLimiter,
    session_buffer: Arc<tokio::sync::Mutex<SessionBuffer>>,
    registry: JobRegistry,
    store: Arc<settings::Settings>,
    msg: Message,
    allowed: &HashSet<i64>,
    proxy_pool: &[String],
    cli: &Cli,
    board: JobBoard,
) -> Result<()> {
    let user_id = msg.from.as_ref().map(|u| u.id).unwrap_or(0);
    let username = msg.from.as_ref().and_then(|u| u.username.clone());
    let first_name = msg.from.as_ref().and_then(|u| u.first_name.clone());

    // Ghi nhận user kết nối (nguồn danh sách broadcast). Username có thể đổi —
    // lưu theo user_id bền vững. Bỏ qua message không có sender (channel, ...).
    if user_id != 0 {
        if let Err(e) = store.record_user(
            user_id,
            username.as_deref(),
            first_name.as_deref(),
            msg.chat.id,
        ) {
            tracing::warn!(user_id, "record_user fail: {}", e);
        }
    }

    let is_admin = cli.admin_chat_id != 0 && user_id == cli.admin_chat_id;

    // Anti-flood: register message → drop nếu vượt rate.
    // SKIP nếu user đang có buffer pending (chunks tiếp theo của paste dài,
    // không phải intent mới — Telegram split message > ~4096 chars).
    let is_chunk_continuation = session_buffer.lock().await.has_pending(msg.chat.id);
    if !is_chunk_continuation {
        match limiter.register_message(user_id).await {
            MessageDecision::Allow => {}
            MessageDecision::Drop { observed, limit } => {
                tracing::warn!(
                    user_id,
                    observed,
                    limit,
                    "message dropped (rate limit exceeded)"
                );
                return Ok(());
            }
        }
    }

    // Ban check — banned user (theo user_id) bị chặn hoàn toàn. Admin miễn nhiễm.
    if !is_admin {
        match store.is_banned(user_id) {
            Ok(true) => {
                tg.send_message(
                    msg.chat.id,
                    &i18n::banned(lang_or_default(&store, user_id)),
                    Some(msg.message_id),
                )
                .await
                .ok();
                return Ok(());
            }
            Ok(false) => {}
            Err(e) => tracing::warn!(user_id, "is_banned check fail: {}", e),
        }
    }

    let lang_opt = user_lang(&store, user_id);
    if !allowed.is_empty() && !allowed.contains(&user_id) {
        tg.send_message(
            msg.chat.id,
            &i18n::not_whitelisted(lang_opt.unwrap_or(Lang::Vi)),
            Some(msg.message_id),
        )
        .await
        .ok();
        return Ok(());
    }

    // ── Cổng ngôn ngữ: user PHẢI chọn Việt/Anh trước khi dùng bot ──
    let lang = match lang_opt {
        Some(l) => l,
        None => {
            tg.send_message_kb(
                msg.chat.id,
                i18n::choose_language(),
                None,
                language_keyboard(),
            )
            .await
            .ok();
            return Ok(());
        }
    };

    // Commands
    if let Some(text) = &msg.text {
        let trimmed = text.trim();
        if trimmed.starts_with("/start") {
            // Clear buffer khi /start để không gộp nhầm session cũ
            session_buffer.lock().await.clear(msg.chat.id);
            // Refresh menu lệnh localized đầy đủ (kèm admin nếu là admin).
            tg.set_my_commands_for_chat(msg.chat.id, &localized_commands(lang, is_admin))
                .await
                .ok();
            send_welcome(&tg, msg.chat.id, msg.message_id, lang).await;
            return Ok(());
        }
        if trimmed.starts_with("/language") || trimmed.starts_with("/lang") {
            tg.send_message_kb(msg.chat.id, i18n::choose_language(), None, language_keyboard())
                .await
                .ok();
            return Ok(());
        }
        if trimmed.starts_with("/settings") {
            tg.send_message_kb(
                msg.chat.id,
                &i18n::settings_title(lang),
                None,
                settings_keyboard(lang),
            )
            .await
            .ok();
            return Ok(());
        }
        if trimmed.starts_with("/help") {
            send_help(&tg, &store, msg.chat.id, msg.message_id, user_id, is_admin, lang).await;
            return Ok(());
        }
        if trimmed.starts_with("/status") {
            let pending = queue.pending();
            tg.send_message(
                msg.chat.id,
                &format!("{} · queue {}/{}", i18n::status_online(lang), pending, cli.queue_capacity),
                None,
            )
            .await
            .ok();
            return Ok(());
        }
        if trimmed.starts_with("/board") {
            // Board cho mọi user: admin xem toàn hệ thống, user thường CHỈ xem
            // tiến trình của chính mình (bảo mật). Kèm nút Stop từng process.
            handle_board(&tg, &board, msg.chat.id, user_id, is_admin, lang).await;
            return Ok(());
        }
        if trimmed.starts_with("/cancel") {
            session_buffer.lock().await.clear(msg.chat.id);
            tg.send_message(msg.chat.id, &i18n::stopped_all(lang, 0), None)
                .await
                .ok();
            return Ok(());
        }
        if trimmed.starts_with("/stop") {
            let stopped = registry.stop_user(user_id).await;
            session_buffer.lock().await.clear(msg.chat.id);
            let body = i18n::stopped_all(lang, stopped);
            tg.send_message(msg.chat.id, &body, Some(msg.message_id))
                .await
                .ok();
            return Ok(());
        }

        // ── Admin commands ────────────────────────────────────────────────
        // Match chính xác token lệnh (strip @botname) để /ban không nuốt /banlist.
        let cmd_base = trimmed
            .split_whitespace()
            .next()
            .unwrap_or("")
            .split('@')
            .next()
            .unwrap_or("");

        // ── Proxy commands (mọi user) ─────────────────────────────────────
        match cmd_base {
            "/proxy_set" => {
                handle_proxy_set(
                    &tg,
                    &store,
                    &msg,
                    text,
                    user_id,
                    &username,
                    cli.admin_chat_id,
                    cli.proxy_from_step,
                    lang,
                )
                .await;
                return Ok(());
            }
            "/proxy_remove" => {
                handle_proxy_remove(&tg, &store, &msg, user_id, lang).await;
                return Ok(());
            }
            _ => {}
        }

        match cmd_base {
            "/notify" | "/chat" | "/ban" | "/unban" | "/banlist"
            | "/proxy_login_set" | "/proxy_login_remove" | "/stopall" | "/flushall" => {
                if !is_admin {
                    tg.send_message(
                        msg.chat.id,
                        &i18n::admin_only(lang),
                        Some(msg.message_id),
                    )
                    .await
                    .ok();
                    return Ok(());
                }
                match cmd_base {
                    "/notify" => handle_notify(&tg, &store, &msg, text).await,
                    "/chat" => handle_chat(&tg, &store, &msg, text).await,
                    "/ban" => {
                        handle_ban(&tg, &store, &registry, &msg, text, cli.admin_chat_id).await
                    }
                    "/unban" => handle_unban(&tg, &store, &msg, text).await,
                    "/banlist" => handle_banlist(&tg, &store, &msg).await,
                    "/proxy_login_set" => {
                        handle_proxy_login_set(&tg, &store, &msg, text, cli.proxy_from_step).await
                    }
                    "/proxy_login_remove" => {
                        handle_proxy_login_remove(&tg, &store, &msg).await
                    }
                    "/stopall" => handle_stopall(&tg, &registry, msg.chat.id).await,
                    "/flushall" => {
                        handle_flushall(&tg, &registry, &session_buffer, &board, msg.chat.id).await
                    }
                    _ => unreachable!(),
                }
                return Ok(());
            }
            _ => {}
        }

        if trimmed.starts_with('/') {
            let cmd = trimmed.split_whitespace().next().unwrap_or(trimmed);
            tg.send_message(msg.chat.id, &i18n::unknown_command(lang, cmd), Some(msg.message_id))
                .await
                .ok();
            return Ok(());
        }

        // Text thường — append vào session buffer
        if !trimmed.is_empty() {
            // Auto-detect combo email|password|2fa (không phải JSON) → login HTTP.
            // Hỗ trợ NHIỀU dòng: mỗi dòng = 1 tài khoản → 1 tiến trình riêng.
            if looks_like_combo_input(trimmed) {
                let (combos, invalid) = parse_account_combos(trimmed);
                if combos.is_empty() {
                    tg.send_message(msg.chat.id, &i18n::invalid_combo(lang), Some(msg.message_id))
                        .await
                        .ok();
                    return Ok(());
                }
                session_buffer.lock().await.clear(msg.chat.id);

                // Cap số dòng xử lý 1 lần = max tiến trình/user (chống flood khi
                // dán quá nhiều dòng). Phần dư bị bỏ, báo rõ trong header.
                let cap = cli.max_per_user.max(1) as usize;
                let dropped = combos.len().saturating_sub(cap);
                let combos: Vec<AccountCombo> = combos.into_iter().take(cap).collect();

                if combos.len() > 1 || invalid > 0 || dropped > 0 {
                    tg.send_message(
                        msg.chat.id,
                        &i18n::combo_batch_received(lang, combos.len(), invalid, dropped),
                        Some(msg.message_id),
                    )
                    .await
                    .ok();
                }

                // Mỗi tài khoản 1 job độc lập — admission (cap/user) + chống
                // trùng tài khoản tự áp trong enqueue_and_track. Lỗi 1 dòng
                // không chặn các dòng còn lại.
                for combo in combos {
                    if let Err(e) = process_account_combo(
                        tg.clone(),
                        queue.clone(),
                        limiter.clone(),
                        registry.clone(),
                        store.clone(),
                        msg.chat.id,
                        msg.message_id,
                        user_id,
                        username.clone(),
                        combo,
                        proxy_pool,
                        cli,
                        board.clone(),
                        lang,
                    )
                    .await
                    {
                        error!("process_account_combo (batch) error: {}", e);
                    }
                }
                return Ok(());
            }
            let result = {
                let mut buf = session_buffer.lock().await;
                buf.append(msg.chat.id, text)
            };
            match result {
                AppendResult::Ready(raw) => {
                    return process_session_json(
                        tg,
                        queue,
                        limiter,
                        registry,
                        store.clone(),
                        msg.chat.id,
                        msg.message_id,
                        user_id,
                        username,
                        raw,
                        proxy_pool,
                        cli,
                        board,
                        lang,
                    )
                    .await;
                }
                AppendResult::Pending => {
                    // Im lặng — đợi chunk tiếp. Nếu user paste 1 lần bị split
                    // bởi Telegram → các message kế tiếp sẽ ghép lại trong TTL.
                    return Ok(());
                }
                AppendResult::Invalid(_reason) => {
                    tg.send_message(
                        msg.chat.id,
                        &i18n::invalid_session_json(lang),
                        Some(msg.message_id),
                    )
                    .await
                    .ok();
                    return Ok(());
                }
            }
        }
        return Ok(());
    }

    // Document upload
    let Some(doc) = msg.document.clone() else {
        tg.send_message(
            msg.chat.id,
            &i18n::need_input(lang),
            Some(msg.message_id),
        )
        .await
        .ok();
        return Ok(());
    };

    let file_name = doc.file_name.unwrap_or_else(|| "session.json".into());
    if !file_name.to_lowercase().ends_with(".json") {
        tg.send_message(msg.chat.id, &i18n::invalid_session_json(lang), Some(msg.message_id))
            .await
            .ok();
        return Ok(());
    }
    if doc.file_size.unwrap_or(0) > 1_500_000 {
        tg.send_message(
            msg.chat.id,
            &i18n::invalid_session_json(lang),
            Some(msg.message_id),
        )
        .await
        .ok();
        return Ok(());
    }

    let file_path = tg.get_file_path(&doc.file_id).await?;
    let bytes = tg.download_file(&file_path).await?;
    let raw = String::from_utf8(bytes.to_vec())
        .unwrap_or_else(|e| String::from_utf8_lossy(e.as_bytes()).into_owned());
    process_session_json(
        tg,
        queue,
        limiter,
        registry,
        store,
        msg.chat.id,
        msg.message_id,
        user_id,
        username,
        raw,
        proxy_pool,
        cli,
        board,
        lang,
    )
    .await
}

#[allow(clippy::too_many_arguments)]
async fn enqueue_and_track(
    tg: Arc<TelegramClient>,
    queue: Arc<JobQueue>,
    limiter: UserLimiter,
    registry: JobRegistry,
    store: Arc<settings::Settings>,
    chat_id: i64,
    reply_to: i64,
    user_id: i64,
    username: Option<String>,
    email: String,
    auth: AuthSource,
    proxy_pool: &[String],
    cli: &Cli,
    board: JobBoard,
    lang: Lang,
) -> Result<()> {
    // Admission: tối đa N tiến trình/user (mặc định 10). try_admit RESERVE slot
    // atomic ngay khi Allow → không còn khe TOCTOU với preflight proxy phía sau.
    match limiter.try_admit(user_id).await {
        AdmitDecision::Allow => {}
        AdmitDecision::MaxConcurrent { max } => {
            tg.send_message(chat_id, &i18n::max_concurrent(lang, max), Some(reply_to))
                .await
                .ok();
            return Ok(());
        }
        AdmitDecision::Cooldown { remaining_secs } => {
            tg.send_message(chat_id, &i18n::cooldown(lang, remaining_secs), Some(reply_to))
                .await
                .ok();
            return Ok(());
        }
    }

    // ── Chống trùng tài khoản: 1 user không được chạy/queue cùng 1 email 2 lần.
    // Đăng ký job NGAY (atomic) để giữ chỗ email_key trước mọi await (preflight
    // proxy) → không có khe TOCTOU. Mọi đường return sớm sau đây PHẢI nhả cả
    // reservation (limiter.release) lẫn entry registry (unregister).
    let masked_email = upi::runner::mask_email(&email);
    let email_key = email.trim().to_lowercase();
    let (job_id, cancel_token) = match registry.try_register(user_id, &email_key).await {
        Some(v) => v,
        None => {
            limiter.release(user_id).await;
            tg.send_message(
                chat_id,
                &i18n::duplicate_account(lang, &masked_email),
                Some(reply_to),
            )
            .await
            .ok();
            return Ok(());
        }
    };

    // ── Proxy resolution: login proxy (admin, global) + user proxy (private) ──
    let login_proxy_raw = store.get_login_proxy();
    let user_proxy_raw = match store.get_user_proxy(user_id) {
        Ok(v) => v,
        Err(e) => {
            tracing::warn!(user_id, "get_user_proxy fail: {}", e);
            None
        }
    };

    // Login proxy materialize 1 lần (segment login: step < proxy_from_step).
    let login_proxy_url: Option<String> = match login_proxy_raw.as_deref() {
        Some(raw) => match proxy_format::materialize_for_client(raw, 8) {
            Ok(url) => Some(url),
            Err(e) => {
                tracing::warn!(user_id, "login proxy materialize fail: {}", e);
                None
            }
        },
        None => None,
    };

    // effective_pool = segment "work" (từ proxy_from_step trở đi):
    //   - user có proxy riêng        → [user_proxy]   (login proxy chỉ ở segment login)
    //   - user KHÔNG có, có login px  → [login_proxy]  (login proxy phủ toàn flow)
    //   - không có cả 2              → pool global (hành vi cũ / DIRECT)
    let effective_pool: Vec<String> = match user_proxy_raw.as_deref() {
        Some(raw) => match proxy_format::materialize_for_client(raw, 8) {
            Ok(url) => {
                tracing::info!(
                    user_id,
                    proxy = %proxy_format::mask_proxy(&url),
                    "user proxy override active"
                );
                vec![url]
            }
            Err(e) => {
                tracing::warn!(user_id, "user proxy materialize fail: {} — fallback", e);
                login_proxy_url
                    .clone()
                    .map(|u| vec![u])
                    .unwrap_or_else(|| proxy_pool.to_vec())
            }
        },
        None => match &login_proxy_url {
            Some(u) => vec![u.clone()],
            None => proxy_pool.to_vec(),
        },
    };

    // ── Pre-flight proxy check (cache 5'); fail-fast nếu proxy chết ──────
    // Chỉ probe proxy do feature này quản lý (login proxy + user proxy raw);
    // pool global thuần (env) giữ hành vi cũ, không pre-flight.
    let probe_login_raw = login_proxy_raw.clone().filter(|_| login_proxy_url.is_some());
    let probe_user_raw = user_proxy_raw.clone();
    let (login_status, user_status) = tokio::join!(
        async {
            match &probe_login_raw {
                Some(r) => Some(bot::proxy_status::PROXY_STATUS.get_or_probe(r).await),
                None => None,
            }
        },
        async {
            match &probe_user_raw {
                Some(r) => Some(bot::proxy_status::PROXY_STATUS.get_or_probe(r).await),
                None => None,
            }
        },
    );

    let mut proxy_lines: Vec<String> = Vec::new();
    let mut proxy_dead = false;
    if let Some(st) = &login_status {
        proxy_lines.push(build_proxy_line(lang, "Login", st));
        if !st.ok {
            proxy_dead = true;
        }
    }
    if let Some(st) = &user_status {
        proxy_lines.push(build_proxy_line(lang, "User", st));
        if !st.ok {
            proxy_dead = true;
        }
    }
    if proxy_dead {
        // Đã reserve slot (try_admit) + try_register ở trên → phải nhả cả hai
        // trước khi return, nếu không slot/email_key sẽ kẹt vĩnh viễn.
        registry.unregister(user_id, job_id).await;
        limiter.release(user_id).await;
        // Chưa vào queue, chưa chạy → chỉ báo user (và admin) rồi
        // dừng. Không tốn slot/worker.
        tg.send_message(
            chat_id,
            &bot::proc_view::render_preflight_blocked(lang, &masked_email, &proxy_lines),
            Some(reply_to),
        )
        .await
        .ok();
        if cli.admin_chat_id != 0 && user_id != cli.admin_chat_id {
            let note = format!(
                "⛔ Job bị chặn (proxy chết)\nTừ: @{} (id {})\nEmail: {}\n{}",
                username.as_deref().unwrap_or("-"),
                user_id,
                masked_email,
                proxy_lines.join("\n"),
            );
            tg.send_message(cli.admin_chat_id, &note, None).await.ok();
        }
        return Ok(());
    }
    if !proxy_lines.is_empty() {
        tg.send_message(
            chat_id,
            &bot::proc_view::render_preflight_ok(lang, &masked_email, &proxy_lines),
            None,
        )
        .await
        .ok();
    }

    // Đăng ký job đã thực hiện ở đầu hàm (try_register) — job_id/cancel_token
    // đã có sẵn. Chỉ tạo channel sự kiện ở đây.
    let (event_tx, mut event_rx) = mpsc::unbounded_channel::<JobEvent>();
    let qr_path = cli
        .qr_out_dir
        .join(format!("qr_{}_{}.png", user_id, job_id));

    // Nhãn nguồn auth (để báo admin) — lấy trước khi move vào job_config.
    let auth_kind = match &auth {
        AuthSource::Login { .. } => "combo (email|pass|2fa)",
        AuthSource::Session { .. } => "session.json",
    };

    let job_config = UpiJobConfig {
        email: email.clone(),
        auth,
        proxy_pool: effective_pool,
        login_proxy: login_proxy_url,
        approve_retries: cli.approve_retries,
        restart_threshold: cli.restart_threshold,
        max_restarts: cli.max_restarts,
        proxy_from_step: cli.proxy_from_step,
        qr_out_path: qr_path.clone(),
        bundles_cache_dir: cli.bundles_cache_dir.clone(),
        qr_watermark: cli.qr_watermark.clone(),
    };

    // Tin riêng cho tiến trình này, kèm nút Stop mang job_id.
    let status_msg_id = tg
        .send_message_kb(
            chat_id,
            &i18n::job_received(lang, &masked_email),
            Some(reply_to),
            stop_job_keyboard(lang, job_id),
        )
        .await?;

    let username_for_log = username.clone();
    let job = Job {
        user_id,
        job_id,
        chat_id,
        username,
        config: job_config,
        log_tx: event_tx,
        cancel: cancel_token.clone(),
    };

    match queue.try_submit(job) {
        Ok(position) => {
            // Slot đã reserve ở try_admit — KHÔNG tăng lại. on_done → mark_done
            // sẽ trả slot khi job kết thúc.
            board
                .insert_queued(job_id, user_id, username_for_log.clone(), masked_email.clone())
                .await;
            tg.edit_message_text_kb(
                chat_id,
                status_msg_id,
                &bot::proc_view::render_queued(lang, &masked_email, position),
                stop_job_keyboard(lang, job_id),
            )
            .await
            .ok();
            // Báo admin khi user KHÁC tạo tiến trình mới (skip nếu chính admin).
            if cli.admin_chat_id != 0 && user_id != cli.admin_chat_id {
                let note = format!(
                    "🆕 Tiến trình mới\nTừ: @{} (id {})\nEmail: {}\nNguồn: {}\nHàng chờ: ~{}",
                    username_for_log.as_deref().unwrap_or("-"),
                    user_id,
                    masked_email,
                    auth_kind,
                    position,
                );
                if let Err(e) = tg.send_message(cli.admin_chat_id, &note, None).await {
                    tracing::warn!(admin = cli.admin_chat_id, "admin new-process notify fail: {}", e);
                }
            }
        }
        Err(SubmitError::QueueFull { pending, capacity }) => {
            registry.unregister(user_id, job_id).await;
            limiter.release(user_id).await;
            tg.edit_message_text(chat_id, status_msg_id, &i18n::queue_full(lang, pending, capacity))
                .await
                .ok();
            return Ok(());
        }
        Err(SubmitError::Closed) => {
            registry.unregister(user_id, job_id).await;
            limiter.release(user_id).await;
            tg.edit_message_text(chat_id, status_msg_id, &i18n::queue_closed(lang))
                .await
                .ok();
            return Ok(());
        }
    }

    let tg_for_log = tg.clone();
    let qr_path_for_send = qr_path.clone();
    let admin_chat_id = cli.admin_chat_id;
    let board_for_task = board;
    let board_job_id = job_id;
    let email_for_done = masked_email.clone();
    tokio::spawn(async move {
        let mut last_edit = std::time::Instant::now()
            .checked_sub(Duration::from_secs(60))
            .unwrap_or_else(std::time::Instant::now);
        let mut started_at = std::time::Instant::now();
        let mut view = bot::proc_view::ProcView::new(email_for_done.clone());
        let mut running = false;
        let mut tick = tokio::time::interval(Duration::from_secs(3));
        tick.tick().await; // first tick fires immediately — consume it.

        loop {
            tokio::select! {
                event = event_rx.recv() => {
                    let Some(event) = event else { break; };
                    match event {
                        JobEvent::Queued { position } => {
                            tg_for_log
                                .edit_message_text_kb(
                                    chat_id,
                                    status_msg_id,
                                    &bot::proc_view::render_queued(lang, &email_for_done, position),
                                    stop_job_keyboard(lang, board_job_id),
                                )
                                .await
                                .ok();
                        }
                        JobEvent::Started => {
                            started_at = std::time::Instant::now();
                            running = true;
                            board_for_task.mark_running(board_job_id).await;
                            tg_for_log
                                .edit_message_text_kb(
                                    chat_id,
                                    status_msg_id,
                                    &bot::proc_view::render_your_turn(lang, &email_for_done),
                                    stop_job_keyboard(lang, board_job_id),
                                )
                                .await
                                .ok();
                            last_edit = std::time::Instant::now();
                        }
                        JobEvent::Log(line) => {
                            // Board admin giữ log thô; user xem thẻ tiến trình gọn.
                            board_for_task.set_step(board_job_id, line.clone()).await;
                            view.update(&line);
                            running = true;
                            if last_edit.elapsed() > Duration::from_millis(2500) {
                                let body = view.render_running(lang, started_at.elapsed().as_secs_f64());
                                tg_for_log
                                    .edit_message_text_kb(
                                        chat_id,
                                        status_msg_id,
                                        &body,
                                        stop_job_keyboard(lang, board_job_id),
                                    )
                                    .await
                                    .ok();
                                last_edit = std::time::Instant::now();
                            }
                        }
                        JobEvent::Done(result) => {
                            board_for_task.remove(board_job_id).await;
                            let expires = format_expires(result.qr_expires_at);
                            let attempts = result.approve_attempts.len();
                            let body = if result.ok {
                                bot::proc_view::render_done_ok(
                                    lang,
                                    &email_for_done,
                                    result.elapsed_seconds,
                                    &expires,
                                    attempts,
                                )
                            } else {
                                let raw = result
                                    .error
                                    .clone()
                                    .or_else(|| result.qr_reason.clone())
                                    .unwrap_or_default();
                                bot::proc_view::render_done_fail(
                                    lang,
                                    &email_for_done,
                                    result.elapsed_seconds,
                                    &raw,
                                    attempts,
                                )
                            };
                            // Tin terminal: BỎ nút Stop (edit không kèm keyboard).
                            tg_for_log
                                .edit_message_text(chat_id, status_msg_id, &body)
                                .await
                                .ok();
                            if let Some(qr) = result.qr_path.as_deref() {
                                let path = std::path::Path::new(qr);
                                if path.exists() {
                                    let caption = i18n::qr_caption(lang, &email_for_done, &expires);
                                    if let Err(e) = tg_for_log
                                        .send_photo(chat_id, path, Some(&caption), None)
                                        .await
                                    {
                                        tracing::warn!("sendPhoto fail: {}", e);
                                    }
                                }
                            }
                            // Gửi link thanh toán ở 1 tin nhắn MỚI (không sửa tin cũ).
                            // return_url dạng https://checkout.stripe.com/c/pay/{cs_...};
                            // đổi host → pay.openai.com cho khớp convention payment_link.py.
                            if result.ok && result.return_url.contains("/c/pay/") {
                                let pay_url = result
                                    .return_url
                                    .replace("checkout.stripe.com", "pay.openai.com");
                                let pay_msg = i18n::payment_link_msg(lang, &pay_url);
                                if let Err(e) =
                                    tg_for_log.send_message(chat_id, &pay_msg, None).await
                                {
                                    tracing::warn!("send payment link fail: {}", e);
                                }
                            }
                            // Notify admin khi user KHÁC tạo QR thành công.
                            if result.ok && admin_chat_id != 0 && user_id != admin_chat_id {
                                let summary = format!(
                                    "🆕 QR success\nFrom: @{} (id {})\nEmail: {}\nElapsed: {:.1}s",
                                    username_for_log.as_deref().unwrap_or("-"),
                                    user_id,
                                    result.email,
                                    result.elapsed_seconds,
                                );
                                if let Err(e) = tg_for_log.send_message(admin_chat_id, &summary, None).await {
                                    tracing::warn!(admin_chat_id, "admin notify fail: {}", e);
                                }
                            }
                            crate::bot::queue::cleanup_qr_artifacts(&qr_path_for_send);
                            break;
                        }
                        JobEvent::Timeout => {
                            board_for_task.remove(board_job_id).await;
                            tg_for_log
                                .edit_message_text(
                                    chat_id,
                                    status_msg_id,
                                    &bot::proc_view::render_timeout(
                                        lang,
                                        &email_for_done,
                                        started_at.elapsed().as_secs_f64(),
                                    ),
                                )
                                .await
                                .ok();
                            crate::bot::queue::cleanup_qr_artifacts(&qr_path_for_send);
                            break;
                        }
                        JobEvent::Cancelled => {
                            board_for_task.remove(board_job_id).await;
                            tg_for_log
                                .edit_message_text(
                                    chat_id,
                                    status_msg_id,
                                    &bot::proc_view::render_stopped(
                                        lang,
                                        &email_for_done,
                                        started_at.elapsed().as_secs_f64(),
                                    ),
                                )
                                .await
                                .ok();
                            crate::bot::queue::cleanup_qr_artifacts(&qr_path_for_send);
                            break;
                        }
                    }
                }
                _ = tick.tick() => {
                    // Nhịp tim — cập nhật đồng hồ dù không có log mới.
                    if running && last_edit.elapsed() > Duration::from_millis(2500) {
                        let body = view.render_running(lang, started_at.elapsed().as_secs_f64());
                        tg_for_log
                            .edit_message_text_kb(
                                chat_id,
                                status_msg_id,
                                &body,
                                stop_job_keyboard(lang, board_job_id),
                            )
                            .await
                            .ok();
                        last_edit = std::time::Instant::now();
                    }
                }
            }
        }
    });

    Ok(())
}

/// Combo tài khoản dạng `email|password|totp_secret`.
struct AccountCombo {
    email: String,
    password: String,
    totp_secret: String,
}

/// Heuristic: text trông giống combo (không phải JSON, có '|' và '@').
fn looks_like_combo_input(text: &str) -> bool {
    let t = text.trim();
    !t.starts_with('{') && !t.starts_with('[') && t.contains('|') && t.contains('@')
}

/// Parse 1 dòng `email|password|totp_secret`. Yêu cầu đúng 1 tài khoản/lần
/// (multi-line bị từ chối ở caller). Trả None nếu format sai.
fn parse_account_combo(text: &str) -> Option<AccountCombo> {
    let lines: Vec<&str> = text
        .lines()
        .map(|l| l.trim())
        .filter(|l| !l.is_empty())
        .collect();
    if lines.len() != 1 {
        return None;
    }
    let parts: Vec<&str> = lines[0].split('|').collect();
    if parts.len() < 3 {
        return None;
    }
    let email = parts[0].trim();
    let password = parts[1].trim();
    let secret = parts[2].trim();
    if email.is_empty() || password.is_empty() || secret.is_empty() || !email.contains('@') {
        return None;
    }
    Some(AccountCombo {
        email: email.to_string(),
        password: password.to_string(),
        totp_secret: secret.to_string(),
    })
}

/// Parse NHIỀU dòng combo `email|password|2fa` (mỗi dòng 1 tài khoản). Bỏ qua
/// dòng rỗng, dedupe theo email (giữ lần đầu) để 1 lần dán không tạo 2 job
/// trùng. Trả `(combos hợp lệ, số dòng sai định dạng)`.
fn parse_account_combos(text: &str) -> (Vec<AccountCombo>, usize) {
    let mut out: Vec<AccountCombo> = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    let mut invalid = 0usize;
    for line in text.lines() {
        let l = line.trim();
        if l.is_empty() {
            continue;
        }
        match parse_account_combo(l) {
            Some(c) => {
                let key = c.email.trim().to_lowercase();
                if seen.insert(key) {
                    out.push(c);
                }
            }
            None => invalid += 1,
        }
    }
    (out, invalid)
}

/// Nhận session.json (file hoặc paste text) → build job với session có sẵn.
#[allow(clippy::too_many_arguments)]
async fn process_session_json(
    tg: Arc<TelegramClient>,
    queue: Arc<JobQueue>,
    limiter: UserLimiter,
    registry: JobRegistry,
    store: Arc<settings::Settings>,
    chat_id: i64,
    reply_to: i64,
    user_id: i64,
    username: Option<String>,
    raw: String,
    proxy_pool: &[String],
    cli: &Cli,
    board: JobBoard,
    lang: Lang,
) -> Result<()> {
    // Chỉ nhận JSON đúng shape của https://chatgpt.com/api/auth/session.
    let session_json: Value = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(_) => {
            tg.send_message(chat_id, &i18n::invalid_session_json(lang), Some(reply_to))
                .await
                .ok();
            return Ok(());
        }
    };
    if !is_auth_session_json(&session_json) {
        tg.send_message(chat_id, &i18n::invalid_session_json(lang), Some(reply_to))
            .await
            .ok();
        return Ok(());
    }
    let access_token = session_json
        .get("accessToken")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    // Session BẮT BUỘC có email thật (user.email) — đây là khóa định danh để
    // chống trùng tài khoản. Không fallback "unknown@unknown" (sẽ làm dedupe sai
    // và job vô danh). Thiếu/email sai định dạng → từ chối, báo user.
    let email = match session_json
        .get("user")
        .and_then(|u| u.get("email"))
        .and_then(|e| e.as_str())
        .map(|s| s.trim())
        .filter(|s| !s.is_empty() && s.contains('@'))
    {
        Some(e) => e.to_string(),
        None => {
            tg.send_message(chat_id, &i18n::session_no_email(lang), Some(reply_to))
                .await
                .ok();
            return Ok(());
        }
    };
    let cookie_header = build_cookie_header(&session_json);

    enqueue_and_track(
        tg,
        queue,
        limiter,
        registry,
        store,
        chat_id,
        reply_to,
        user_id,
        username,
        email,
        AuthSource::Session {
            access_token,
            cookie_header,
        },
        proxy_pool,
        cli,
        board,
        lang,
    )
    .await
}

/// Nhận combo `email|password|2fa` → login HTTP ở step [1/6] rồi chạy UPI.
#[allow(clippy::too_many_arguments)]
async fn process_account_combo(
    tg: Arc<TelegramClient>,
    queue: Arc<JobQueue>,
    limiter: UserLimiter,
    registry: JobRegistry,
    store: Arc<settings::Settings>,
    chat_id: i64,
    reply_to: i64,
    user_id: i64,
    username: Option<String>,
    combo: AccountCombo,
    proxy_pool: &[String],
    cli: &Cli,
    board: JobBoard,
    lang: Lang,
) -> Result<()> {
    enqueue_and_track(
        tg,
        queue,
        limiter,
        registry,
        store,
        chat_id,
        reply_to,
        user_id,
        username,
        combo.email,
        AuthSource::Login {
            password: combo.password,
            totp_secret: combo.totp_secret,
        },
        proxy_pool,
        cli,
        board,
        lang,
    )
    .await
}

/// Giờ VN dạng HH:MM:SS cho nhãn cập nhật của board.
fn now_hms() -> String {
    let vn = chrono::FixedOffset::east_opt(7 * 3600).unwrap();
    chrono::Utc::now()
        .with_timezone(&vn)
        .format("%H:%M:%S")
        .to_string()
}

/// `/stopall` (admin) — cancel MỌI job của MỌI user. Slot limiter tự trả qua
/// `on_done` khi worker xử lý xong các job đã cancel.
async fn handle_stopall(tg: &Arc<TelegramClient>, registry: &JobRegistry, chat_id: i64) {
    let n = registry.stop_everyone().await;
    tg.send_message(
        chat_id,
        &format!("🛑 Stopped {} process(es) across all users.", n),
        None,
    )
    .await
    .ok();
}

/// `/flushall` (admin) — reset toàn bộ trạng thái tạm: cancel mọi job, xóa
/// board + buffer text đang chờ. KHÔNG zero cứng counter limiter (slot trả tự
/// nhiên qua `on_done`) để tránh lệch số đếm.
async fn handle_flushall(
    tg: &Arc<TelegramClient>,
    registry: &JobRegistry,
    session_buffer: &Arc<tokio::sync::Mutex<SessionBuffer>>,
    board: &JobBoard,
    chat_id: i64,
) {
    let jobs = registry.stop_everyone().await;
    let cards = board.clear_all().await;
    let buffers = session_buffer.lock().await.clear_all();
    tg.send_message(
        chat_id,
        &format!(
            "🧹 Flushed all.\n• Cancelled jobs: {}\n• Board entries cleared: {}\n• Pending text buffers cleared: {}",
            jobs, cards, buffers
        ),
        None,
    )
    .await
    .ok();
}

/// Số nút Stop tối đa render trên 1 board (chống vượt giới hạn inline keyboard
/// của Telegram). Danh sách text vẫn liệt kê đủ; nút chỉ cho N process đầu.
const BOARD_STOP_BTN_CAP: usize = 25;

/// Render board thành text. `scope_all` = true: admin xem toàn hệ thống;
/// false: chỉ tiến trình của viewer.
fn render_board_text(entries: &[(u64, JobStatus)], scope_all: bool, lang: Lang) -> String {
    use crate::bot::board::JobState;
    let running = entries
        .iter()
        .filter(|(_, s)| s.state == JobState::Running)
        .count();
    let queued = entries.len() - running;
    let mut out = i18n::board_header(lang, running, queued, scope_all, &now_hms());
    if entries.is_empty() {
        out.push_str(&i18n::board_empty(lang, scope_all));
        return out;
    }
    for (i, (_, s)) in entries.iter().enumerate() {
        let age = crate::bot::board::fmt_age(s.since.elapsed().as_secs());
        let step = if s.step.is_empty() {
            "—".to_string()
        } else {
            crate::bot::board::truncate_chars(&s.step, 32)
        };
        // Admin thấy thêm chủ sở hữu; user không cần (đều của mình).
        let who = if scope_all {
            match s.username.as_deref() {
                Some(u) if !u.is_empty() => format!(" @{}", u),
                _ => format!(" id{}", s.user_id),
            }
        } else {
            String::new()
        };
        out.push_str(&format!(
            "\n{}. {} {}{}  ⏱{} · {}",
            i + 1,
            s.state.icon(),
            s.email_masked,
            who,
            age,
            step,
        ));
    }
    out
}

/// Keyboard board: 1 nút Stop / process (cap) + nút Refresh.
fn board_keyboard(entries: &[(u64, JobStatus)], lang: Lang) -> Value {
    let mut rows: Vec<Value> = Vec::new();
    for (job_id, s) in entries.iter().take(BOARD_STOP_BTN_CAP) {
        rows.push(serde_json::json!([{
            "text": i18n::btn_board_stop(lang, &s.email_masked),
            "callback_data": format!("bstop:{}", job_id),
        }]));
    }
    rows.push(serde_json::json!([{
        "text": i18n::btn_board_refresh(lang),
        "callback_data": "board:refresh",
    }]));
    Value::Array(rows)
}

/// Lấy snapshot (admin = tất cả, user = riêng) rồi build (text, keyboard).
/// Filter theo `viewer` khi không phải admin → KHÔNG lộ tiến trình người khác.
async fn build_board_view(
    board: &JobBoard,
    viewer: i64,
    is_admin: bool,
    lang: Lang,
) -> (String, Value) {
    let entries = if is_admin {
        board.snapshot_entries().await
    } else {
        board.snapshot_entries_for_user(viewer).await
    };
    let text = render_board_text(&entries, is_admin, lang);
    let kb = board_keyboard(&entries, lang);
    (text, kb)
}

/// `/board` — bảng tiến trình kèm nút Stop. Admin xem toàn hệ thống; user
/// thường CHỈ thấy tiến trình của chính mình (bảo mật). Snapshot tĩnh + nút
/// 🔄 Refresh để cập nhật.
async fn handle_board(
    tg: &Arc<TelegramClient>,
    board: &JobBoard,
    chat_id: i64,
    viewer: i64,
    is_admin: bool,
    lang: Lang,
) {
    let (text, kb) = build_board_view(board, viewer, is_admin, lang).await;
    tg.send_message_kb(chat_id, &text, None, kb).await.ok();
}

fn build_cookie_header(session_json: &Value) -> String {
    let mut pairs: Vec<String> = Vec::new();
    let cookies_v = session_json
        .get("__cookies")
        .or_else(|| session_json.get("cookies"));
    if let Some(arr) = cookies_v.and_then(|v| v.as_array()) {
        for c in arr {
            let Some(name) = c.get("name").and_then(|v| v.as_str()) else { continue };
            let Some(value) = c.get("value").and_then(|v| v.as_str()) else { continue };
            let domain = c
                .get("domain")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim_start_matches('.')
                .to_lowercase();
            if !domain.is_empty() && !domain.contains("chatgpt.com") && !domain.contains("openai.com") {
                continue;
            }
            pairs.push(format!("{}={}", name, value));
        }
    } else if let Some(obj) = cookies_v.and_then(|v| v.as_object()) {
        for (k, v) in obj {
            if let Some(s) = v.as_str() {
                pairs.push(format!("{}={}", k, s));
            }
        }
    }
    pairs.join("; ")
}

fn format_expires(ts_opt: Option<i64>) -> String {
    let Some(ts) = ts_opt else {
        return "—".into();
    };
    let Some(utc) = chrono::DateTime::<chrono::Utc>::from_timestamp(ts, 0) else {
        return ts.to_string();
    };
    let vn_offset = chrono::FixedOffset::east_opt(7 * 3600).unwrap();
    let vn = utc.with_timezone(&vn_offset);
    let now = chrono::Utc::now();
    let delta = utc - now;
    let suffix = if delta.num_seconds() < 0 {
        format!(" · expired {}m ago", -delta.num_minutes())
    } else if delta.num_minutes() < 60 {
        format!(" · in {}m", delta.num_minutes().max(0))
    } else {
        format!(
            " · in {}h{:02}m",
            delta.num_hours(),
            (delta.num_minutes() % 60).abs()
        )
    };
    format!("{} (UTC+7){}", vn.format("%d/%m/%Y %H:%M"), suffix)
}

async fn run_stripe_probe(client: Arc<HttpClient>, cache_dir: PathBuf) -> Result<()> {
    let cache = stripe::bundles::BundleCache::new(cache_dir);
    println!("→ fetch_bundles_live ...");
    let started = std::time::Instant::now();
    let cfg = stripe::bundles::extract_config_live(&client, &cache).await?;
    let elapsed = started.elapsed().as_secs_f64();
    println!(
        "✓ extract_config_live OK ({:.2}s)\n  shift = {}\n  rv_ts = {}\n  rv    = {} (len={})\n  sv    = {} (len={})\n  bundle_hash = {}…",
        elapsed,
        cfg.shift,
        cfg.rv_ts,
        &cfg.rv[..cfg.rv.len().min(16)],
        cfg.rv.len(),
        &cfg.sv[..cfg.sv.len().min(16)],
        cfg.sv.len(),
        &cfg.bundle_hash[..16],
    );
    // Test compute với dummy ppage_id
    let js = stripe_token::compute_js_checksum("test_ppage_id_abc", cfg.shift);
    let rv_ts = stripe_token::compute_rv_timestamp(&cfg);
    println!(
        "  js_checksum(test_ppage_id_abc) = {}\n  rv_timestamp                  = {}",
        js, rv_ts
    );
    Ok(())
}

async fn run_once(
    client: Arc<HttpClient>,
    cli: &Cli,
    proxy_pool: &[String],
    session_json: Option<&std::path::Path>,
    combo: &str,
    qr_out: &PathBuf,
) -> Result<()> {
    // Build (email, auth) từ combo email|pass|2fa HOẶC session.json.
    let (email, auth) = if !combo.trim().is_empty() {
        let parts: Vec<&str> = combo.split('|').collect();
        if parts.len() < 3 {
            return Err(anyhow!("combo phải là email|password|totp_secret"));
        }
        let (e, p, s) = (parts[0].trim(), parts[1].trim(), parts[2].trim());
        if e.is_empty() || p.is_empty() || s.is_empty() {
            return Err(anyhow!("combo có field rỗng"));
        }
        (
            e.to_string(),
            AuthSource::Login {
                password: p.to_string(),
                totp_secret: s.to_string(),
            },
        )
    } else {
        let path = session_json.ok_or_else(|| anyhow!("cần --session-json hoặc --combo"))?;
        let raw = std::fs::read(path).context("failed to read session.json")?;
        let v: Value = serde_json::from_slice(&raw).context("session.json is not valid JSON")?;
        let access_token = v
            .get("accessToken")
            .and_then(|x| x.as_str())
            .ok_or_else(|| anyhow!("session.json missing accessToken"))?
            .to_string();
        let email = v
            .get("user")
            .and_then(|u| u.get("email"))
            .and_then(|e| e.as_str())
            .unwrap_or("unknown@unknown")
            .to_string();
        let cookie_header = build_cookie_header(&v);
        (
            email,
            AuthSource::Session {
                access_token,
                cookie_header,
            },
        )
    };

    let job = UpiJobConfig {
        email,
        auth,
        proxy_pool: proxy_pool.to_vec(),
        login_proxy: None,
        approve_retries: cli.approve_retries,
        restart_threshold: cli.restart_threshold,
        max_restarts: cli.max_restarts,
        proxy_from_step: cli.proxy_from_step,
        qr_out_path: qr_out.clone(),
        bundles_cache_dir: cli.bundles_cache_dir.clone(),
        qr_watermark: cli.qr_watermark.clone(),
    };
    let log: crate::upi::runner::LogFn = Arc::new(|line: &str| {
        println!("{}", line);
    });
    let result = crate::upi::runner::run_upi_qr(client, job, log).await;
    println!("\n=== RESULT ===");
    println!("{}", serde_json::to_string_pretty(&result)?);
    Ok(())
}

/// Test login HTTP từ combo `email|password|2fa` → in kết quả (không in token).
async fn run_login_once(combo: &str, proxy: &str) -> Result<()> {
    let parts: Vec<&str> = combo.split('|').collect();
    if parts.len() < 3 {
        return Err(anyhow!("combo phải là email|password|totp_secret"));
    }
    let email = parts[0].trim();
    let password = parts[1].trim();
    let secret = parts[2].trim();
    if email.is_empty() || password.is_empty() || secret.is_empty() {
        return Err(anyhow!("combo có field rỗng"));
    }
    let proxy_opt = if proxy.trim().is_empty() {
        None
    } else {
        Some(proxy.trim())
    };

    let log: crate::upi::runner::LogFn = Arc::new(|line: &str| println!("{}", line));
    match crate::auth::login_pure_request(email, password, secret, proxy_opt, &log).await {
        Ok(sess) => {
            println!("\n=== LOGIN OK ===");
            println!(
                "accessToken_len={} cookie_count={} cookie_header_len={}",
                sess.access_token.len(),
                sess.cookie_count,
                sess.cookie_header.len()
            );
            Ok(())
        }
        Err(e) => {
            println!("\n=== LOGIN FAIL ===\n{}", e);
            Err(anyhow!("login failed"))
        }
    }
}


/// Welcome message with inline keyboard (localized).
async fn send_welcome(tg: &Arc<TelegramClient>, chat_id: i64, reply_to: i64, lang: Lang) {
    let info = i18n::welcome(lang);
    let kb = serde_json::json!([
        [
            {"text": i18n::btn_status(lang), "callback_data": "cmd:status"},
            {"text": i18n::btn_stop_all(lang), "callback_data": "cmd:stop"}
        ],
        [
            {"text": i18n::btn_settings(lang), "callback_data": "cmd:settings"},
            {"text": i18n::btn_help(lang), "callback_data": "cmd:help"}
        ],
        [
            {"text": "💬 Contact", "url": "https://t.me/prr9293"},
            {"text": "👥 Join Group", "url": "https://t.me/+QOsyt6bh5341YWM9"}
        ]
    ]);
    if let Err(e) = tg.send_message_kb(chat_id, &info, Some(reply_to), kb).await {
        tracing::warn!("send_welcome fail: {}", e);
        tg.send_message(chat_id, &info, Some(reply_to)).await.ok();
    }
}

/// Inline button click handler. Maps `cmd:<action>` → bot action for clicker.
async fn handle_callback(
    tg: Arc<TelegramClient>,
    registry: JobRegistry,
    session_buffer: Arc<tokio::sync::Mutex<SessionBuffer>>,
    store: Arc<settings::Settings>,
    cb: CallbackQuery,
    allowed: &HashSet<i64>,
    admin_chat_id: i64,
    board: JobBoard,
) -> Result<()> {
    let user_id = cb.from.id;
    let is_admin = admin_chat_id != 0 && user_id == admin_chat_id;
    let lang = lang_or_default(&store, user_id);

    if !is_admin {
        if let Ok(true) = store.is_banned(user_id) {
            tg.answer_callback_query(&cb.id, Some(&i18n::toast_blocked(lang)))
                .await
                .ok();
            return Ok(());
        }
    }

    if !allowed.is_empty() && !allowed.contains(&user_id) {
        tg.answer_callback_query(&cb.id, Some(&i18n::toast_not_whitelisted(lang)))
            .await
            .ok();
        return Ok(());
    }

    let chat_id = cb.message.as_ref().map(|m| m.chat.id).unwrap_or(0);
    let data = cb.data.clone().unwrap_or_default();

    // Chọn ngôn ngữ (có thể xảy ra trước khi user có lang).
    if let Some(code) = data.strip_prefix("setlang:") {
        if let Some(l) = Lang::from_code(code) {
            store.set_user_lang(user_id, l.code()).ok();
            tg.answer_callback_query(&cb.id, Some(&i18n::language_set(l))).await.ok();
            // Đặt menu lệnh localized cho riêng chat này (đầy đủ + admin nếu có).
            let cmds = localized_commands(l, is_admin);
            tg.set_my_commands_for_chat(chat_id, &cmds).await.ok();
            send_welcome(&tg, chat_id, 0, l).await;
        } else {
            tg.answer_callback_query(&cb.id, None).await.ok();
        }
        return Ok(());
    }

    // Dừng đúng 1 tiến trình theo job_id.
    if let Some(id_str) = data.strip_prefix("stopjob:") {
        if let Ok(job_id) = id_str.parse::<u64>() {
            let ok = registry.stop_job(user_id, job_id).await;
            let toast = if ok { i18n::stopped_this(lang) } else { i18n::stop_not_found(lang) };
            tg.answer_callback_query(&cb.id, Some(&toast)).await.ok();
        } else {
            tg.answer_callback_query(&cb.id, None).await.ok();
        }
        return Ok(());
    }

    // Board: làm mới snapshot (admin = toàn hệ thống; user = riêng).
    if data == "board:refresh" {
        let message_id = cb.message.as_ref().map(|m| m.message_id).unwrap_or(0);
        if message_id != 0 {
            let (text, kb) = build_board_view(&board, user_id, is_admin, lang).await;
            tg.edit_message_text_kb(chat_id, message_id, &text, kb).await.ok();
        }
        tg.answer_callback_query(&cb.id, None).await.ok();
        return Ok(());
    }

    // Board: nút Stop 1 process. Admin dừng được job bất kỳ; user thường CHỈ
    // dừng job của mình — `stop_job` verify ownership theo user_id nên user
    // KHÔNG thể dừng job người khác kể cả khi forge job_id (bảo mật).
    if let Some(id_str) = data.strip_prefix("bstop:") {
        if let Ok(job_id) = id_str.parse::<u64>() {
            let ok = if is_admin {
                registry.stop_job_any(job_id).await
            } else {
                registry.stop_job(user_id, job_id).await
            };
            tg.answer_callback_query(&cb.id, Some(&i18n::board_stopped_toast(lang, ok)))
                .await
                .ok();
            let message_id = cb.message.as_ref().map(|m| m.message_id).unwrap_or(0);
            if message_id != 0 {
                let (text, kb) = build_board_view(&board, user_id, is_admin, lang).await;
                tg.edit_message_text_kb(chat_id, message_id, &text, kb).await.ok();
            }
        } else {
            tg.answer_callback_query(&cb.id, None).await.ok();
        }
        return Ok(());
    }

    match data.as_str() {
        "cmd:status" => {
            tg.answer_callback_query(&cb.id, None).await.ok();
            tg.send_message(chat_id, &i18n::status_online(lang), None).await.ok();
        }
        "cmd:stop" => {
            let n = registry.stop_user(user_id).await;
            session_buffer.lock().await.clear(chat_id);
            let body = i18n::stopped_all(lang, n);
            tg.answer_callback_query(&cb.id, Some(&body)).await.ok();
            tg.send_message(chat_id, &body, None).await.ok();
        }
        "cmd:settings" => {
            tg.answer_callback_query(&cb.id, None).await.ok();
            tg.send_message_kb(chat_id, &i18n::settings_title(lang), None, settings_keyboard(lang))
                .await
                .ok();
        }
        "cmd:language" => {
            tg.answer_callback_query(&cb.id, None).await.ok();
            tg.send_message_kb(chat_id, i18n::choose_language(), None, language_keyboard())
                .await
                .ok();
        }
        "cmd:cancel" => {
            session_buffer.lock().await.clear(chat_id);
            tg.answer_callback_query(&cb.id, None).await.ok();
        }
        "cmd:help" => {
            tg.answer_callback_query(&cb.id, None).await.ok();
            send_help(&tg, &store, chat_id, 0, user_id, is_admin, lang).await;
        }
        "proxy:check" => {
            // Probe live status proxy của chính user — không cho user khác
            // probe proxy của ai khác.
            tg.answer_callback_query(&cb.id, Some(&i18n::toast_probing(lang))).await.ok();
            let raw = match store.get_user_proxy(user_id) {
                Ok(Some(r)) => r,
                Ok(None) => {
                    tg.send_message(chat_id, &i18n::proxy_not_set(lang), None).await.ok();
                    return Ok(());
                }
                Err(e) => {
                    tg.send_message(chat_id, &i18n::db_error(lang, &e.to_string()), None).await.ok();
                    return Ok(());
                }
            };
            let result = bot::proxy_status::PROXY_STATUS.refresh(&raw).await;
            let body = render_probe_result(lang, &raw, &result);
            tg.send_message_kb(chat_id, &body, None, proxy_keyboard(lang))
                .await
                .ok();
        }
        "proxy:remove" => {
            match store.remove_user_proxy(user_id) {
                Ok(true) => {
                    tg.answer_callback_query(&cb.id, Some(&i18n::toast_removed(lang))).await.ok();
                    tg.send_message(chat_id, &i18n::proxy_removed_direct(lang), None)
                        .await
                        .ok();
                }
                Ok(false) => {
                    tg.answer_callback_query(&cb.id, Some(&i18n::toast_nothing_to_remove(lang))).await.ok();
                    tg.send_message(chat_id, &i18n::proxy_not_set(lang), None).await.ok();
                }
                Err(e) => {
                    tg.answer_callback_query(&cb.id, Some(&i18n::toast_db_error(lang))).await.ok();
                    tg.send_message(chat_id, &i18n::proxy_remove_failed(lang, &e.to_string()), None).await.ok();
                }
            }
        }
        _ => {
            tg.answer_callback_query(&cb.id, Some(&i18n::toast_unknown_action(lang)))
                .await
                .ok();
        }
    }
    Ok(())
}

// ── Admin command helpers ──────────────────────────────────────────────

/// Tách phần nội dung sau token lệnh. Trả `(body, body_start_byte)` với `body`
/// đã trim khoảng trắng 2 đầu. None nếu lệnh không có nội dung.
/// `body_start_byte` = vị trí byte trong `text` nơi body bắt đầu (trước trim
/// phải) — dùng để tính offset entity cần dịch.
fn command_body(text: &str) -> Option<(&str, usize)> {
    let ws = text.char_indices().find(|(_, c)| c.is_whitespace())?.0;
    let rest = &text[ws..];
    let body_rel = rest.char_indices().find(|(_, c)| !c.is_whitespace())?.0;
    let start = ws + body_rel;
    let body = text[start..].trim_end();
    if body.is_empty() {
        return None;
    }
    Some((body, start))
}

/// Dịch entities của message admin về body sau khi cắt prefix lệnh. Offset/length
/// theo UTF-16 code unit (chuẩn Telegram). Entity nằm hẳn trong prefix bị bỏ;
/// entity vắt qua ranh giới bị clip cho khớp body.
fn shift_entities(entities: &[Value], prefix_utf16: usize, body_utf16: usize) -> Vec<Value> {
    let p = prefix_utf16 as i64;
    let blen = body_utf16 as i64;
    let mut out = Vec::new();
    for e in entities {
        let off = e.get("offset").and_then(|v| v.as_i64()).unwrap_or(0);
        let len = e.get("length").and_then(|v| v.as_i64()).unwrap_or(0);
        let nstart = (off - p).max(0);
        let nend = (off + len - p).min(blen);
        if nend <= nstart {
            continue;
        }
        let mut ne = e.clone();
        if let Some(obj) = ne.as_object_mut() {
            obj.insert("offset".into(), serde_json::json!(nstart));
            obj.insert("length".into(), serde_json::json!(nend - nstart));
        }
        out.push(ne);
    }
    out
}

/// `/notify <nội dung>` — broadcast tới mọi user (trừ user bị ban), giữ nguyên
/// xuống dòng + format chữ admin đã gõ. Prune user đã block bot.
async fn handle_notify(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    msg: &Message,
    text: &str,
) {
    let Some((body, body_start)) = command_body(text) else {
        tg.send_message(
            msg.chat.id,
            "Usage: /notify <message>\n(Supports line breaks + text formatting.)",
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    };

    let prefix_utf16 = text[..body_start].encode_utf16().count();
    let body_utf16 = body.encode_utf16().count();
    let entities: Vec<Value> = msg
        .entities
        .as_ref()
        .map(|es| shift_entities(es, prefix_utf16, body_utf16))
        .unwrap_or_default();

    let targets = match store.broadcast_targets() {
        Ok(t) => t,
        Err(e) => {
            tg.send_message(
                msg.chat.id,
                &format!("❌ Could not read user list: {}", e),
                Some(msg.message_id),
            )
            .await
            .ok();
            return;
        }
    };

    let total = targets.len();
    let status_id = tg
        .send_message(
            msg.chat.id,
            &format!("📢 Broadcasting to {} users...", total),
            Some(msg.message_id),
        )
        .await
        .unwrap_or(0);

    let mut ok = 0usize;
    let mut fail = 0usize;
    let mut pruned = 0usize;
    for (uid, chat_id) in targets {
        let ents = if entities.is_empty() {
            None
        } else {
            Some(entities.clone())
        };
        match tg.send_message_entities(chat_id, body, ents).await {
            Ok(_) => ok += 1,
            Err(e) => {
                fail += 1;
                let s = e.to_string().to_lowercase();
                if s.contains("bot was blocked")
                    || s.contains("user is deactivated")
                    || s.contains("chat not found")
                {
                    if let Err(e2) = store.remove_user(uid) {
                        tracing::warn!(uid, "prune user fail: {}", e2);
                    } else {
                        pruned += 1;
                    }
                }
            }
        }
        // Tôn trọng giới hạn ~30 msg/s của Telegram broadcast.
        tokio::time::sleep(Duration::from_millis(40)).await;
    }

    let summary = format!(
        "✅ Broadcast done\nSent OK: {}\nFailed: {}\nPruned users who blocked the bot: {}",
        ok, fail, pruned
    );
    if status_id != 0 {
        tg.edit_message_text(msg.chat.id, status_id, &summary)
            .await
            .ok();
    } else {
        tg.send_message(msg.chat.id, &summary, None).await.ok();
    }
}

/// `/chat <@username | id> <message>` — gửi DM trực tiếp tới 1 user. Resolve
/// target (số = user_id, còn lại = username qua bot_users), giữ nguyên format
/// (entities) admin gõ giống `/notify`. Report kết quả về admin.
async fn handle_chat(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    msg: &Message,
    text: &str,
) {
    let Some((arg, arg_start)) = command_body(text) else {
        tg.send_message(
            msg.chat.id,
            "Usage: /chat <@username | id> <message>\nE.g.: /chat @vipproor hello there  ·  /chat 2314324 your QR is ready",
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    };

    // Token đầu = target; phần còn lại = nội dung tin nhắn.
    let token = arg.split_whitespace().next().unwrap_or("");
    if token.is_empty() {
        tg.send_message(
            msg.chat.id,
            "Usage: /chat <@username | id> <message>",
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    }

    // Byte offset của nội dung tin nhắn trong `text` gốc (sau target + whitespace)
    // — để shift entities chính xác (UTF-16). target bắt đầu tại arg_start.
    let after_target = &arg[token.len()..];
    let Some((body, body_rel)) = after_target
        .char_indices()
        .find(|(_, c)| !c.is_whitespace())
        .map(|(i, _)| (after_target[i..].trim_end(), i))
    else {
        tg.send_message(
            msg.chat.id,
            "❌ Empty message. Usage: /chat <@username | id> <message>",
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    };
    if body.is_empty() {
        tg.send_message(
            msg.chat.id,
            "❌ Empty message. Usage: /chat <@username | id> <message>",
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    }
    let msg_start = arg_start + token.len() + body_rel;

    // Resolve target → user_id. Số (có/không '@') = user_id; còn lại = username.
    let stripped = token.trim_start_matches('@');
    let (target_id, uname): (i64, Option<String>) = match stripped.parse::<i64>() {
        Ok(id) => (id, store.known_username(id).ok().flatten()),
        Err(_) => match store.resolve_username(stripped) {
            Ok(Some(id)) => (id, Some(stripped.to_string())),
            Ok(None) => {
                tg.send_message(
                    msg.chat.id,
                    &format!(
                        "❌ Haven't seen @{} connect to the bot — cannot resolve user_id.\n\
                         Use the numeric id if you know it: /chat <id> <message>",
                        stripped
                    ),
                    Some(msg.message_id),
                )
                .await
                .ok();
                return;
            }
            Err(e) => {
                tg.send_message(
                    msg.chat.id,
                    &format!("❌ Username resolve error: {}", e),
                    Some(msg.message_id),
                )
                .await
                .ok();
                return;
            }
        },
    };

    // chat_id để gửi: ưu tiên chat_id đã lưu, fallback user_id (private chat).
    let target_chat = store
        .chat_id_for_user(target_id)
        .ok()
        .flatten()
        .unwrap_or(target_id);

    // Shift entities (giữ format admin gõ) về body sau khi cắt "/chat <target> ".
    let prefix_utf16 = text[..msg_start].encode_utf16().count();
    let body_utf16 = body.encode_utf16().count();
    let entities: Option<Vec<Value>> = msg
        .entities
        .as_ref()
        .map(|es| shift_entities(es, prefix_utf16, body_utf16))
        .filter(|v| !v.is_empty());

    match tg.send_message_entities(target_chat, body, entities).await {
        Ok(_) => {
            let uname_disp = uname
                .as_deref()
                .map(|u| format!(" (@{})", u))
                .unwrap_or_default();
            tg.send_message(
                msg.chat.id,
                &format!("✅ Message sent to user_id {}{}.", target_id, uname_disp),
                Some(msg.message_id),
            )
            .await
            .ok();
        }
        Err(e) => {
            let s = e.to_string().to_lowercase();
            let hint = if s.contains("bot was blocked") {
                " (user has blocked the bot)"
            } else if s.contains("chat not found") {
                " (user has never started the bot)"
            } else if s.contains("user is deactivated") {
                " (account deactivated)"
            } else {
                ""
            };
            tg.send_message(
                msg.chat.id,
                &format!("❌ Send failed{}: {}", hint, e),
                Some(msg.message_id),
            )
            .await
            .ok();
        }
    }
}

/// `/ban <@username | id> [lý do]` — resolve về user_id rồi lưu ban theo user_id
/// (đổi username vẫn dính ban). Cũng stop mọi job đang chạy của user đó.
async fn handle_ban(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    registry: &JobRegistry,
    msg: &Message,
    text: &str,
    admin_id: i64,
) {
    let Some((arg, _)) = command_body(text) else {
        tg.send_message(
            msg.chat.id,
            "Usage: /ban <@username | id> [reason]\nE.g.: /ban @vipproor  ·  /ban 2314324",
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    };
    let token = arg.split_whitespace().next().unwrap_or("");
    let reason = arg[token.len()..].trim();
    let reason_opt = if reason.is_empty() { None } else { Some(reason) };

    // Token toàn chữ số (có/không '@') → coi là user_id; còn lại → username.
    let stripped = token.trim_start_matches('@');
    let (target_id, uname): (i64, Option<String>) = match stripped.parse::<i64>() {
        Ok(id) => (id, store.known_username(id).ok().flatten()),
        Err(_) => match store.resolve_username(stripped) {
            Ok(Some(id)) => (id, Some(stripped.to_string())),
            Ok(None) => {
                tg.send_message(
                    msg.chat.id,
                    &format!(
                        "❌ Haven't seen @{} connect to the bot — cannot resolve user_id.\n\
                         Ban by numeric id if you know it: /ban <id> [reason]",
                        stripped
                    ),
                    Some(msg.message_id),
                )
                .await
                .ok();
                return;
            }
            Err(e) => {
                tg.send_message(
                    msg.chat.id,
                    &format!("❌ Username resolve error: {}", e),
                    Some(msg.message_id),
                )
                .await
                .ok();
                return;
            }
        },
    };

    if target_id == admin_id {
        tg.send_message(msg.chat.id, "⚠️ Cannot ban the admin.", Some(msg.message_id))
            .await
            .ok();
        return;
    }

    match store.ban(target_id, uname.as_deref(), reason_opt, admin_id) {
        Ok(_) => {
            let stopped = registry.stop_user(target_id).await;
            let uname_disp = uname
                .as_deref()
                .map(|u| format!(" (@{})", u))
                .unwrap_or_default();
            let reason_disp = reason_opt
                .map(|r| format!("\nReason: {}", r))
                .unwrap_or_default();
            tg.send_message(
                msg.chat.id,
                &format!(
                    "🚫 Banned user_id {}{}{}\nStopped {} running job(s) of the user.",
                    target_id, uname_disp, reason_disp, stopped
                ),
                Some(msg.message_id),
            )
            .await
            .ok();
        }
        Err(e) => {
            tg.send_message(
                msg.chat.id,
                &format!("❌ Ban failed: {}", e),
                Some(msg.message_id),
            )
            .await
            .ok();
        }
    }
}

/// `/unban <@username | id>` — gỡ ban. Resolve username qua bot_users, fallback
/// sang username lưu trong chính bảng ban (phòng user đã rời/đổi tên).
async fn handle_unban(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    msg: &Message,
    text: &str,
) {
    let Some((arg, _)) = command_body(text) else {
        tg.send_message(
            msg.chat.id,
            "Usage: /unban <@username | id>",
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    };
    let token = arg.split_whitespace().next().unwrap_or("");
    let stripped = token.trim_start_matches('@');

    let target_id: Option<i64> = match stripped.parse::<i64>() {
        Ok(id) => Some(id),
        Err(_) => match store.resolve_username(stripped) {
            Ok(Some(id)) => Some(id),
            Ok(None) => store.banned_user_id_by_username(stripped).ok().flatten(),
            Err(_) => store.banned_user_id_by_username(stripped).ok().flatten(),
        },
    };

    let Some(target_id) = target_id else {
        tg.send_message(
            msg.chat.id,
            &format!("❌ Could not find user_id for '{}'.", token),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    };

    match store.unban(target_id) {
        Ok(true) => {
            tg.send_message(
                msg.chat.id,
                &format!("✅ Unbanned user_id {}.", target_id),
                Some(msg.message_id),
            )
            .await
            .ok();
        }
        Ok(false) => {
            tg.send_message(
                msg.chat.id,
                &format!("ℹ️ user_id {} is not in the ban list.", target_id),
                Some(msg.message_id),
            )
            .await
            .ok();
        }
        Err(e) => {
            tg.send_message(
                msg.chat.id,
                &format!("❌ Unban failed: {}", e),
                Some(msg.message_id),
            )
            .await
            .ok();
        }
    }
}

/// `/banlist` — liệt kê user bị ban (mới nhất trước).
async fn handle_banlist(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    msg: &Message,
) {
    let bans = match store.list_bans() {
        Ok(b) => b,
        Err(e) => {
            tg.send_message(
                msg.chat.id,
                &format!("❌ Could not read ban list: {}", e),
                Some(msg.message_id),
            )
            .await
            .ok();
            return;
        }
    };
    if bans.is_empty() {
        tg.send_message(msg.chat.id, "✅ No users are banned.", Some(msg.message_id))
            .await
            .ok();
        return;
    }
    let mut body = format!("🚫 Banned users ({}):\n", bans.len());
    for b in &bans {
        let uname = b
            .username
            .as_deref()
            .map(|u| format!(" @{}", u))
            .unwrap_or_default();
        let reason = b
            .reason
            .as_deref()
            .map(|r| format!(" — {}", r))
            .unwrap_or_default();
        body.push_str(&format!("• {}{}{}\n", b.user_id, uname, reason));
    }
    if body.len() > 3800 {
        body.truncate(3800);
        body.push('…');
    }
    tg.send_message(msg.chat.id, &body, Some(msg.message_id))
        .await
        .ok();
}

// ── Proxy command helpers ─────────────────────────────────────────────

/// Inline keyboard cho thao tác proxy (check live + remove). Dùng chung cho
/// `/proxy_set` (sau khi save) và callback `proxy:check` (sau khi probe xong).
fn proxy_keyboard(lang: Lang) -> Value {
    serde_json::json!([
        [
            {"text": i18n::btn_proxy_check(lang), "callback_data": "proxy:check"},
            {"text": i18n::btn_proxy_remove(lang), "callback_data": "proxy:remove"},
        ]
    ])
}

/// Render probe result thành text Telegram (song ngữ). Mask credential mọi chỗ.
fn render_probe_result(lang: Lang, raw_line: &str, r: &bot::proxy_probe::ProbeResult) -> String {
    let status = proxy_status_text(r);
    let detail_value = if r.ok {
        r.detail.clone()
    } else {
        // Sanitize detail trước khi hiển thị để không leak creds materialized.
        proxy_format::sanitize_proxy_text(&r.detail)
    };
    let detail_line = i18n::proxy_probe_detail(lang, r.ok, &detail_value);
    i18n::proxy_probe_card(
        lang,
        r.ok,
        status,
        &proxy_format::mask_proxy(raw_line),
        r.latency_ms,
        &detail_line,
        bot::proxy_probe::PROBE_ENDPOINT,
    )
}

/// Map ProbeResult → nhãn trạng thái ngắn (dùng chung probe card + pre-flight).
fn proxy_status_text(r: &bot::proxy_probe::ProbeResult) -> &'static str {
    use bot::proxy_probe::ProbeReason;
    match (&r.reason, r.ok) {
        (ProbeReason::Ok, true) => "ALIVE",
        (ProbeReason::Auth, _) => "AUTH FAIL",
        (ProbeReason::Ip, _) => "IP-LEVEL FAIL",
        (ProbeReason::BadFormat, _) => "BAD FORMAT",
        _ => "UNKNOWN",
    }
}

/// 1 dòng trạng thái proxy cho pre-flight card (mask + sanitize detail).
fn build_proxy_line(lang: Lang, label: &str, r: &bot::proxy_probe::ProbeResult) -> String {
    let detail = if r.ok {
        r.detail.clone()
    } else {
        proxy_format::sanitize_proxy_text(&r.detail)
    };
    bot::proc_view::render_proxy_line(lang, label, r.ok, proxy_status_text(r), &detail, r.latency_ms)
}

/// `/proxy_set <line>` — set proxy private cho user. Không có arg → show
/// trạng thái + keyboard nếu user đã có proxy, hoặc usage nếu chưa.
///
/// Format hỗ trợ (đồng bộ Python `materialize_proxy`):
///   - host:port
///   - host:port:user
///   - host:port:user:pass
///   - scheme://user:pass@host:port
///   - placeholder `{SID}` / `{sid}` cho sticky session
async fn handle_proxy_set(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    msg: &Message,
    text: &str,
    user_id: i64,
    username: &Option<String>,
    admin_chat_id: i64,
    proxy_from_step: u32,
    lang: Lang,
) {
    let body_opt = command_body(text);
    if body_opt.is_none() {
        // No arg → show trạng thái hiện tại + keyboard nếu đã có proxy
        match store.get_user_proxy(user_id) {
            Ok(Some(raw)) => {
                let body = i18n::proxy_show_current(lang, &proxy_format::mask_proxy(&raw));
                tg.send_message_kb(msg.chat.id, &body, Some(msg.message_id), proxy_keyboard(lang))
                    .await
                    .ok();
            }
            Ok(None) => {
                tg.send_message(msg.chat.id, &i18n::proxy_set_usage(lang), Some(msg.message_id))
                    .await
                    .ok();
            }
            Err(e) => {
                tg.send_message(
                    msg.chat.id,
                    &i18n::db_error(lang, &e.to_string()),
                    Some(msg.message_id),
                )
                .await
                .ok();
            }
        }
        return;
    }

    let (arg, _) = body_opt.unwrap();
    // Lấy token đầu tiên — không cho whitespace lọt vào DB.
    let raw_line = arg.split_whitespace().next().unwrap_or("");
    if raw_line.is_empty() {
        tg.send_message(msg.chat.id, &i18n::proxy_empty_line(lang), Some(msg.message_id))
            .await
            .ok();
        return;
    }

    // Validate format trước khi save — fail-fast (không lưu rác vào DB).
    if let Err(e) = proxy_format::validate_and_mask(raw_line) {
        tg.send_message(
            msg.chat.id,
            &i18n::proxy_invalid_format(lang, &e.to_string()),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    }

    if let Err(e) = store.set_user_proxy(user_id, raw_line) {
        tg.send_message(
            msg.chat.id,
            &i18n::proxy_save_failed(lang, &e.to_string()),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    }

    let masked = proxy_format::mask_proxy(raw_line);
    let body = i18n::proxy_set_ok(lang, &masked, proxy_from_step);
    tg.send_message_kb(msg.chat.id, &body, Some(msg.message_id), proxy_keyboard(lang))
        .await
        .ok();

    // Notify admin — full info (không mask) để admin verify proxy đáng ngờ.
    // Skip nếu admin tự set hoặc admin disabled.
    if admin_chat_id != 0 && user_id != admin_chat_id {
        let summary = format!(
            "🌐 User set proxy\nFrom: @{} (id {})\nRaw: {}\nMasked: {}",
            username.as_deref().unwrap_or("-"),
            user_id,
            raw_line,
            masked,
        );
        if let Err(e) = tg.send_message(admin_chat_id, &summary, None).await {
            tracing::warn!(admin_chat_id, "admin notify proxy_set fail: {}", e);
        }
    }
}

/// `/proxy_remove` — xóa proxy của user. Job tiếp theo sẽ dùng pool global
/// (hoặc DIRECT nếu pool global rỗng).
async fn handle_proxy_remove(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    msg: &Message,
    user_id: i64,
    lang: Lang,
) {
    match store.remove_user_proxy(user_id) {
        Ok(true) => {
            tg.send_message(
                msg.chat.id,
                &i18n::proxy_removed_global(lang),
                Some(msg.message_id),
            )
            .await
            .ok();
        }
        Ok(false) => {
            tg.send_message(
                msg.chat.id,
                &i18n::proxy_none_to_remove(lang),
                Some(msg.message_id),
            )
            .await
            .ok();
        }
        Err(e) => {
            tg.send_message(
                msg.chat.id,
                &i18n::proxy_remove_failed(lang, &e.to_string()),
                Some(msg.message_id),
            )
            .await
            .ok();
        }
    }
}

/// `/proxy_login_set <line>` — ADMIN set login proxy global. Áp cho mọi step
/// TRƯỚC `proxy_from_step` (gồm login HTTP) của TẤT CẢ user. Validate + probe
/// tươi (bypass cache) để admin verify ngay.
async fn handle_proxy_login_set(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    msg: &Message,
    text: &str,
    proxy_from_step: u32,
) {
    let body_opt = command_body(text);
    if body_opt.is_none() {
        // Không arg → show login proxy hiện tại.
        match store.get_login_proxy() {
            Some(raw) => {
                tg.send_message(
                    msg.chat.id,
                    &format!(
                        "🌐 Login proxy hiện tại:\n{}\n\nÁp cho step 1..{} (login). Đổi: /proxy_login_set <line> · Xóa: /proxy_login_remove",
                        proxy_format::mask_proxy(&raw),
                        proxy_from_step.saturating_sub(1).max(1),
                    ),
                    Some(msg.message_id),
                )
                .await
                .ok();
            }
            None => {
                tg.send_message(
                    msg.chat.id,
                    "ℹ️ Chưa set login proxy.\n\nUsage:\n\
                     /proxy_login_set host:port\n\
                     /proxy_login_set host:port:user:pass\n\
                     /proxy_login_set http://user:pass@host:port\n\
                     /proxy_login_set socks5://user:pass@host:1080\n\n\
                     Hỗ trợ {SID} cho sticky session.",
                    Some(msg.message_id),
                )
                .await
                .ok();
            }
        }
        return;
    }

    let (arg, _) = body_opt.unwrap();
    let raw_line = arg.split_whitespace().next().unwrap_or("");
    if raw_line.is_empty() {
        tg.send_message(msg.chat.id, "❌ Empty proxy line.", Some(msg.message_id))
            .await
            .ok();
        return;
    }

    if let Err(e) = proxy_format::validate_and_mask(raw_line) {
        tg.send_message(
            msg.chat.id,
            &format!("❌ Invalid proxy format: {}", e),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    }

    if let Err(e) = store.set_login_proxy(raw_line) {
        tg.send_message(
            msg.chat.id,
            &format!("❌ Save failed: {}", e),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    }

    // Probe tươi (bypass cache) để verify ngay sau khi set.
    let result = bot::proxy_status::PROXY_STATUS.refresh(raw_line).await;
    let admin_lang = lang_or_default(store, msg.from.as_ref().map(|u| u.id).unwrap_or(0));
    let probe = render_probe_result(admin_lang, raw_line, &result);
    let body = format!(
        "✅ Login proxy đã set (áp cho step 1..{} của mọi user).\n{}\n\n{}",
        proxy_from_step.saturating_sub(1).max(1),
        proxy_format::mask_proxy(raw_line),
        probe,
    );
    tg.send_message(msg.chat.id, &body, Some(msg.message_id))
        .await
        .ok();
}

/// `/proxy_login_remove` — ADMIN xóa login proxy global. Sau đó segment login
/// chạy DIRECT (hoặc pool global env nếu user không có proxy riêng).
async fn handle_proxy_login_remove(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    msg: &Message,
) {
    match store.remove_login_proxy() {
        Ok(true) => {
            tg.send_message(
                msg.chat.id,
                "🧹 Đã xóa login proxy. Segment login giờ chạy DIRECT (hoặc pool global env).",
                Some(msg.message_id),
            )
            .await
            .ok();
        }
        Ok(false) => {
            tg.send_message(
                msg.chat.id,
                "ℹ️ Chưa có login proxy để xóa.",
                Some(msg.message_id),
            )
            .await
            .ok();
        }
        Err(e) => {
            tg.send_message(
                msg.chat.id,
                &format!("❌ Remove failed: {}", e),
                Some(msg.message_id),
            )
            .await
            .ok();
        }
    }
}

/// `/help` — show full command list. Section admin chỉ hiện khi user là admin.
/// Cũng đính kèm trạng thái proxy hiện tại của user (mask).
async fn send_help(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    chat_id: i64,
    reply_to: i64,
    user_id: i64,
    is_admin: bool,
    lang: Lang,
) {
    let vi = matches!(lang, Lang::Vi);
    let mut help = if vi {
        String::from(
            "📚 Danh sách lệnh\n\n\
             Người dùng:\n\
             /start — mở menu / hướng dẫn\n\
             /status — trạng thái bot + hàng chờ\n\
             /stop — dừng TẤT CẢ tiến trình của bạn\n\
             /board — bảng tiến trình của bạn + nút Dừng\n\
             /cancel — xóa bộ đệm văn bản đang chờ\n\
             /proxy_set <line> — đặt proxy riêng của bạn\n\
             /proxy_remove — xóa proxy của bạn\n\
             /settings — cài đặt\n\
             /language — đổi ngôn ngữ\n\
             /help — bảng này\n",
        )
    } else {
        String::from(
            "📚 Commands\n\n\
             User:\n\
             /start — open menu / instructions\n\
             /status — bot status + queue\n\
             /stop — stop ALL your processes\n\
             /board — your process board + Stop buttons\n\
             /cancel — clear pending text buffer\n\
             /proxy_set <line> — set your own proxy\n\
             /proxy_remove — remove your proxy\n\
             /settings — settings\n\
             /language — change language\n\
             /help — this message\n",
        )
    };
    if is_admin {
        help.push_str(if vi {
            "\nAdmin:\n\
             /notify <nội dung> — gửi thông báo tới mọi user (giữ format)\n\
             /chat <@user | id> <nội dung> — nhắn riêng 1 user\n\
             /ban <@user | id> [lý do] — cấm theo user_id\n\
             /unban <@user | id> — gỡ cấm\n\
             /banlist — danh sách user bị cấm\n\
             /stopall — dừng TẤT CẢ tiến trình của mọi user\n\
             /flushall — xóa sạch hàng chờ + board + buffer\n\
             /proxy_login_set <line> — đặt login proxy chung (áp segment login mọi user)\n\
             /proxy_login_remove — xóa login proxy chung\n"
        } else {
            "\nAdmin:\n\
             /notify <message> — broadcast to all users (keeps formatting)\n\
             /chat <@user | id> <message> — direct message one user\n\
             /ban <@user | id> [reason] — ban by user_id\n\
             /unban <@user | id> — unban\n\
             /banlist — list banned users\n\
             /stopall — stop ALL processes of all users\n\
             /flushall — clear queue + board + buffers\n\
             /proxy_login_set <line> — set shared login proxy (login segment for all users)\n\
             /proxy_login_remove — remove shared login proxy\n"
        });
    }
    help.push_str(if vi {
        "\nĐịnh dạng proxy hỗ trợ:\n\
         • host:port\n\
         • host:port:user:pass\n\
         • scheme://user:pass@host:port (http, https, socks5)\n\
         • {SID} cho sticky session\n"
    } else {
        "\nSupported proxy formats:\n\
         • host:port\n\
         • host:port:user:pass\n\
         • scheme://user:pass@host:port (http, https, socks5)\n\
         • {SID} placeholder for sticky sessions\n"
    });

    match store.get_user_proxy(user_id) {
        Ok(Some(raw)) => {
            help.push_str(&format!(
                "\n🌐 {}: {}\n",
                if vi { "Proxy của bạn" } else { "Your proxy" },
                proxy_format::mask_proxy(&raw)
            ));
        }
        Ok(None) => {
            help.push_str(if vi {
                "\n🌐 Proxy: chưa đặt (DIRECT hoặc dùng pool chung của admin)\n"
            } else {
                "\n🌐 Proxy: not set (DIRECT or admin's global pool)\n"
            });
        }
        Err(_) => {}
    }

    help.push_str(if vi {
        "\nGửi file session.json (lấy từ chatgpt.com/api/auth/session) hoặc combo `email|password|2fa` để bắt đầu."
    } else {
        "\nSend a session.json file (from chatgpt.com/api/auth/session) or combo `email|password|2fa` to start."
    });

    let reply = if reply_to == 0 { None } else { Some(reply_to) };
    tg.send_message(chat_id, &help, reply).await.ok();
}
