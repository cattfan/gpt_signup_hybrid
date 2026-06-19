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
mod random_profile;
mod settings;
mod stripe;
mod stripe_token;
mod upi;
mod user_agent;

use anyhow::{anyhow, Context, Result};
use clap::Parser;
use serde_json::Value;
use std::collections::HashSet;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::mpsc;
use tracing::{error, info, warn};

use crate::bot::limiter::{AdmitDecision, MessageDecision, UserLimiter};
use crate::bot::queue::{spawn_workers, Job, JobEvent, JobQueue, SubmitError, WorkerConfig};
use crate::bot::registry::JobRegistry;
use crate::bot::session_buffer::{AppendResult, SessionBuffer};
use crate::bot::telegram::{CallbackQuery, Message, TelegramClient};
use crate::http::HttpClient;
use crate::upi::runner::UpiJobConfig;
use crate::upi::types::UpiQrResult;

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
    #[arg(long, env = "MAX_CONCURRENT", default_value = "5", global = true)]
    max_concurrent: usize,

    /// Hard cap số job pending trong queue. Khi đầy, job mới bị reject. Bảo vệ RAM.
    #[arg(long, env = "QUEUE_CAPACITY", default_value = "50", global = true)]
    queue_capacity: usize,

    /// Số lần retry approve (per job).
    #[arg(long, env = "APPROVE_RETRIES", default_value = "200", global = true)]
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
    RunOnce {
        /// Path tới session.json
        #[arg(long)]
        session_json: PathBuf,
        /// Path xuất QR PNG (nếu có).
        #[arg(long, default_value = "/tmp/upi-runonce.png")]
        qr_out: PathBuf,
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

    std::fs::create_dir_all(&cli.qr_out_dir).ok();
    std::fs::create_dir_all(&cli.bundles_cache_dir).ok();
    if let Some(p) = cli.db_path.parent() {
        std::fs::create_dir_all(p).ok();
    }

    // Open Settings Store — giữ sống suốt runtime: chứa settings + danh sách
    // user kết nối (broadcast) + danh sách ban (theo user_id).
    let store = Arc::new(
        settings::Settings::open(&cli.db_path).context("không mở được SQLite settings store")?,
    );
    info!("settings store opened: {}", cli.db_path.display());

    let client = HttpClient::new(cli.http_timeout)?;

    // Sub-commands
    if let Some(cmd) = cli.cmd.clone() {
        match cmd {
            SubCmd::StripeProbe => return run_stripe_probe(client, cli.bundles_cache_dir.clone()).await,
            SubCmd::RunOnce { session_json, qr_out } => {
                return run_once(client, &cli, &proxy_pool, &session_json, &qr_out).await;
            }
        }
    }

    if cli.telegram_token.is_empty() {
        return Err(anyhow!(
            "--telegram-token bắt buộc khi chạy bot mode (hoặc dùng sub-command stripe-probe / run-once)"
        ));
    }
    let tg = Arc::new(TelegramClient::new(&cli.telegram_token)?);

    let (queue, queue_rx) = JobQueue::new(cli.queue_capacity);
    let queue = Arc::new(queue);
    let limiter = UserLimiter::new(
        Duration::from_secs(cli.user_cooldown_seconds),
        cli.user_msg_rate_per_min,
    );
    let session_buffer = Arc::new(tokio::sync::Mutex::new(SessionBuffer::new(
        cli.session_buffer_ttl_seconds,
    )));
    let registry = JobRegistry::new();
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
        ("start", "Open menu"),
        ("status", "Bot status"),
        ("stop", "Cancel my running jobs"),
        ("cancel", "Clear pending text buffer"),
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
            ("help", "Show help"),
            ("notify", "Broadcast tới mọi user (admin)"),
            ("ban", "Ban user theo @username/id (admin)"),
            ("unban", "Gỡ ban theo @username/id (admin)"),
            ("banlist", "Danh sách user bị ban (admin)"),
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
                        tokio::spawn(async move {
                            if let Err(e) = handle_callback(
                                tg,
                                registry,
                                session_buffer,
                                store,
                                cb,
                                &allowed,
                                admin_chat_id,
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
                    "⛔ Bạn đã bị admin chặn khỏi bot.",
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

    if !allowed.is_empty() && !allowed.contains(&user_id) {
        tg.send_message(
            msg.chat.id,
            "⛔ Tài khoản chưa được whitelist. Liên hệ admin.",
            Some(msg.message_id),
        )
        .await
        .ok();
        return Ok(());
    }

    // Commands
    if let Some(text) = &msg.text {
        let trimmed = text.trim();
        if trimmed.starts_with("/start") || trimmed.starts_with("/help") {
            // Clear buffer khi /start để không gộp nhầm session cũ
            session_buffer.lock().await.clear(msg.chat.id);
            send_welcome(&tg, msg.chat.id, msg.message_id).await;
            return Ok(());
        }
        if trimmed.starts_with("/status") {
            let pending = queue.pending();
            tg.send_message(
                msg.chat.id,
                &format!("✅ Bot online · queue {}/{}", pending, cli.queue_capacity),
                None,
            )
            .await
            .ok();
            return Ok(());
        }
        if trimmed.starts_with("/cancel") {
            session_buffer.lock().await.clear(msg.chat.id);
            tg.send_message(msg.chat.id, "🧹 Cleared pending text buffer.", None)
                .await
                .ok();
            return Ok(());
        }
        if trimmed.starts_with("/stop") {
            let stopped = registry.stop_user(user_id).await;
            session_buffer.lock().await.clear(msg.chat.id);
            let body = if stopped == 0 {
                "ℹ️ No running jobs to stop.".to_string()
            } else {
                format!("🛑 Stopped {} job(s) of yours.", stopped)
            };
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
        match cmd_base {
            "/notify" | "/ban" | "/unban" | "/banlist" => {
                if !is_admin {
                    tg.send_message(
                        msg.chat.id,
                        "⛔ Lệnh chỉ dành cho admin.",
                        Some(msg.message_id),
                    )
                    .await
                    .ok();
                    return Ok(());
                }
                match cmd_base {
                    "/notify" => handle_notify(&tg, &store, &msg, text).await,
                    "/ban" => {
                        handle_ban(&tg, &store, &registry, &msg, text, cli.admin_chat_id).await
                    }
                    "/unban" => handle_unban(&tg, &store, &msg, text).await,
                    "/banlist" => handle_banlist(&tg, &store, &msg).await,
                    _ => unreachable!(),
                }
                return Ok(());
            }
            _ => {}
        }

        if trimmed.starts_with('/') {
            return Ok(());
        }

        // Text thường — append vào session buffer
        if !trimmed.is_empty() {
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
                        msg.chat.id,
                        msg.message_id,
                        user_id,
                        username,
                        raw,
                        proxy_pool,
                        cli,
                    )
                    .await;
                }
                AppendResult::Pending => {
                    // Im lặng — đợi chunk tiếp. Nếu user paste 1 lần bị split
                    // bởi Telegram → các message kế tiếp sẽ ghép lại trong TTL.
                    return Ok(());
                }
                AppendResult::Invalid(reason) => {
                    tg.send_message(
                        msg.chat.id,
                        &format!(
                            "❌ Text is not a valid JSON object: {}\n\nSend session JSON again (file or paste).",
                            reason
                        ),
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
            "📄 Send session.json file or paste JSON text.",
            Some(msg.message_id),
        )
        .await
        .ok();
        return Ok(());
    };

    let file_name = doc.file_name.unwrap_or_else(|| "session.json".into());
    if !file_name.to_lowercase().ends_with(".json") {
        tg.send_message(msg.chat.id, "❌ File must be `.json`.", Some(msg.message_id))
            .await
            .ok();
        return Ok(());
    }
    if doc.file_size.unwrap_or(0) > 1_500_000 {
        tg.send_message(
            msg.chat.id,
            "❌ File too large (>1.5MB). A valid session.json is usually < 100KB.",
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
        msg.chat.id,
        msg.message_id,
        user_id,
        username,
        raw,
        proxy_pool,
        cli,
    )
    .await
}

#[allow(clippy::too_many_arguments)]
async fn process_session_json(
    tg: Arc<TelegramClient>,
    queue: Arc<JobQueue>,
    limiter: UserLimiter,
    registry: JobRegistry,
    chat_id: i64,
    reply_to: i64,
    user_id: i64,
    username: Option<String>,
    raw: String,
    proxy_pool: &[String],
    cli: &Cli,
) -> Result<()> {
    // Anti-spam: check submit decision
    match limiter.check_submit(user_id).await {
        AdmitDecision::Allow => {}
        AdmitDecision::JobInFlight => {
            tg.send_message(
                chat_id,
                "⚠️ You already have a running job. Wait until it finishes before sending again.",
                Some(reply_to),
            )
            .await
            .ok();
            return Ok(());
        }
        AdmitDecision::Cooldown { remaining_secs } => {
            tg.send_message(
                chat_id,
                &format!(
                    "⏱ Cooldown active — wait {}s before retrying.",
                    remaining_secs
                ),
                Some(reply_to),
            )
            .await
            .ok();
            return Ok(());
        }
    }

    let session_json: Value = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(e) => {
            tg.send_message(
                chat_id,
                &format!("❌ Invalid session JSON: {}", e),
                Some(reply_to),
            )
            .await
            .ok();
            return Ok(());
        }
    };

    let access_token = match session_json.get("accessToken").and_then(|v| v.as_str()) {
        Some(t) if !t.is_empty() => t.to_string(),
        _ => {
            tg.send_message(
                chat_id,
                "❌ session JSON missing or empty `accessToken`.",
                Some(reply_to),
            )
            .await
            .ok();
            return Ok(());
        }
    };
    let email = session_json
        .get("user")
        .and_then(|u| u.get("email"))
        .and_then(|e| e.as_str())
        .unwrap_or("unknown@unknown")
        .to_string();
    let cookie_header = build_cookie_header(&session_json);

    let job_id = uuid::Uuid::new_v4().simple().to_string();
    let qr_path = cli.qr_out_dir.join(format!("qr_{}_{}.png", user_id, &job_id[..8]));
    let job_config = UpiJobConfig {
        email: email.clone(),
        access_token,
        cookie_header,
        proxy_pool: proxy_pool.to_vec(),
        approve_retries: cli.approve_retries,
        restart_threshold: cli.restart_threshold,
        max_restarts: cli.max_restarts,
        proxy_from_step: cli.proxy_from_step,
        qr_out_path: qr_path.clone(),
        bundles_cache_dir: cli.bundles_cache_dir.clone(),
        qr_watermark: cli.qr_watermark.clone(),
    };

    let status_msg_id = tg
        .send_message(
            chat_id,
            &format!(
                "🚀 Job received\nEmail: {}\nUser: {}\nQueueing...",
                upi::runner::mask_email(&email),
                username.as_deref().unwrap_or("-")
            ),
            Some(reply_to),
        )
        .await?;

    let (event_tx, mut event_rx) = mpsc::unbounded_channel::<JobEvent>();
    let (job_id, cancel_token) = registry.register(user_id).await;
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
            limiter.mark_in_flight(user_id).await;
            tg.edit_message_text(
                chat_id,
                status_msg_id,
                &format!("⏳ Queued (position ≈{})", position),
            )
            .await
            .ok();
        }
        Err(SubmitError::QueueFull { pending, capacity }) => {
            // Job không vào queue được — release token + entry registry.
            registry.unregister(user_id, job_id).await;
            tg.edit_message_text(
                chat_id,
                status_msg_id,
                &format!(
                    "🚫 Queue full ({}/{}). Bot is busy, please retry in a few minutes.",
                    pending, capacity
                ),
            )
            .await
            .ok();
            return Ok(());
        }
        Err(SubmitError::Closed) => {
            registry.unregister(user_id, job_id).await;
            tg.edit_message_text(chat_id, status_msg_id, "🚫 Queue closed.")
                .await
                .ok();
            return Ok(());
        }
    }

    let tg_for_log = tg.clone();
    let qr_path_for_send = qr_path.clone();
    let admin_chat_id = cli.admin_chat_id;
    tokio::spawn(async move {
        let mut last_edit = std::time::Instant::now()
            .checked_sub(Duration::from_secs(60))
            .unwrap_or_else(std::time::Instant::now);
        let mut log_buffer: Vec<String> = Vec::new();
        let mut started_at = std::time::Instant::now();
        let mut tick = tokio::time::interval(Duration::from_secs(5));
        tick.tick().await; // first tick fires immediately — consume it.

        let render_progress =
            |started: std::time::Instant, buf: &Vec<String>| -> String {
                let body = format!(
                    "▶️ Processing ({:.0}s)\n\n{}",
                    started.elapsed().as_secs_f64(),
                    buf.join("\n")
                );
                if body.len() > 3800 {
                    format!("{}…", &body[..3800])
                } else {
                    body
                }
            };

        loop {
            tokio::select! {
                event = event_rx.recv() => {
                    let Some(event) = event else { break; };
                    match event {
                        JobEvent::Queued { position } => {
                            tg_for_log
                                .edit_message_text(
                                    chat_id,
                                    status_msg_id,
                                    &format!("⏳ Queued (position ≈{})", position),
                                )
                                .await
                                .ok();
                        }
                        JobEvent::Started => {
                            started_at = std::time::Instant::now();
                            tg_for_log
                                .edit_message_text(
                                    chat_id,
                                    status_msg_id,
                                    "▶️ Starting UPI flow...",
                                )
                                .await
                                .ok();
                            last_edit = std::time::Instant::now();
                        }
                        JobEvent::Log(line) => {
                            log_buffer.push(line);
                            if log_buffer.len() > 24 {
                                let drop_n = log_buffer.len() - 24;
                                log_buffer.drain(0..drop_n);
                            }
                            if last_edit.elapsed() > Duration::from_millis(2500) {
                                let body = render_progress(started_at, &log_buffer);
                                tg_for_log
                                    .edit_message_text(chat_id, status_msg_id, &body)
                                    .await
                                    .ok();
                                last_edit = std::time::Instant::now();
                            }
                        }
                        JobEvent::Done(result) => {
                            let body = render_done_message(&result);
                            tg_for_log
                                .edit_message_text(chat_id, status_msg_id, &body)
                                .await
                                .ok();
                            if let Some(qr) = result.qr_path.as_deref() {
                                let path = std::path::Path::new(qr);
                                if path.exists() {
                                    let caption = format!(
                                        "✅ UPI QR ready\nEmail: {}\nExpires: {}",
                                        result.email,
                                        format_expires(result.qr_expires_at),
                                    );
                                    if let Err(e) = tg_for_log
                                        .send_photo(chat_id, path, Some(&caption), None)
                                        .await
                                    {
                                        tg_for_log
                                            .send_message(
                                                chat_id,
                                                &format!("⚠️ sendPhoto fail: {}", e),
                                                None,
                                            )
                                            .await
                                            .ok();
                                    }
                                }
                            }
                            // Notify admin chat khi user KHÁC tạo QR thành công.
                            // Skip nếu admin tự tạo, hoặc admin_chat_id = 0 (disabled).
                            if result.ok
                                && admin_chat_id != 0
                                && user_id != admin_chat_id
                            {
                                let summary = format!(
                                    "🆕 QR success\nFrom: @{} (id {})\nEmail: {}\nElapsed: {:.1}s",
                                    username_for_log.as_deref().unwrap_or("-"),
                                    user_id,
                                    result.email,
                                    result.elapsed_seconds,
                                );
                                if let Err(e) = tg_for_log
                                    .send_message(admin_chat_id, &summary, None)
                                    .await
                                {
                                    tracing::warn!(
                                        admin_chat_id,
                                        "admin notify fail: {}",
                                        e
                                    );
                                }
                            }
                            crate::bot::queue::cleanup_qr_artifacts(&qr_path_for_send);
                            break;
                        }
                        JobEvent::Timeout => {
                            tg_for_log
                                .edit_message_text(
                                    chat_id,
                                    status_msg_id,
                                    "⏰ Job timeout — killed to free worker. You can retry.",
                                )
                                .await
                                .ok();
                            crate::bot::queue::cleanup_qr_artifacts(&qr_path_for_send);
                            break;
                        }
                        JobEvent::Cancelled => {
                            tg_for_log
                                .edit_message_text(
                                    chat_id,
                                    status_msg_id,
                                    "🛑 Job cancelled by /stop.",
                                )
                                .await
                                .ok();
                            crate::bot::queue::cleanup_qr_artifacts(&qr_path_for_send);
                            break;
                        }
                    }
                }
                _ = tick.tick() => {
                    // Periodic heartbeat — refresh elapsed counter dù không có log mới.
                    // Quan trọng khi 1 step (vd checkout) stall lâu trên mạng — user
                    // vẫn thấy "Processing (Xs)" tăng để biết bot còn sống.
                    if !log_buffer.is_empty() && last_edit.elapsed() > Duration::from_millis(2500) {
                        let body = render_progress(started_at, &log_buffer);
                        tg_for_log
                            .edit_message_text(chat_id, status_msg_id, &body)
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
    format!("{} (VN){}", vn.format("%d/%m/%Y %H:%M"), suffix)
}

fn render_done_message(r: &UpiQrResult) -> String {
    let icon = if r.ok { "✅" } else { "❌" };
    let status = if r.ok { "DONE" } else { "FAIL" };
    let mut s = format!(
        "{} {}\nEmail: {}\nElapsed: {:.1}s\nApproved: {}\nRestarts: {}\n",
        icon,
        status,
        r.email,
        r.elapsed_seconds,
        if r.qr_path.is_some() && r.ok {
            "yes"
        } else {
            "no"
        },
        r.restart_count,
    );
    if let Some(reason) = &r.qr_reason {
        s.push_str(&format!("Reason: {}\n", reason));
    }
    if let Some(err) = &r.error {
        s.push_str(&format!("Error: {}\n", err));
    }
    if !r.ok {
        s.push_str("\n💡 Failed — you can retry by sending session JSON again after cooldown.\n");
    }
    s
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
    session_json: &PathBuf,
    qr_out: &PathBuf,
) -> Result<()> {
    let raw = std::fs::read(session_json).context("đọc session.json fail")?;
    let v: Value = serde_json::from_slice(&raw).context("session.json không phải JSON hợp lệ")?;
    let access_token = v
        .get("accessToken")
        .and_then(|x| x.as_str())
        .ok_or_else(|| anyhow!("session.json thiếu accessToken"))?
        .to_string();
    let email = v
        .get("user")
        .and_then(|u| u.get("email"))
        .and_then(|e| e.as_str())
        .unwrap_or("unknown@unknown")
        .to_string();
    let cookie_header = build_cookie_header(&v);

    let job = UpiJobConfig {
        email,
        access_token,
        cookie_header,
        proxy_pool: proxy_pool.to_vec(),
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


/// Welcome message with inline keyboard.
async fn send_welcome(tg: &Arc<TelegramClient>, chat_id: i64, reply_to: i64) {
    let info = "👋 UPI QR Bot\n\n\
        Send your session.json file or paste the JSON text \
        (long text auto-merged from Telegram chunks).\n\n\
        Pick an action:";
    let kb = serde_json::json!([
        [
            {"text": "📊 Status", "callback_data": "cmd:status"},
            {"text": "🛑 Stop my jobs", "callback_data": "cmd:stop"}
        ],
        [
            {"text": "🧹 Clear buffer", "callback_data": "cmd:cancel"},
            {"text": "❓ Help", "callback_data": "cmd:help"}
        ]
    ]);
    if let Err(e) = tg
        .send_message_kb(chat_id, info, Some(reply_to), kb)
        .await
    {
        tracing::warn!("send_welcome fail: {}", e);
        // Fallback: plain text
        tg.send_message(chat_id, info, Some(reply_to)).await.ok();
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
) -> Result<()> {
    let user_id = cb.from.id;
    let is_admin = admin_chat_id != 0 && user_id == admin_chat_id;

    if !is_admin {
        if let Ok(true) = store.is_banned(user_id) {
            tg.answer_callback_query(&cb.id, Some("Bạn đã bị chặn"))
                .await
                .ok();
            return Ok(());
        }
    }

    if !allowed.is_empty() && !allowed.contains(&user_id) {
        tg.answer_callback_query(&cb.id, Some("Not whitelisted"))
            .await
            .ok();
        return Ok(());
    }

    let chat_id = cb.message.as_ref().map(|m| m.chat.id).unwrap_or(0);
    let data = cb.data.clone().unwrap_or_default();

    match data.as_str() {
        "cmd:status" => {
            tg.answer_callback_query(&cb.id, None).await.ok();
            tg.send_message(chat_id, "✅ Bot online.", None).await.ok();
        }
        "cmd:stop" => {
            let n = registry.stop_user(user_id).await;
            session_buffer.lock().await.clear(chat_id);
            let body = if n == 0 {
                "ℹ️ No running jobs to stop.".to_string()
            } else {
                format!("🛑 Stopped {} job(s) of yours.", n)
            };
            tg.answer_callback_query(&cb.id, Some(&body)).await.ok();
            tg.send_message(chat_id, &body, None).await.ok();
        }
        "cmd:cancel" => {
            session_buffer.lock().await.clear(chat_id);
            tg.answer_callback_query(&cb.id, Some("Buffer cleared"))
                .await
                .ok();
            tg.send_message(chat_id, "🧹 Cleared pending text buffer.", None)
                .await
                .ok();
        }
        "cmd:help" => {
            tg.answer_callback_query(&cb.id, None).await.ok();
            let mut help = String::from(
                "Commands:\n\
                /start — open menu\n\
                /status — bot status\n\
                /stop — cancel YOUR running jobs\n\
                /cancel — clear pending text buffer\n\
                /help — this message\n",
            );
            if is_admin {
                help.push_str(
                    "\nAdmin:\n\
                    /notify <nội dung> — broadcast tới mọi user (giữ xuống dòng + format)\n\
                    /ban <@username | id> [lý do] — ban theo user_id\n\
                    /unban <@username | id> — gỡ ban\n\
                    /banlist — danh sách user bị ban\n",
                );
            }
            help.push_str("\nSend a session.json file or paste JSON text to start.");
            tg.send_message(chat_id, &help, None).await.ok();
        }
        _ => {
            tg.answer_callback_query(&cb.id, Some("Unknown action"))
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
            "Usage: /notify <nội dung>\n(Hỗ trợ xuống dòng + format chữ.)",
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
                &format!("❌ Không đọc được danh sách user: {}", e),
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
            &format!("📢 Đang broadcast tới {} user...", total),
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
        "✅ Broadcast xong\nGửi OK: {}\nLỗi: {}\nGỡ user đã block bot: {}",
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
            "Usage: /ban <@username | id> [lý do]\nVD: /ban @vipproor  ·  /ban 2314324",
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
                        "❌ Chưa thấy @{} kết nối bot — không resolve được user_id.\n\
                         Ban bằng số id nếu bạn biết: /ban <id> [lý do]",
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
                    &format!("❌ Lỗi resolve username: {}", e),
                    Some(msg.message_id),
                )
                .await
                .ok();
                return;
            }
        },
    };

    if target_id == admin_id {
        tg.send_message(msg.chat.id, "⚠️ Không thể ban admin.", Some(msg.message_id))
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
                .map(|r| format!("\nLý do: {}", r))
                .unwrap_or_default();
            tg.send_message(
                msg.chat.id,
                &format!(
                    "🚫 Đã ban user_id {}{}{}\nĐã stop {} job đang chạy của user.",
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
                &format!("❌ Ban thất bại: {}", e),
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
            &format!("❌ Không tìm thấy user_id cho '{}'.", token),
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
                &format!("✅ Đã gỡ ban user_id {}.", target_id),
                Some(msg.message_id),
            )
            .await
            .ok();
        }
        Ok(false) => {
            tg.send_message(
                msg.chat.id,
                &format!("ℹ️ user_id {} không nằm trong ban list.", target_id),
                Some(msg.message_id),
            )
            .await
            .ok();
        }
        Err(e) => {
            tg.send_message(
                msg.chat.id,
                &format!("❌ Unban thất bại: {}", e),
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
                &format!("❌ Không đọc được ban list: {}", e),
                Some(msg.message_id),
            )
            .await
            .ok();
            return;
        }
    };
    if bans.is_empty() {
        tg.send_message(msg.chat.id, "✅ Không có user nào bị ban.", Some(msg.message_id))
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
