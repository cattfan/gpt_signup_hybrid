//! UPI flow orchestrator — port từ `web/upi_runner.py::run_upi_qr_probe`.
//!
//! Giữ nguyên 100% logic:
//!   - 4 confirm variants thử lần lượt
//!   - approve loop với `restart_threshold` + `max_restarts`
//!   - network outage detection + recovery polling
//!   - proxy advance per-batch
//!   - aggregate matches qua mọi response → tìm UPI URI / QR image URL.

use crate::http::HttpClient;
use crate::random_profile::random_india_profile;
use crate::stripe::bundles::{extract_config_live, BundleCache};
use crate::stripe_token::StripeTokenConfig;
use crate::upi::endpoints::{
    chatgpt_approve_checkout, create_chatgpt_checkout, extract_amount, stripe_confirm_upi_qr,
    stripe_elements_session, stripe_init, stripe_payment_page_refresh, ApproveAttempt,
    ConfirmAttempt, RefreshAttempt,
};
use crate::upi::matchers::{
    find_matches, find_qr_expires_at, find_qr_image_url, find_upi_uri, Match,
};
use crate::upi::qr::{download_qr_image, render_qr_png};
use crate::upi::types::{
    ApproveAttemptSummary, ConfirmAttemptSummary, RefreshAttemptSummary, UpiAuth, UpiQrResult,
};
use anyhow::Result;
use serde_json::Value;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

/// Hardcoded knobs — đồng bộ Python upi_runner.py.
pub const PROMO: bool = true;
pub const APPROVE_DELAY_MS: u64 = 3000;
pub const APPROVE_PROXY_BATCH: u32 = 3;
pub const APPROVE_BACKEND_EXCEPTION_CONSECUTIVE: u32 = 0; // disabled
pub const NETWORK_FAIL_DETECT: u32 = 3;
pub const NETWORK_RECOVERY_POLL_MS: u64 = 5000;
pub const NETWORK_RECOVERY_MAX_WAIT_S: u64 = 600;
pub const CONFIRM_VARIANTS: &[&str] = &["qr_code", "empty", "flow_qr", "intent"];

#[derive(Debug, Clone)]
pub struct UpiJobConfig {
    pub email: String,
    pub access_token: String,
    pub cookie_header: String,
    pub proxy_pool: Vec<String>,
    pub approve_retries: u32,
    pub restart_threshold: u32,
    pub max_restarts: u32,
    pub proxy_from_step: u32,
    pub qr_out_path: PathBuf,
    pub bundles_cache_dir: PathBuf,
    pub qr_watermark: String,
}

pub type LogFn = Arc<dyn Fn(&str) + Send + Sync>;

/// Mask email cho log (giữ 3 ký tự đầu + 2 cuối local part).
pub fn mask_email(email: &str) -> String {
    if let Some(at_idx) = email.find('@') {
        let local = &email[..at_idx];
        let domain = &email[at_idx + 1..];
        if local.len() <= 3 {
            return format!("{}***@{}", &local[..local.len().min(1)], domain);
        }
        let head = &local[..3];
        let tail = &local[local.len() - 2..];
        return format!("{}***{}@{}", head, tail, domain);
    }
    "***".into()
}

pub fn mask_proxy(proxy: &str) -> String {
    if proxy.is_empty() {
        return "direct".into();
    }
    if !proxy.contains('@') {
        return proxy.to_string();
    }
    if let Some(scheme_end) = proxy.find("://") {
        let scheme = &proxy[..scheme_end];
        let rest = &proxy[scheme_end + 3..];
        if let Some(at) = rest.rfind('@') {
            return format!("{}://***@{}", scheme, &rest[at + 1..]);
        }
    }
    "***".into()
}

fn proxy_for_step<'a>(
    proxy_pool: &'a [String],
    from_step: u32,
    step: u32,
) -> Option<&'a str> {
    if step >= from_step && !proxy_pool.is_empty() {
        Some(proxy_pool[0].as_str())
    } else {
        None
    }
}

fn proxy_for_retry<'a>(
    proxy_pool: &'a [String],
    from_step: u32,
    step: u32,
    attempt: u32,
    per_proxy: u32,
) -> Option<&'a str> {
    if step < from_step || proxy_pool.is_empty() {
        return None;
    }
    let idx = ((attempt.saturating_sub(1)) / per_proxy) as usize % proxy_pool.len();
    Some(proxy_pool[idx].as_str())
}

fn is_backend_exception(att: &ApproveAttempt) -> bool {
    att.http_status == Some(200) && att.result.as_deref() == Some("exception")
}

fn is_network_error(att: &ApproveAttempt) -> bool {
    att.http_status.is_none()
}

async fn probe_connectivity(client: &HttpClient) -> bool {
    matches!(client.head("https://chatgpt.com/", 5).await, Ok(_))
}

async fn wait_network_recovery(client: &HttpClient, log: &LogFn) -> bool {
    let started = Instant::now();
    let mut poll_idx: u32 = 0;
    loop {
        if started.elapsed() > Duration::from_secs(NETWORK_RECOVERY_MAX_WAIT_S) {
            log(&format!(
                "[net]  outage      FAIL not recovered in {}s",
                NETWORK_RECOVERY_MAX_WAIT_S
            ));
            return false;
        }
        if probe_connectivity(client).await {
            log(&format!(
                "[net]  recovered   OK   after {:.0}s ({} probes)",
                started.elapsed().as_secs_f64(),
                poll_idx + 1
            ));
            return true;
        }
        poll_idx += 1;
        if poll_idx == 1 || poll_idx % 6 == 0 {
            log(&format!(
                "[net]  waiting     ...  poll={} elapsed={:.0}s max={}s",
                poll_idx,
                started.elapsed().as_secs_f64(),
                NETWORK_RECOVERY_MAX_WAIT_S
            ));
        }
        tokio::time::sleep(Duration::from_millis(NETWORK_RECOVERY_POLL_MS)).await;
    }
}

fn confirm_to_summary(a: &ConfirmAttempt, phase: u32) -> ConfirmAttemptSummary {
    ConfirmAttemptSummary {
        variant: a.variant.clone(),
        phase,
        http_status: a.http_status,
        ok: a.ok,
        keys: a.keys.clone(),
        error: a.error.clone(),
    }
}

fn approve_to_summary(
    a: &ApproveAttempt,
    variant: Option<&str>,
    attempt: u32,
    phase: u32,
    proxy: &str,
) -> ApproveAttemptSummary {
    ApproveAttemptSummary {
        variant: variant.map(|s| s.to_string()),
        attempt,
        phase,
        proxy: proxy.to_string(),
        http_status: a.http_status,
        ok: a.ok,
        result: a.result.clone(),
        error_type: a.error_type.clone(),
        error: a.error.clone(),
        keys: a.keys.clone(),
    }
}

fn refresh_to_summary(a: &RefreshAttempt, attempt: u32, proxy: &str) -> RefreshAttemptSummary {
    RefreshAttemptSummary {
        attempt,
        proxy: proxy.to_string(),
        http_status: a.http_status,
        ok: a.ok,
        error_type: a.error_type.clone(),
        error: a.error_msg.clone(),
        keys: a.keys.clone(),
    }
}

pub async fn run_upi_qr(
    client: Arc<HttpClient>,
    cfg: UpiJobConfig,
    log: LogFn,
) -> UpiQrResult {
    let started = Instant::now();
    let masked_email = mask_email(&cfg.email);
    let auth = UpiAuth {
        email: cfg.email.clone(),
        access_token: cfg.access_token.clone(),
        cookie_header: cfg.cookie_header.clone(),
    };

    let restart_enabled = cfg.restart_threshold > 0 && cfg.max_restarts > 0;
    let proxy_advance_enabled = cfg.proxy_from_step <= 6
        && APPROVE_PROXY_BATCH > 1
        && cfg.proxy_pool.len() > 1;

    log(&format!("Account: {}", masked_email));
    log("[1/6] login   OK   session supplied");

    let stripe_js_id = uuid::Uuid::new_v4().to_string();
    let profile = random_india_profile();
    let bundle_cache = BundleCache::new(cfg.bundles_cache_dir.clone());

    let mut confirm_attempts: Vec<ConfirmAttemptSummary> = Vec::new();
    let mut approve_attempts: Vec<ApproveAttemptSummary> = Vec::new();
    let mut refresh_attempts: Vec<RefreshAttemptSummary> = Vec::new();

    let mut backend_exception_count: u32 = 0;
    let mut fatal_approve_error: Option<String> = None;
    let mut amount: i64 = 0;
    let mut return_url = String::new();
    let mut session_id = String::new();
    let mut publishable_key = String::new();
    let mut approved = false;
    let mut final_confirmed = false;
    let mut approve_index_total: u32 = 0;
    let mut proxy_virtual_attempt: u32 = 0;
    let mut restart_count: u32 = 0;
    let mut token_config: Option<StripeTokenConfig> = None;

    // Per-phase context — overwrite mỗi phase, dùng cho aggregate match.
    let mut checkout_value = Value::Null;
    let mut init_data = Value::Null;
    let mut elements_data = Value::Null;
    let mut last_confirm_data: Value = Value::Null;
    let mut last_refresh_data: Value = Value::Null;
    let mut all_confirm_data: Vec<(String, Value)> = Vec::new();
    let mut all_approve_data: Vec<(String, Value)> = Vec::new();
    let mut all_refresh_data: Vec<Value> = Vec::new();

    'phase_loop: loop {
        let phase_idx = restart_count + 1;
        let phase_tag = if restart_enabled {
            format!(" [p{}]", phase_idx)
        } else {
            String::new()
        };
        let mut triggered_restart = false;

        if restart_count > 0 {
            log(&format!(
                "[restart] phase {}/{}  approve_idx kept at {}/{}",
                phase_idx,
                cfg.max_restarts + 1,
                approve_index_total,
                cfg.approve_retries
            ));
        }

        // Step 2 — checkout (retry network errors)
        log(&format!(
            "[2/6{}] checkout   →    requesting...",
            phase_tag
        ));
        let mut checkout_result = None;
        for attempt in 1..=3u32 {
            match create_chatgpt_checkout(
                &client,
                &auth,
                proxy_for_step(&cfg.proxy_pool, cfg.proxy_from_step, 2),
            )
            .await
            {
                Ok(co) => {
                    checkout_result = Some(Ok(co));
                    break;
                }
                Err(e) => {
                    let msg = format!("{}", e);
                    if msg.contains("checkout HTTP") {
                        // Server reject (HTTP 4xx/5xx) — log nguyên message + fail-fast.
                        log(&format!(
                            "[2/6{}] checkout   FAIL {}",
                            phase_tag,
                            short_msg(&msg, 200)
                        ));
                        checkout_result = Some(Err(e));
                        break;
                    }
                    if attempt < 3 {
                        log(&format!(
                            "[2/6{}] checkout   WARN attempt {}/3: {} → retry in 2s",
                            phase_tag,
                            attempt,
                            short_msg(&msg, 160)
                        ));
                        tokio::time::sleep(Duration::from_secs(2)).await;
                        continue;
                    }
                    log(&format!(
                        "[2/6{}] checkout   FAIL all 3 attempts failed: {}",
                        phase_tag,
                        short_msg(&msg, 200)
                    ));
                    checkout_result = Some(Err(e));
                }
            }
        }
        match checkout_result.unwrap() {
            Ok(co) => {
                session_id = co.session_id.clone();
                return_url = format!("https://checkout.stripe.com/c/pay/{}", session_id);
                publishable_key = co.publishable_key;
                checkout_value = co.raw;
                log(&format!(
                    "[2/6{}] checkout   OK   cs={} ui={}",
                    phase_tag,
                    short(&session_id, 14),
                    co.checkout_ui_mode.unwrap_or_else(|| "-".into())
                ));
            }
            Err(e) => {
                let msg = format!("phase {} checkout fail: {}", phase_idx, e);
                if restart_count == 0 {
                    return finalize_error(masked_email, started, msg);
                }
                fatal_approve_error = Some(msg.clone());
                break 'phase_loop;
            }
        }

        // Step 3 — Stripe init
        log(&format!("[3/6{}] init       →    requesting...", phase_tag));
        match stripe_init(
            &client,
            &session_id,
            &publishable_key,
            &stripe_js_id,
            proxy_for_step(&cfg.proxy_pool, cfg.proxy_from_step, 3),
        )
        .await
        {
            Ok(d) => {
                amount = extract_amount(&d);
                let id = d.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string();
                init_data = d;
                log(&format!(
                    "[3/6{}] init       OK   amount={} ppage={}",
                    phase_tag,
                    amount,
                    short(&id, 12)
                ));
                if PROMO && amount > 0 {
                    log(&format!(
                        "[upi] no free offer   FAIL amount={} (promo enabled but > 0)",
                        amount
                    ));
                    if restart_count == 0 {
                        let mut r = UpiQrResult {
                            ok: false,
                            email: masked_email.clone(),
                            amount,
                            return_url: return_url.clone(),
                            checkout_session: short(&session_id, 18),
                            error: Some("no free offer (promo enabled but amount > 0)".into()),
                            elapsed_seconds: started.elapsed().as_secs_f64(),
                            ..Default::default()
                        };
                        r.ok = false;
                        return r;
                    }
                    fatal_approve_error =
                        Some(format!("phase {} no free offer (amount={})", phase_idx, amount));
                    break 'phase_loop;
                }
            }
            Err(e) => {
                let msg = format!("phase {} init fail: {}", phase_idx, e);
                if restart_count == 0 {
                    return finalize_error(masked_email, started, msg);
                }
                fatal_approve_error = Some(msg.clone());
                log(&format!("[3/6{}] init       FAIL {}", phase_tag, &msg));
                break 'phase_loop;
            }
        }

        // Step 4 — elements
        log(&format!("[4/6{}] elements   →    requesting...", phase_tag));
        match stripe_elements_session(
            &client,
            &session_id,
            &publishable_key,
            &stripe_js_id,
            amount,
            proxy_for_step(&cfg.proxy_pool, cfg.proxy_from_step, 4),
        )
        .await
        {
            Ok(d) => {
                let sid = d
                    .get("session_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                elements_data = d;
                log(&format!(
                    "[4/6{}] elements   OK   session={}",
                    phase_tag,
                    short(&sid, 14)
                ));
            }
            Err(e) => {
                let msg = format!("phase {} elements fail: {}", phase_idx, e);
                if restart_count == 0 {
                    return finalize_error(masked_email, started, msg);
                }
                fatal_approve_error = Some(msg.clone());
                log(&format!("[4/6{}] elements   FAIL {}", phase_tag, &msg));
                break 'phase_loop;
            }
        }

        // Step 5a — token config (chỉ phase 1)
        if restart_count == 0 {
            log("[5a]   token-cfg  →    fetching Stripe bundle...");
            match extract_config_live(&client, &bundle_cache).await {
                Ok(cfg2) => {
                    log(&format!(
                        "[5a]   token-cfg  OK   shift={} rv={}",
                        cfg2.shift,
                        short(&cfg2.rv, 8)
                    ));
                    token_config = Some(cfg2);
                }
                Err(e) => {
                    log(&format!("[5a]   token-cfg  WARN extract fail: {}", e));
                }
            }
        }

        // Step 5b — confirm variants
        let mut phase_confirmed = false;
        let mut confirm_variant_used: Option<String> = None;
        for variant in CONFIRM_VARIANTS {
            let proxy = proxy_for_step(&cfg.proxy_pool, cfg.proxy_from_step, 5);
            let attempt = match stripe_confirm_upi_qr(
                &client,
                &session_id,
                &publishable_key,
                &stripe_js_id,
                &init_data,
                &elements_data,
                &profile,
                &cfg.email,
                amount,
                variant,
                token_config.as_ref(),
                proxy,
            )
            .await
            {
                Ok(a) => a,
                Err(e) => {
            log(&format!(
                "[5b{}] confirm    FAIL network: {}",
                phase_tag, e
            ));
                    ConfirmAttempt {
                        variant: variant.to_string(),
                        http_status: None,
                        ok: false,
                        keys: vec![],
                        error: Some(Value::String(e.to_string())),
                        data: None,
                    }
                }
            };
            confirm_attempts.push(confirm_to_summary(&attempt, phase_idx));
            if let Some(ref d) = attempt.data {
                last_confirm_data = d.clone();
                all_confirm_data.push((variant.to_string(), d.clone()));
            }
            log(&format!(
                "[5b{}] confirm    {}    variant={} http={}",
                phase_tag,
                if attempt.ok { "OK  " } else { "FAIL" },
                variant,
                attempt
                    .http_status
                    .map(|s| s.to_string())
                    .unwrap_or_else(|| "—".into())
            ));
            if attempt.ok {
                phase_confirmed = true;
                confirm_variant_used = Some(variant.to_string());
                break;
            }
        }

        if !phase_confirmed {
            if restart_count == 0 {
                break 'phase_loop;
            }
            fatal_approve_error =
                Some(format!("phase {} confirm failed (all variants)", phase_idx));
            log(&format!(
                "[5b{}] confirm    FAIL all variants failed in restart phase",
                phase_tag
            ));
            break 'phase_loop;
        }
        final_confirmed = true;

        // Step 5c — page refresh trước approve
        match stripe_payment_page_refresh(
            &client,
            &session_id,
            &publishable_key,
            &stripe_js_id,
            &elements_data,
            proxy_for_step(&cfg.proxy_pool, cfg.proxy_from_step, 5),
        )
        .await
        {
            Ok(r) => {
                if let Some(ref d) = r.data {
                    last_refresh_data = d.clone();
                    all_refresh_data.push(d.clone());
                }
                log(&format!(
                    "[5c{}] refresh    {}    http={}",
                    phase_tag,
                    if r.ok { "OK  " } else { "FAIL" },
                    r.http_status
                        .map(|s| s.to_string())
                        .unwrap_or_else(|| "—".into())
                ));
                refresh_attempts.push(refresh_to_summary(&r, 1, "direct"));
            }
            Err(e) => {
                log(&format!("[5c{}] refresh    FAIL network: {}", phase_tag, e));
            }
        }

        // Step 6 — approve loop
        if restart_count == 0 {
            log(&format!(
                "[6/6] approve loop start  retries={} delay={:.1}s batch={}",
                cfg.approve_retries,
                APPROVE_DELAY_MS as f64 / 1000.0,
                APPROVE_PROXY_BATCH
            ));
        } else {
            log(&format!(
                "[6/6{}] approve resume  from {}/{}",
                phase_tag, approve_index_total, cfg.approve_retries
            ));
        }
        let mut consec_be: u32 = 0;
        let mut consec_net: u32 = 0;

        let approve_started = Instant::now();
        while approve_index_total < cfg.approve_retries {
            approve_index_total += 1;
            proxy_virtual_attempt += 1;
            let proxy_url = proxy_for_retry(
                &cfg.proxy_pool,
                cfg.proxy_from_step,
                6,
                proxy_virtual_attempt,
                APPROVE_PROXY_BATCH,
            );
            let attempt = match chatgpt_approve_checkout(&client, &auth, &session_id, proxy_url)
                .await
            {
                Ok(a) => a,
                Err(e) => ApproveAttempt {
                    http_status: None,
                    ok: false,
                    result: None,
                    keys: vec![],
                    error_type: Some("NetworkError".into()),
                    error: Some(format!("{}", e)),
                    data: None,
                },
            };
            let proxy_mask = proxy_url.map(mask_proxy).unwrap_or_else(|| "direct".into());
            approve_attempts.push(approve_to_summary(
                &attempt,
                confirm_variant_used.as_deref(),
                approve_index_total,
                phase_idx,
                &proxy_mask,
            ));
            if let Some(ref d) = attempt.data {
                let v = confirm_variant_used.clone().unwrap_or_default();
                all_approve_data.push((v, d.clone()));
            }
            log(&format!(
                "      try {:03}/{:03}  {}  http={:>3}  {:<10} proxy={}",
                approve_index_total,
                cfg.approve_retries,
                if attempt.ok { "OK  " } else { "FAIL" },
                attempt
                    .http_status
                    .map(|s| s.to_string())
                    .unwrap_or_else(|| "---".into()),
                attempt
                    .result
                    .clone()
                    .or_else(|| attempt.error_type.clone())
                    .unwrap_or_else(|| "—".into()),
                proxy_mask
            ));
            if attempt.ok {
                approved = true;
                break;
            }
            if is_network_error(&attempt) {
                consec_net += 1;
                if consec_net >= NETWORK_FAIL_DETECT {
                    log(&format!(
                        "[net]  outage      WARN {} timeouts → pause loop, polling connectivity",
                        consec_net
                    ));
                    if wait_network_recovery(&client, &log).await {
                        consec_net = 0;
                        continue;
                    }
                    fatal_approve_error = Some(format!(
                        "network outage not recovered in {}s (consec={})",
                        NETWORK_RECOVERY_MAX_WAIT_S, consec_net
                    ));
                    break;
                }
            } else if is_backend_exception(&attempt) {
                consec_net = 0;
                backend_exception_count += 1;
                consec_be += 1;
                if restart_enabled
                    && consec_be >= cfg.restart_threshold
                    && restart_count < cfg.max_restarts
                {
                    triggered_restart = true;
                    log(&format!(
                        "[6/6{}] approve     WARN consec exceptions {}/{} → restart ({}/{})",
                        phase_tag,
                        consec_be,
                        cfg.restart_threshold,
                        restart_count + 1,
                        cfg.max_restarts
                    ));
                    break;
                }
                if APPROVE_BACKEND_EXCEPTION_CONSECUTIVE > 0
                    && consec_be >= APPROVE_BACKEND_EXCEPTION_CONSECUTIVE
                {
                    fatal_approve_error = Some(format!(
                        "approve consec exception threshold ({}/{}) total={}",
                        consec_be, APPROVE_BACKEND_EXCEPTION_CONSECUTIVE, backend_exception_count
                    ));
                    log(&format!(
                        "[6/6] approve     FAIL consec exception {}/{}",
                        consec_be, APPROVE_BACKEND_EXCEPTION_CONSECUTIVE
                    ));
                    break;
                }
                if proxy_advance_enabled {
                    let current_batch = (proxy_virtual_attempt - 1) / APPROVE_PROXY_BATCH;
                    let pos_in_batch =
                        proxy_virtual_attempt - current_batch * APPROVE_PROXY_BATCH;
                    if pos_in_batch < APPROVE_PROXY_BATCH {
                        proxy_virtual_attempt = (current_batch + 1) * APPROVE_PROXY_BATCH;
                    }
                }
            } else {
                let http = attempt.http_status;
                let res = attempt.result.clone();
                if http == Some(200) && res.as_deref().map_or(false, |s| s != "exception") {
                    consec_net = 0;
                    if consec_be > 0 {
                        log(&format!(
                            "[6/6] approve     INFO reset consec exception ({} → 0) result={}",
                            consec_be,
                            res.as_deref().unwrap_or("—")
                        ));
                        consec_be = 0;
                    }
                } else {
                    consec_net = 0;
                }
            }
            if approve_index_total < cfg.approve_retries {
                tokio::time::sleep(Duration::from_millis(APPROVE_DELAY_MS)).await;
            }
        }
        let approve_elapsed = approve_started.elapsed().as_secs_f64();
        if approved {
            log(&format!(
                "[6/6] approve     OK   approved at {}/{} ({:.1}s, restarts={})",
                approve_index_total, cfg.approve_retries, approve_elapsed, restart_count
            ));
        }

        // Refresh post-approve (best-effort)
        if !triggered_restart && fatal_approve_error.is_none() && (approved || !approve_attempts.is_empty()) {
            if let Ok(r) = stripe_payment_page_refresh(
                &client,
                &session_id,
                &publishable_key,
                &stripe_js_id,
                &elements_data,
                proxy_for_step(&cfg.proxy_pool, cfg.proxy_from_step, 5),
            )
            .await
            {
                if let Some(ref d) = r.data {
                    last_refresh_data = d.clone();
                    all_refresh_data.push(d.clone());
                }
                log(&format!(
                    "[5c{}] refresh    {}    http={}",
                    phase_tag,
                    if r.ok { "OK  " } else { "FAIL" },
                    r.http_status
                        .map(|s| s.to_string())
                        .unwrap_or_else(|| "—".into())
                ));
                refresh_attempts.push(refresh_to_summary(&r, 2, "direct"));
            }
        }

        if approved || fatal_approve_error.is_some() {
            break 'phase_loop;
        }
        if approve_index_total >= cfg.approve_retries {
            log(&format!(
                "[6/6] approve     FAIL not approved after {} attempts ({:.1}s, restarts={})",
                cfg.approve_retries, approve_elapsed, restart_count
            ));
            break 'phase_loop;
        }
        if triggered_restart {
            restart_count += 1;
            continue 'phase_loop;
        }
        break 'phase_loop;
    }

    // Avoid unused_variable warnings on temp vars used only for cumulative match below.
    let _ = (&last_confirm_data, &last_refresh_data);

    // Aggregate matches
    let mut matches: Vec<Match> = Vec::new();
    matches.extend(find_matches(&checkout_value, "chatgpt_checkout"));
    matches.extend(find_matches(&init_data, "stripe_init"));
    matches.extend(find_matches(&elements_data, "stripe_elements"));
    for (variant, d) in &all_confirm_data {
        matches.extend(find_matches(d, &format!("confirm:{}", variant)));
    }
    for (variant, d) in &all_approve_data {
        matches.extend(find_matches(d, &format!("approve:{}", variant)));
    }
    for (i, d) in all_refresh_data.iter().enumerate() {
        matches.extend(find_matches(d, &format!("payment_page_refresh:{}", i + 1)));
    }
    let upi_uri = find_upi_uri(&matches);
    let qr_image_url = find_qr_image_url(&matches);
    let qr_expires_at = find_qr_expires_at(&matches);

    let mut qr_path: Option<String> = None;
    let mut qr_source: Option<String> = None;
    let mut qr_reason: Option<String> = None;

    if let Some(url) = &qr_image_url {
        let ext = if url.to_lowercase().ends_with(".svg") {
            "svg"
        } else {
            "png"
        };
        let target = cfg.qr_out_path.with_extension(ext);
        let watermark = if cfg.qr_watermark.is_empty() {
            None
        } else {
            Some(cfg.qr_watermark.as_str())
        };
        match download_qr_image(
            &client,
            url,
            &target,
            proxy_for_step(&cfg.proxy_pool, cfg.proxy_from_step, 5),
            watermark,
        )
        .await
        {
            Ok(d) if d.rendered => {
                qr_path = d.path.map(|p| p.to_string_lossy().to_string());
                qr_source = Some(d.source.unwrap_or_else(|| "stripe_image".into()));
            }
            Ok(d) => {
                qr_reason = d.reason.or_else(|| Some("stripe image download fail".into()));
            }
            Err(e) => {
                qr_reason = Some(format!("download fail: {}", e));
            }
        }
    } else if let Some(uri) = &upi_uri {
        let watermark = if cfg.qr_watermark.is_empty() {
            None
        } else {
            Some(cfg.qr_watermark.as_str())
        };
        match render_qr_png(uri, &cfg.qr_out_path, watermark) {
            Ok(()) => {
                qr_path = Some(cfg.qr_out_path.to_string_lossy().to_string());
                qr_source = Some("upi_uri".into());
            }
            Err(e) => qr_reason = Some(format!("qrcode render fail: {}", e)),
        }
    } else {
        qr_reason = Some("no upi:// URI or QR image URL found in any response".into());
    }

    if qr_path.is_some() {
        log(&format!(
            "[QR]  ready       OK   expires_at={}",
            qr_expires_at
                .map(|n| n.to_string())
                .unwrap_or_else(|| "—".into())
        ));
    } else {
        log(&format!(
            "[QR]  ready       FAIL {}",
            qr_reason.clone().unwrap_or_else(|| "unknown".into())
        ));
    }

    let elapsed = started.elapsed().as_secs_f64();
    let error_msg = if let Some(ref e) = fatal_approve_error {
        Some(e.clone())
    } else if !final_confirmed {
        Some("confirm thất bại với mọi variant".into())
    } else if !approved {
        Some(format!(
            "approve failed after {} attempts (retries={})",
            approve_attempts.len(),
            cfg.approve_retries
        ))
    } else if qr_path.is_none() {
        qr_reason.clone().or_else(|| Some("no QR generated".into()))
    } else {
        None
    };
    let ok = error_msg.is_none();

    log(&format!(
        "[done] {}  qr={} approved={} restarts={} total={:.1}s{}",
        if ok { "OK  " } else { "FAIL" },
        if qr_path.is_some() { "yes" } else { "no" },
        if approved { "yes" } else { "no" },
        restart_count,
        elapsed,
        error_msg
            .as_deref()
            .map(|e| format!("  error={}", e))
            .unwrap_or_default()
    ));

    UpiQrResult {
        ok,
        email: masked_email,
        amount,
        return_url,
        checkout_session: short(&session_id, 18),
        qr_path,
        qr_source,
        qr_source_url: qr_image_url,
        qr_reason,
        qr_expires_at,
        has_upi_uri: upi_uri.is_some(),
        has_qr_image_url: false, // re-set below
        confirm_attempts,
        approve_attempts,
        page_refresh_attempts: refresh_attempts,
        backend_exception_count,
        restart_count,
        error: error_msg,
        elapsed_seconds: elapsed,
    }
}

fn finalize_error(masked: String, started: Instant, msg: String) -> UpiQrResult {
    UpiQrResult {
        ok: false,
        email: masked,
        error: Some(msg),
        elapsed_seconds: started.elapsed().as_secs_f64(),
        ..Default::default()
    }
}

fn short(s: &str, head: usize) -> String {
    if s.len() <= head {
        s.to_string()
    } else {
        format!("{}…", &s[..head])
    }
}

/// Cắt error message dài (vd: full reqwest error chain) — giữ head + tail.
fn short_msg(s: &str, max: usize) -> String {
    let s = s.replace('\n', " ");
    if s.chars().count() <= max {
        s
    } else {
        let take_head = max.saturating_sub(20);
        let head: String = s.chars().take(take_head).collect();
        format!("{}…", head)
    }
}
