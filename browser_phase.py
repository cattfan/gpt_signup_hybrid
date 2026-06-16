"""Phase 1: Browser signup — register (email+pass) → OTP → /about-you → session.

Flow (theo HAR mới):
  1. chatgpt.com → bootstrap NextAuth (csrf + signin/openai) → authorize URL
  2. Navigate authorize → /email-verification page load
  3. Click "Continue with password" → /create-account/password
  4. Fill password → submit → POST /api/accounts/user/register {username, password}
  5. Server trigger OTP (GET /email-otp/send) → redirect /email-verification (OTP form)
  6. Poll OTP → submit → POST /email-otp/validate
  7. /about-you → fill name+age → POST /create_account
  8. Đợi session-token cookie (đã login)
  9. Exfil cookies → BrowserHandoff

Retry (account đã tồn tại):
  - Register trả lỗi "already exists" → fallback OTP-only login
  - HOẶC: OTP → login → chatgpt.com

Kết quả: BrowserHandoff đủ context để Phase 2 extract session/access_token.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Settings, ensure_runtime_dirs, prepare_profile_dir
from .mail_providers import MailProvider, OutlookComboError
from .models import BrowserHandoff, SignupRequest
from ._nextauth_bootstrap import bootstrap_authorize_url
from ._browser_retry import (
    DRIVER_DEAD_MARKERS as _DRIVER_DEAD_MARKERS,
    LAUNCH_RETRY_BACKOFF as _LAUNCH_RETRY_BACKOFF,
    LAUNCH_RETRY_MAX as _LAUNCH_RETRY_MAX,
    is_driver_dead_error as _is_driver_dead_error,
    is_navigation_timeout as _is_navigation_timeout,
    is_network_error as _is_network_error,
    parse_proxy_for_playwright as _parse_proxy,
)


class BrowserPhaseError(Exception):
    """Phase 1 failed."""


# Cookies bắt buộc cho Phase 2 (chatgpt.com session).
_REQUIRED_AUTH_COOKIES = (
    "oai-did",
    "__cf_bm",
    "cf_clearance",
)


# ─────────────────────────────────────────────────────────────────────
# JS helpers
# ─────────────────────────────────────────────────────────────────────

# JS: POST /api/accounts/user/register trên auth.openai.com page context
_REGISTER_USER_JS = r"""
async ({username, password}) => {
    const res = await fetch('/api/accounts/user/register', {
        method: 'POST',
        credentials: 'include',
        headers: {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Origin': window.location.origin,
            'Referer': window.location.origin + '/create-account/password',
        },
        body: JSON.stringify({username, password}),
    });
    const text = await res.text();
    let body = null;
    try { body = JSON.parse(text); } catch { body = text; }
    return {status: res.status, body};
}
"""

# JS: fill /about-you (Sentinel monitor form interactions)
_PAGE_CREATE_ACCOUNT_JS = r"""
async ({name, birthdate}) => {
    const res = await fetch('/api/accounts/create_account', {
        method: 'POST',
        credentials: 'include',
        headers: {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({name, birthdate}),
    });
    const text = await res.text();
    let body = null;
    try { body = JSON.parse(text); } catch { body = text; }
    return {status: res.status, body};
}
"""


# ─────────────────────────────────────────────────────────────────────
# Helper functions
# ─────────────────────────────────────────────────────────────────────


def _browser_health(ctx, page) -> str:
    """Non-blocking snapshot trạng thái browser/context/page để log debug.

    Trả về chuỗi short, không raise — dùng trước/sau thao tác có thể fail
    do target closed (Plan D: observability).

    Format: 'page=open ctx_pages=2 browser=connected'
            'page=CLOSED ctx_pages=0 browser=disconnected'
    """
    try:
        page_closed = page.is_closed()
        page_state = "CLOSED" if page_closed else "open"
    except Exception as exc:
        page_state = f"ERR({type(exc).__name__})"

    try:
        pages = list(getattr(ctx, "pages", []) or [])
        live_pages = sum(1 for p in pages if not _safe_is_closed(p))
        ctx_state = f"{live_pages}/{len(pages)}"
    except Exception as exc:
        ctx_state = f"ERR({type(exc).__name__})"

    try:
        browser = getattr(ctx, "browser", None)
        if browser is None:
            br_state = "n/a"
        else:
            br_state = "connected" if browser.is_connected() else "DISCONNECTED"
    except Exception as exc:
        br_state = f"ERR({type(exc).__name__})"

    return f"page={page_state} ctx_pages_live={ctx_state} browser={br_state}"


def _safe_is_closed(p) -> bool:
    try:
        return bool(p.is_closed())
    except Exception:
        return True


_GEOIP_CACHE_MAX_AGE = 86400  # 24h


def _ensure_geoip_cache(runtime_dir: Path, *, log) -> None:
    """Cache GeoIP mmdb locally so camoufox doesn't re-download every launch."""
    try:
        from camoufox.locale import MMDB_FILE, download_mmdb
    except ImportError:
        return
    if MMDB_FILE.exists():
        return
    cache = runtime_dir / "geoip" / "GeoLite2-City.mmdb"
    if cache.exists() and (time.time() - cache.stat().st_mtime) < _GEOIP_CACHE_MAX_AGE:
        cache.parent.mkdir(parents=True, exist_ok=True)
        MMDB_FILE.parent.mkdir(parents=True, exist_ok=True)
        import shutil as _shutil
        _shutil.copy2(cache, MMDB_FILE)
        log(f"[geoip] restored from cache ({cache})")
        return
    log("[geoip] downloading GeoIP database (cached for 24h)...")
    download_mmdb()
    cache.parent.mkdir(parents=True, exist_ok=True)
    import shutil as _shutil
    _shutil.copy2(MMDB_FILE, cache)
    log(f"[geoip] cached to {cache}")


async def _bootstrap_oauth_url(page, *, email: str, device_id: str, logging_id: str, log) -> str:
    """Gọi /api/auth/csrf + POST /signin/openai trong page context chatgpt.com."""
    log("[browser] bootstrapping NextAuth (csrf + signin)...")
    url = await bootstrap_authorize_url(
        page,
        email=email,
        device_id=device_id,
        logging_id=logging_id,
    )
    log(f"[browser] authorize URL ready: {url[:120]}...")
    return url


async def _register_with_password(page, *, email: str, password: str, log) -> str:
    """Đăng ký account bằng POST /api/accounts/user/register trên auth.openai.com.

    Flow:
      1. Click "Continue with password" (nếu cần)
      2. POST /api/accounts/user/register {username, password}
      3. GET continue_url (/email-otp/send) → trigger OTP

    Returns: "otp_sent" (success) hoặc raise error.
    """
    # Click "Continue with password" button nếu đang ở /email-verification
    try:
        pwd_btn = page.locator(
            'button:has-text("password"), a:has-text("password"), '
            '[role="button"]:has-text("password")'
        )
        if await pwd_btn.count() > 0:
            btn_text = await pwd_btn.first.text_content(timeout=1000)
            await pwd_btn.first.click(timeout=3000)
            log(f"[browser] clicked password button: {(btn_text or '').strip()[:60]}")
            # Đợi page navigate tới /create-account/password (SPA)
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if "password" in page.url:
                    break
                # Hoặc password input visible
                try:
                    pwd_input = page.locator('input[type="password"]').first
                    if await pwd_input.is_visible(timeout=500):
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.3)
            log(f"[browser] page ready: {page.url.split('?')[0]}")
    except Exception:
        pass

    await asyncio.sleep(0.5)

    # Check: nếu page ở /log-in/password → account đã tồn tại → login thay vì register
    if "log-in" in page.url:
        log("[browser] account exists → login with password")
        # Fill password form
        pwd_input = None
        for sel in ('input[type="password"]', 'input[name="password"]'):
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=2000):
                    pwd_input = loc
                    break
            except Exception:
                continue
        if pwd_input:
            await pwd_input.click(force=True, timeout=3000)
            await pwd_input.fill("")
            await pwd_input.type(password, delay=50)
            await asyncio.sleep(0.3)
            for btn in ('button[type="submit"]', 'button:has-text("Continue")'):
                try:
                    await page.click(btn, timeout=3000)
                    break
                except Exception:
                    continue
            log("[browser] submitted login password")
            return "login"
        raise BrowserPhaseError(f"login page but no password input. URL: {page.url}")

    # POST /api/accounts/user/register
    log(f"[browser] POST /api/accounts/user/register (email={email})")
    result = await page.evaluate(_REGISTER_USER_JS, {"username": email, "password": password})

    if not isinstance(result, dict):
        raise BrowserPhaseError(f"register unexpected result: {result}")

    status = result.get("status")
    body = result.get("body") or {}

    if status == 200:
        # Success → navigate tới continue_url để trigger OTP send
        continue_url = None
        if isinstance(body, dict):
            continue_url = body.get("continue_url")
        log(f"[browser] register OK → continue_url={continue_url}")

        if continue_url:
            if continue_url.startswith("/"):
                continue_url = f"https://auth.openai.com{continue_url}"
            await page.goto(continue_url, wait_until="domcontentloaded")
            log("[browser] OTP send triggered")
        # Đợi 1s để page settle — otp_started_at sẽ được set SAU đây bởi caller
        await asyncio.sleep(1.0)
        return "otp_sent"

    # Error cases
    body_str = json.dumps(body) if isinstance(body, dict) else str(body or "")

    # Account already exists → fallback: submit OTP trực tiếp (email đã gửi)
    if "already" in body_str.lower() or "exists" in body_str.lower() or status == 409:
        log(f"[browser] register: account already exists (HTTP {status}) — fallback OTP login")
        return "already_exists"

    raise BrowserPhaseError(f"register failed HTTP {status}: {body_str[:200]}")


async def _wait_otp_form(page, *, timeout_seconds: float, log) -> str:
    """Đợi OTP form xuất hiện. Return selector."""
    selectors = (
        'input[name="code"]',
        'input[autocomplete="one-time-code"]',
        'input[inputmode="numeric"]',
    )
    for sel in selectors:
        try:
            await page.wait_for_selector(sel, state="visible", timeout=int(timeout_seconds * 1000))
            log(f"[browser] OTP input ready ({sel})")
            return sel
        except Exception:
            continue
    raise BrowserPhaseError(f"OTP input không xuất hiện sau {timeout_seconds}s. URL: {page.url}")


async def _submit_otp(ctx, page, *, otp_code: str, otp_selector: str, log) -> None:
    """Fill OTP + click submit qua UI. Fallback: gọi validate API qua context request.

    Plan A — defensive:
      - Pre-check page.is_closed() trước khi fill
      - Nếu UI fill/click raise driver-dead error → fallback ctx.request.post()
        (không dùng page.evaluate vì page đã chết)
      - Mọi failure log kèm browser health snapshot (Plan D)
      - Cuối cùng vẫn fail → raise BrowserPhaseError với message rõ
    """
    log(f"[browser] typing OTP {otp_code} ({_browser_health(ctx, page)})")

    if _safe_is_closed(page):
        log(f"[browser] page closed before OTP fill — fallback API ({_browser_health(ctx, page)})")
        await _submit_otp_via_api(ctx, otp_code=otp_code, log=log)
        return

    # UI path: fill input + click submit
    ui_failed_with: BaseException | None = None
    try:
        await page.fill(otp_selector, otp_code)
    except Exception as exc:
        ui_failed_with = exc
        if _is_driver_dead_error(exc):
            log(
                f"[browser] page.fill failed — driver/page dead "
                f"({type(exc).__name__}: {exc}) — fallback API "
                f"({_browser_health(ctx, page)})"
            )
            await _submit_otp_via_api(ctx, otp_code=otp_code, log=log)
            return
        # Lỗi non-driver-dead khi fill (vd: selector mismatch) — vẫn thử click button
        log(
            f"[browser] page.fill error (non-driver) "
            f"{type(exc).__name__}: {exc} — vẫn thử click submit"
        )

    if ui_failed_with is None:
        for btn in ('button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Verify")'):
            try:
                await page.click(btn, timeout=2000)
                log(f"[browser] clicked {btn}")
                return
            except Exception as exc:
                if _is_driver_dead_error(exc):
                    log(
                        f"[browser] click {btn} — driver dead "
                        f"({type(exc).__name__}: {exc}) — fallback API "
                        f"({_browser_health(ctx, page)})"
                    )
                    await _submit_otp_via_api(ctx, otp_code=otp_code, log=log)
                    return
                continue

    # Không click được button nào (hoặc fill fail non-driver-dead) → fallback API
    log(
        f"[browser] no submit button worked — fallback API "
        f"({_browser_health(ctx, page)})"
    )
    await _submit_otp_via_api(ctx, otp_code=otp_code, log=log)


async def _submit_otp_via_api(ctx, *, otp_code: str, log) -> None:
    """Submit OTP qua context.request — không phụ thuộc page sống.

    Dùng cookies từ context (đã chia sẻ với page) để giữ session.
    Fail-fast nếu HTTP status không OK.
    """
    request_ctx = getattr(ctx, "request", None)
    if request_ctx is None:
        raise BrowserPhaseError(
            "OTP fallback failed: context.request không khả dụng "
            "(Camoufox/Playwright version cũ?)"
        )
    url = "https://auth.openai.com/api/accounts/email-otp/validate"
    try:
        resp = await request_ctx.post(
            url,
            data={"code": otp_code},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://auth.openai.com",
                "Referer": "https://auth.openai.com/email-verification",
            },
        )
    except Exception as exc:
        raise BrowserPhaseError(
            f"OTP fallback API request failed: {type(exc).__name__}: {exc}"
        ) from exc

    status = resp.status
    try:
        body_text = await resp.text()
    except Exception:
        body_text = "<no body>"
    log(f"[browser] OTP fallback API → HTTP {status} body={body_text[:120]}")
    if status >= 400:
        raise BrowserPhaseError(
            f"OTP validate API rejected: HTTP {status}: {body_text[:200]}"
        )


async def _wait_after_login(page, *, timeout_seconds: float, log) -> str:
    """Sau submit login password, đợi:
    - chatgpt.com (login OK, không cần OTP)
    - /email-verification (cần OTP)
    - error
    Returns: 'chatgpt' hoặc 'otp_required'.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        cur = page.url
        if "chatgpt.com" in cur and "auth.openai.com" not in cur and "/auth/error" not in cur:
            log("[browser] login OK — redirected to chatgpt.com")
            return "chatgpt"
        if "/email-verification" in cur or "/email-otp" in cur:
            log("[browser] login requires OTP")
            return "otp_required"
        if "/auth/error" in cur:
            raise BrowserPhaseError(f"login error page: {cur}")
        # Detect OTP form xuất hiện (SPA case)
        try:
            otp_input = page.locator('input[name="code"], input[autocomplete="one-time-code"]').first
            if await otp_input.is_visible(timeout=300):
                log("[browser] login OTP form detected (SPA)")
                return "otp_required"
        except Exception:
            pass
        # Detect login error (sai password)
        try:
            err_el = page.locator('[role="alert"], [class*="error"]').first
            err_text = await err_el.text_content(timeout=300)
            if err_text and ("incorrect" in err_text.lower() or "wrong password" in err_text.lower() or "invalid" in err_text.lower()):
                raise BrowserPhaseError(f"login error: {err_text.strip()}")
        except BrowserPhaseError:
            raise
        except Exception:
            pass
        await asyncio.sleep(0.5)
    raise BrowserPhaseError(f"timeout {timeout_seconds}s after login submit. URL: {page.url}")


async def _detect_screen(page) -> str:
    """Detect màn hình hiện tại từ URL + DOM. Return 1 trong:
      - 'chatgpt'              : đã login xong, page ở chatgpt.com
      - 'about_you'            : form name+age (auth.openai.com/about-you)
      - 'mfa_challenge'        : account có 2FA → cần TOTP code từ authenticator
      - 'turnstile_challenge'  : Cloudflare Turnstile challenge visible
      - 'otp'                  : OTP input visible (/email-verification or SPA)
      - 'password_create'      : /create-account/password (form set password mới)
      - 'password_login'       : /log-in/password (form login với account đã tồn tại)
      - 'continue'             : /email-verification trang chọn 'Continue with password'
      - 'auth_error'           : page lỗi /auth/error
      - 'unknown'              : không nhận diện được
    """
    cur = page.url
    if "/auth/error" in cur:
        return "auth_error"
    if "chatgpt.com" in cur and "auth.openai.com" not in cur:
        return "chatgpt"
    if "auth.openai.com/about-you" in cur:
        return "about_you"
    if "passkey" in cur.lower():
        return "passkey_enroll"
    # Nội dung SPA có thể đã render /about-you mà URL chưa đổi
    try:
        name_el = page.locator('input[name="name"], input[autocomplete="name"]').first
        if await name_el.is_visible(timeout=200):
            return "about_you"
    except Exception:
        pass

    # MFA challenge — phải check TRƯỚC OTP vì input selector trùng nhau
    # (cả MFA và OTP đều dùng input[name="code"] / inputmode=numeric).
    # Phân biệt qua URL pattern hoặc text marker đặc trưng MFA.
    if "/mfa" in cur or "/totp" in cur or "/two-factor" in cur:
        return "mfa_challenge"
    try:
        # Text marker: "authenticator app", "two-factor", "Enter the 6-digit code from your authenticator"
        mfa_text = page.locator(
            'text=/authenticator app/i, text=/two[- ]factor/i, text=/from your authenticator/i'
        ).first
        if await mfa_text.is_visible(timeout=200):
            return "mfa_challenge"
    except Exception:
        pass

    if "/create-account/password" in cur:
        return "password_create"
    if "/log-in/password" in cur:
        # SPA case: URL vẫn là /log-in/password nhưng content đã chuyển sang OTP form
        # hoặc "Check your inbox" page (email verification sau login)
        try:
            otp_input = page.locator('input[name="code"], input[autocomplete="one-time-code"]').first
            if await otp_input.is_visible(timeout=200):
                return "otp"
        except Exception:
            pass
        try:
            inbox_el = page.locator(
                'text="Check your inbox", text="Check your email", text="Enter the verification code"'
            ).first
            if await inbox_el.is_visible(timeout=200):
                return "otp"
        except Exception:
            pass
        return "password_login"
    # /email-verification: ƯU TIÊN button "password" để bắt buộc set password
    # Nếu cả OTP input và password button cùng visible, password button thắng
    _PWD_BTN_SELECTOR = (
        'button:has-text("password"), a:has-text("password"), '
        '[role="button"]:has-text("password")'
    )
    if "/email-verification" in cur or "/email-otp" in cur or "/identifier" in cur:
        try:
            pwd_btn = page.locator(_PWD_BTN_SELECTOR).first
            if await pwd_btn.is_visible(timeout=800):
                return "continue"
        except Exception:
            pass
    # Broad check: trên bất kỳ auth.openai.com page nào có nút password → ưu tiên click
    if "auth.openai.com" in cur:
        try:
            pwd_btn = page.locator(_PWD_BTN_SELECTOR).first
            if await pwd_btn.is_visible(timeout=300):
                return "continue"
        except Exception:
            pass
    # Turnstile / Cloudflare challenge — check trước OTP vì có thể overlay trên OTP form
    try:
        turnstile = page.locator(
            'iframe[src*="challenges.cloudflare.com"], '
            'iframe[src*="turnstile"], '
            '#cf-turnstile, .cf-turnstile, '
            '[data-turnstile-callback]'
        ).first
        if await turnstile.is_visible(timeout=200):
            return "turnstile_challenge"
    except Exception:
        pass
    # OTP form (URL có thể là /email-verification, /email-otp, /log-in/email-verification, ...)
    try:
        otp_input = page.locator('input[name="code"], input[autocomplete="one-time-code"]').first
        if await otp_input.is_visible(timeout=200):
            return "otp"
    except Exception:
        pass
    if "/email-verification" in cur or "/email-otp" in cur:
        return "otp"  # fallback: chỉ có OTP form, không có password button
    return "unknown"


async def _skip_passkey(page, *, log, leave_timeout: float = 10.0) -> bool:
    """Skip passkey enrollment page. Returns True khi đã rời khỏi passkey URL.

    Strategy:
      1. Click explicit skip/dismiss buttons (text-based)
      2. Click any secondary/non-primary button or link
    Sau khi click, ĐỢI URL không còn chứa "passkey" (timeout `leave_timeout`s).
    Nếu click rồi mà page vẫn ở passkey → return False (caller xử lý).
    KHÔNG fallback goto chatgpt.com — sẽ cướp navigation OAuth callback inflight,
    làm Set-Cookie session-token bị abort.
    """
    async def _wait_leave_passkey() -> bool:
        try:
            await page.wait_for_url(
                lambda u: "passkey" not in (u or "").lower(),
                timeout=int(leave_timeout * 1000),
            )
            return True
        except Exception:
            return False

    # 1. Explicit skip buttons
    for sel in (
        'button:has-text("Skip")',
        'button:has-text("Maybe later")',
        'button:has-text("Do this later")',
        'button:has-text("Not now")',
        'button:has-text("I\'ll do this later")',
        'a:has-text("Skip")',
        'a:has-text("Maybe later")',
        'a:has-text("Do this later")',
        'a:has-text("Not now")',
        'a:has-text("I\'ll do this later")',
        '[data-testid*="skip" i]',
        '[data-testid*="dismiss" i]',
    ):
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=800):
                await btn.click(timeout=3000)
                log(f"[browser] clicked skip passkey: {sel}")
                if await _wait_leave_passkey():
                    log("[browser] passkey page left after skip click")
                    return True
                log("[browser] click landed but URL still passkey — continue trying")
                break  # đừng click thêm selector khác, page đã transition
        except Exception:
            continue

    # 2. Log page content for debugging
    try:
        buttons_info = await page.evaluate(r"""
            () => {
                const els = [...document.querySelectorAll('button, a[href], [role="button"]')];
                return els.slice(0, 10).map(e => ({
                    tag: e.tagName, text: (e.textContent || '').trim().substring(0, 60),
                    cls: (e.className || '').substring(0, 40),
                }));
            }
        """)
        log(f"[browser] passkey page elements: {json.dumps(buttons_info, ensure_ascii=False)}")
    except Exception:
        pass

    # 3. Try clicking non-primary buttons (secondary/tertiary)
    try:
        all_buttons = page.locator('button, a[role="button"]')
        count = await all_buttons.count()
        for i in range(count):
            btn = all_buttons.nth(i)
            text = ((await btn.text_content()) or "").strip().lower()
            if any(k in text for k in ("create", "set up", "enable", "passkey")):
                continue
            if text and await btn.is_visible(timeout=500):
                await btn.click(timeout=3000)
                log(f"[browser] clicked non-primary button on passkey page: {text!r}")
                if await _wait_leave_passkey():
                    log("[browser] passkey page left after non-primary click")
                    return True
                break
    except Exception:
        pass

    log("[browser] could not leave passkey page after click attempts")
    return False


async def _drive_signup_flow(
    *, ctx, page, request, mail_provider, callback_holder, otp_started_at, log,
    overall_timeout: float = 240.0,
) -> tuple[str, float]:
    """State machine: check URL/DOM hiện tại, dispatch handler tương ứng.
    Lặp đến khi đến được chatgpt.com (có session) hoặc gặp lỗi không phục hồi.

    Returns: (callback_url, otp_seconds).
    """
    deadline = time.monotonic() + overall_timeout
    otp_seconds_total = 0.0
    otp_already_polled = False  # tránh poll OTP nhiều lần trong cùng batch
    register_attempted = False
    login_attempted = False
    continue_clicked = False
    otp_submitted = False
    _otp_submit_ts: float | None = None
    _otp_reclick_done = False
    _otp_js_submit_done = False
    _otp_api_done = False
    one_time_code_mode = False  # True sau khi click "Log in with a one-time code"
    tried_codes: set[str] = set()  # codes đã submit + bị reject
    pending_codes: list[str] = []  # codes chưa submit (mail delay catch)
    last_screen = None
    same_screen_count = 0

    while time.monotonic() < deadline:
        screen = await _detect_screen(page)

        # Trong one-time code login flow, /email-verification hiển thị "Continue with password"
        # nhưng ta muốn lấy OTP, không quay lại password
        if one_time_code_mode and screen == "continue":
            screen = "otp"

        if screen != last_screen:
            log(f"[flow] screen={screen} url={page.url.split('?')[0]}")
            last_screen = screen
            same_screen_count = 0
        else:
            same_screen_count += 1

        if screen == "chatgpt":
            await _wait_chatgpt_session(ctx, page, timeout_seconds=30.0, log=log)
            return callback_holder.get("url") or page.url, otp_seconds_total

        if screen == "auth_error":
            raise BrowserPhaseError(f"auth error page: {page.url}")

        if screen == "turnstile_challenge":
            if same_screen_count == 0:
                log("[flow] Turnstile/Cloudflare challenge detected — waiting for auto-solve")
            if same_screen_count > 60:
                raise BrowserPhaseError(
                    f"Turnstile challenge stuck >60 iterations. URL: {page.url}"
                )
            await asyncio.sleep(1.0)
            continue

        if screen == "mfa_challenge":
            # Account đã enable 2FA từ trước (combo đã từng dùng signup + 2FA).
            # Signup flow KHÔNG có TOTP secret để pass — fail-fast với message
            # rõ ràng để user biết dùng "Get Session" flow (cung cấp secret) thay vì retry signup.
            raise BrowserPhaseError(
                f"account đã có 2FA enabled — signup flow không có TOTP secret. "
                f"Dùng Get Session flow với combo email|password|secret. URL: {page.url}"
            )

        if screen == "continue":
            if continue_clicked:
                # Đã click rồi mà page chưa chuyển → đợi thêm rồi retry detect
                await asyncio.sleep(1.0)
                continue
            _pwd_sel = (
                'button:has-text("password"), a:has-text("password"), '
                '[role="button"]:has-text("password")'
            )
            try:
                pwd_btn = page.locator(_pwd_sel).first
                btn_text = await pwd_btn.text_content(timeout=1000)
                await pwd_btn.click(timeout=3000)
                log(f"[flow] clicked password button: {(btn_text or '').strip()[:60]}")
                continue_clicked = True
            except Exception as exc:
                log(f"[flow] click password button failed: {exc}")
            await asyncio.sleep(1.5)
            continue

        if screen == "password_create":
            if register_attempted:
                await asyncio.sleep(1.0)
                continue
            log(f"[flow] POST /api/accounts/user/register (email={request.email})")
            result = await page.evaluate(
                _REGISTER_USER_JS, {"username": request.email, "password": request.password},
            )
            register_attempted = True
            if not isinstance(result, dict):
                raise BrowserPhaseError(f"register unexpected result: {result}")
            status = result.get("status")
            body = result.get("body") or {}
            if status == 200:
                continue_url = body.get("continue_url") if isinstance(body, dict) else None
                log(f"[flow] register OK → continue_url={continue_url}")
                if continue_url:
                    if continue_url.startswith("/"):
                        continue_url = f"https://auth.openai.com{continue_url}"
                    await page.goto(continue_url, wait_until="domcontentloaded")
                await asyncio.sleep(1.0)
                continue
            body_str = json.dumps(body) if isinstance(body, dict) else str(body or "")
            if "already" in body_str.lower() or "exists" in body_str.lower() or status == 409:
                log("[flow] account already exists — page sẽ chuyển login")
                await asyncio.sleep(1.5)
                continue
            raise BrowserPhaseError(f"register failed HTTP {status}: {body_str[:200]}")

        if screen == "password_login":
            if login_attempted:
                # Detect sai password → click "Log in with a one-time code" để dùng OTP thay thế
                if same_screen_count >= 3:
                    try:
                        err_el = page.locator('[role="alert"], [class*="error"]').first
                        err_text = await err_el.text_content(timeout=300)
                        if err_text and any(k in err_text.lower() for k in ("incorrect", "wrong", "invalid")):
                            log(f"[flow] login password wrong ({err_text.strip()[:60]}) — trying one-time code fallback")
                            otc_btn = page.locator(
                                'button:has-text("Log in with a one-time code"), '
                                'a:has-text("Log in with a one-time code")'
                            ).first
                            await otc_btn.click(timeout=5000)
                            log("[flow] clicked 'Log in with a one-time code' — waiting for OTP screen")
                            one_time_code_mode = True
                            login_attempted = False  # reset để không loop vào đây nữa
                            await asyncio.sleep(2.0)
                            continue
                    except Exception:
                        pass
                await asyncio.sleep(1.0)
                continue
            log("[flow] login with password")
            pwd_input = None
            for sel in ('input[type="password"]', 'input[name="password"]'):
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=2000):
                        pwd_input = loc
                        break
                except Exception:
                    continue
            if not pwd_input:
                raise BrowserPhaseError(f"login page but no password input. URL: {page.url}")
            await pwd_input.click(force=True, timeout=3000)
            await pwd_input.fill("")
            await pwd_input.type(request.password, delay=50)
            await asyncio.sleep(0.3)
            for btn in ('button[type="submit"]', 'button:has-text("Continue")'):
                try:
                    await page.click(btn, timeout=3000)
                    break
                except Exception:
                    continue
            log("[flow] submitted login password")
            login_attempted = True
            await asyncio.sleep(1.5)
            continue

        if screen == "otp":
            # Detect "incorrect code" error → thử code kế (nếu có) hoặc resend + poll
            try:
                err_el = page.locator('[role="alert"], [class*="error"]').first
                err_text = await err_el.text_content(timeout=200)
                if err_text and any(k in err_text.lower() for k in ("incorrect", "wrong", "invalid", "expired")):
                    # Clear input trước
                    try:
                        otp_inp = page.locator('input[name="code"]').first
                        await otp_inp.fill("")
                    except Exception:
                        pass
                    # Nếu còn pending code (mail delay) → thử ngay, không resend
                    if pending_codes:
                        next_code = pending_codes.pop(0)
                        log(f"[flow] OTP rejected: {err_text.strip()[:60]} — thử code kế: {next_code}")
                        otp_selector = await _wait_otp_form(page, timeout_seconds=5.0, log=log)
                        await _submit_otp(ctx, page, otp_code=next_code, otp_selector=otp_selector, log=log)
                        tried_codes.add(next_code)
                        otp_submitted = True
                        _otp_submit_ts = time.monotonic()
                        _otp_reclick_done = False
                        _otp_js_submit_done = False
                        _otp_api_done = False
                        same_screen_count = 0
                        await asyncio.sleep(2.0)
                        continue
                    # Không còn pending → resend
                    log(f"[flow] OTP rejected: {err_text.strip()[:80]} — resend email & poll lại")
                    try:
                        resend_btn = page.locator('button:has-text("Resend"), a:has-text("Resend")').first
                        await resend_btn.click(timeout=3000)
                        log("[flow] clicked 'Resend email'")
                    except Exception as exc:
                        log(f"[flow] resend button not found: {exc}")
                    # Reset state để poll code mới
                    otp_submitted = False
                    _otp_submit_ts = None
                    same_screen_count = 0
                    await asyncio.sleep(2.0)
            except Exception:
                pass

            if otp_submitted:
                # Đã submit rồi, đợi page chuyển.
                # Dùng wall-clock time (không phải counter) vì mỗi iteration ~1-2s.
                if _otp_submit_ts is None:
                    _otp_submit_ts = time.monotonic()
                _otp_wait_elapsed = time.monotonic() - _otp_submit_ts

                if _otp_wait_elapsed > 10.0 and not _otp_reclick_done:
                    log(f"[flow] OTP screen vẫn ở đây sau {_otp_wait_elapsed:.0f}s — thử click submit lại (url={page.url})")
                    for btn in ('button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Verify")'):
                        try:
                            await page.click(btn, timeout=2000)
                            break
                        except Exception:
                            continue
                    _otp_reclick_done = True
                elif _otp_wait_elapsed > 18.0 and not _otp_js_submit_done:
                    log("[flow] OTP UI click không work — thử form.submit() qua JS")
                    try:
                        await page.evaluate("""() => {
                            const form = document.querySelector('form');
                            if (form) form.submit();
                        }""")
                    except Exception as exc:
                        log(f"[flow] JS form.submit() failed: {type(exc).__name__}: {exc}")
                    _otp_js_submit_done = True
                elif _otp_wait_elapsed > 25.0 and not _otp_api_done:
                    log("[flow] OTP UI+JS submit không work — thử validate qua API")
                    try:
                        otp_inp = page.locator('input[name="code"]').first
                        otp_val = await otp_inp.input_value()
                        if otp_val and len(otp_val) == 6:
                            await _submit_otp_via_api(ctx, otp_code=otp_val, log=log)
                    except Exception as exc:
                        log(f"[flow] API fallback failed: {type(exc).__name__}: {exc}")
                    _otp_api_done = True
                elif _otp_wait_elapsed > 35.0:
                    log("[flow] OTP stuck >35s — re-poll code mới")
                    otp_submitted = False
                    _otp_submit_ts = None
                    _otp_reclick_done = False
                    _otp_js_submit_done = False
                    _otp_api_done = False
                    try:
                        otp_inp = page.locator('input[name="code"]').first
                        await otp_inp.fill("")
                    except Exception:
                        pass
                await asyncio.sleep(0.5)
                continue
            # Đợi OTP input fully ready
            try:
                otp_selector = await _wait_otp_form(page, timeout_seconds=10.0, log=log)
            except BrowserPhaseError:
                await asyncio.sleep(0.5)
                continue
            await asyncio.sleep(1.0)
            # Reset timestamp khi sắp poll — bỏ qua code cũ trước thời điểm này
            poll_started = datetime.now(timezone.utc).replace(microsecond=0)
            t_otp = time.monotonic()
            recipient = request.source_email or request.email
            log(f"[flow] polling OTP (recipient={recipient}) since {poll_started.isoformat()}")
            
            # Poll OTP, skip codes đã thử.
            # Nếu đợi >30s chưa có code mới → click Resend rồi poll tiếp.
            # iCloud có thể gửi mail mới trước, mail cũ delay → lấy nhiều codes
            # rồi thử lần lượt trước khi resend.
            resend_after_seconds = 30.0
            resend_count = 0
            max_resends = 3  # tối đa resend 3 lần trong 1 lượt OTP
            stale_poll_count = 0  # đếm lần poll liên tiếp chỉ nhận code cũ
            stale_poll_resend_threshold = 5  # sau 5 lần poll chỉ code cũ → resend
            while True:
                # Nếu có codes pending chưa submit → thử từng cái
                if pending_codes:
                    otp_code = pending_codes.pop(0)
                    break
                remaining = request.otp_timeout_seconds - (time.monotonic() - t_otp)
                if remaining <= 0:
                    raise BrowserPhaseError(f"OTP timeout {request.otp_timeout_seconds}s, chỉ nhận được codes cũ")
                # Poll với mini-timeout = min(resend_after_seconds, remaining)
                mini_timeout = min(resend_after_seconds, remaining)
                try:
                    otp_code = await mail_provider.poll_otp(
                        recipient=recipient,
                        started_at=poll_started,
                        timeout_seconds=mini_timeout,
                        poll_interval_seconds=request.otp_poll_interval_seconds,
                        log=log,
                    )
                except OutlookComboError:
                    raise
                except Exception:
                    otp_code = None
                if otp_code and otp_code not in tried_codes:
                    # Nhận code mới → fetch lại tất cả codes để catch mail delay
                    await asyncio.sleep(3.0)
                    all_codes: list[str] = []
                    if hasattr(mail_provider, 'poll_all_codes'):
                        all_codes = await mail_provider.poll_all_codes(
                            recipient=recipient,
                            started_at=poll_started,
                            log=log,
                        )
                    # Lọc codes chưa thử, giữ thứ tự
                    new_codes = [c for c in all_codes if c not in tried_codes]
                    if not new_codes:
                        new_codes = [otp_code]
                    elif otp_code not in new_codes:
                        new_codes.insert(0, otp_code)
                    if len(new_codes) > 1:
                        log(f"[flow] got {len(new_codes)} OTP codes: {', '.join(new_codes)}")
                    pending_codes = new_codes
                    continue  # loop lại → pop từ pending_codes
                if otp_code and otp_code in tried_codes:
                    # Code cũ quay lại — đếm, sau N lần → resend
                    stale_poll_count += 1
                    if stale_poll_count >= stale_poll_resend_threshold and resend_count < max_resends:
                        resend_count += 1
                        stale_poll_count = 0
                        log(f"[flow] poll {stale_poll_resend_threshold} lần chỉ code cũ — click Resend ({resend_count}/{max_resends})")
                        try:
                            resend_btn = page.locator('button:has-text("Resend"), a:has-text("Resend")').first
                            await resend_btn.click(timeout=3000)
                            log("[flow] clicked 'Resend email'")
                        except Exception as exc:
                            log(f"[flow] resend button not found: {exc}")
                        await asyncio.sleep(2.0)
                        poll_started = datetime.now(timezone.utc).replace(microsecond=0)
                    else:
                        log(f"[flow] OTP={otp_code} đã thử rồi, tiếp tục poll... ({stale_poll_count}/{stale_poll_resend_threshold})")
                        await asyncio.sleep(request.otp_poll_interval_seconds)
                    continue
                # otp_code is None → timeout thật sự, không code nào về → resend
                if resend_count < max_resends:
                    resend_count += 1
                    log(f"[flow] OTP chưa nhận sau {mini_timeout:.0f}s — click Resend ({resend_count}/{max_resends})")
                    try:
                        resend_btn = page.locator('button:has-text("Resend"), a:has-text("Resend")').first
                        await resend_btn.click(timeout=3000)
                        log("[flow] clicked 'Resend email'")
                    except Exception as exc:
                        log(f"[flow] resend button not found: {exc}")
                    # Reset poll_started để chỉ nhận code mới sau resend
                    await asyncio.sleep(2.0)
                    poll_started = datetime.now(timezone.utc).replace(microsecond=0)
            
            otp_seconds_total += time.monotonic() - t_otp
            log(f"[flow] OTP={otp_code} got in {time.monotonic() - t_otp:.1f}s")
            tried_codes.add(otp_code)
            await _submit_otp(ctx, page, otp_code=otp_code, otp_selector=otp_selector, log=log)
            otp_submitted = True
            _otp_submit_ts = time.monotonic()
            _otp_reclick_done = False
            _otp_js_submit_done = False
            _otp_api_done = False
            otp_already_polled = True
            await asyncio.sleep(2.0)
            continue

        if screen == "passkey_enroll":
            log("[flow] passkey enrollment page — skipping")
            if await _skip_passkey(page, log=log):
                await asyncio.sleep(2.0)
            else:
                log("[flow] no skip button found on passkey page — waiting for page change")
                await asyncio.sleep(1.5)
            continue

        if screen == "about_you":
            # Set password TRƯỚC khi fill about_you — khi login bằng one-time code,
            # account chưa có password. Gọi register endpoint set password mới
            # trong session context đã verify OTP.
            if one_time_code_mode:
                try:
                    reg = await page.evaluate(
                        _REGISTER_USER_JS,
                        {"username": request.email, "password": request.password},
                    )
                    st = reg.get("status") if isinstance(reg, dict) else None
                    bd = reg.get("body") if isinstance(reg, dict) else reg
                    log(f"[flow] set password (about_you ctx): HTTP {st}: {str(bd)[:100]}")
                except Exception as exc:
                    log(f"[flow] set password (about_you ctx) failed: {exc}")
            try:
                await _wait_oai_sc(ctx, timeout_seconds=15, log=log)
            except BrowserPhaseError:
                pass  # cookie có thể chưa cần thiết, thử fill xem có pass không
            callback_url = await _fill_about_you(
                page, name=request.name, birthdate=request.birthdate,
                timeout_seconds=60.0, log=log,
            )
            # Sau /about-you có thể vẫn còn step (rare), tiếp tục loop để chờ chatgpt.com
            await _wait_chatgpt_session(ctx, page, timeout_seconds=60.0, log=log)
            return callback_url, otp_seconds_total

        # screen == 'unknown' → đợi page settle
        await asyncio.sleep(0.7)

    raise BrowserPhaseError(f"flow timeout {overall_timeout}s. last URL: {page.url}, last screen: {last_screen}")


async def _handle_login_after_password(
    *, ctx, page, request, mail_provider, callback_holder, log,
) -> tuple[str, float]:
    """Sau khi submit login password, xử lý cả 2 case:
    - Login thẳng → chatgpt.com
    - Cần OTP → poll OTP → submit → /about-you HOẶC chatgpt.com
    Returns: (callback_url, otp_seconds).
    """
    otp_seconds = 0.0
    login_branch = await _wait_after_login(page, timeout_seconds=20.0, log=log)
    if login_branch == "chatgpt":
        await _wait_chatgpt_session(ctx, page, timeout_seconds=30.0, log=log)
        return callback_holder.get("url") or page.url, otp_seconds

    # Cần OTP cho login (hoặc account chưa hoàn thành onboarding)
    otp_selector = await _wait_otp_form(page, timeout_seconds=15.0, log=log)
    await asyncio.sleep(2.0)
    otp_started_at = datetime.now(timezone.utc).replace(microsecond=0)

    t_otp = time.monotonic()
    recipient = request.source_email or request.email
    log(f"[browser] polling OTP for login (recipient={recipient})")
    otp_code = await mail_provider.poll_otp(
        recipient=recipient,
        started_at=otp_started_at,
        timeout_seconds=request.otp_timeout_seconds,
        poll_interval_seconds=request.otp_poll_interval_seconds,
        log=log,
    )
    otp_seconds = time.monotonic() - t_otp
    log(f"[browser] login OTP={otp_code} in {otp_seconds:.1f}s")
    await _submit_otp(ctx, page, otp_code=otp_code, otp_selector=otp_selector, log=log)

    # Sau OTP có 2 case:
    # 1. /about-you (account chưa onboard) → fill name+age → callback
    # 2. chatgpt.com (login bình thường) → wait session-token
    otp_branch = await _wait_after_otp(page, ctx=ctx, timeout_seconds=60.0, log=log)
    if otp_branch == "signup":
        await _wait_oai_sc(ctx, timeout_seconds=15, log=log)
        callback_url = await _fill_about_you(
            page,
            name=request.name,
            birthdate=request.birthdate,
            timeout_seconds=30.0,
            log=log,
        )
    else:
        callback_url = callback_holder.get("url") or page.url

    await _wait_chatgpt_session(ctx, page, timeout_seconds=60.0, log=log)
    return callback_url, otp_seconds


async def _wait_after_otp(page, *, ctx, timeout_seconds: float, log) -> str:
    """Sau submit OTP, đợi navigation: /about-you (signup) hoặc chatgpt.com (login).

    Returns: "signup" hoặc "login".
    Escalation: 10s re-click → 18s JS submit → 25s API fallback → timeout.
    """
    deadline = time.monotonic() + timeout_seconds
    start_ts = time.monotonic()
    _reclick_done = False
    _js_done = False
    _api_done = False
    while time.monotonic() < deadline:
        cur = page.url
        if "auth.openai.com/about-you" in cur:
            log("[browser] reached /about-you (signup)")
            return "signup"
        if "chatgpt.com" in cur and "auth.openai.com" not in cur and "/auth/error" not in cur:
            log("[browser] redirected to chatgpt.com (login — account exists)")
            return "login"
        if "auth/error" in cur:
            raise BrowserPhaseError(f"error page: {cur}")
        # SPA case: URL vẫn /email-verification nhưng form /about-you đã render
        try:
            name_el = page.locator('input[name="name"], input[autocomplete="name"]').first
            if await name_el.is_visible(timeout=300):
                log("[browser] detected /about-you form (SPA, URL unchanged)")
                return "signup"
        except Exception:
            pass
        # Check OTP error message (wrong code)
        try:
            err_el = page.locator('[role="alert"], [class*="error"]').first
            err_text = await err_el.text_content(timeout=300)
            if err_text and ("wrong" in err_text.lower() or "invalid" in err_text.lower() or "incorrect" in err_text.lower()):
                raise BrowserPhaseError(f"OTP wrong code: {err_text.strip()}")
        except BrowserPhaseError:
            raise
        except Exception:
            pass
        # Escalation: re-click → JS submit → API fallback
        elapsed = time.monotonic() - start_ts
        if elapsed > 10.0 and not _reclick_done:
            try:
                otp_input = page.locator('input[name="code"]').first
                if await otp_input.is_visible(timeout=500):
                    val = await otp_input.input_value()
                    if val and len(val) == 6:
                        log(f"[browser] OTP form still visible after {elapsed:.0f}s — retrying submit (url={cur})")
                        for btn in ('button[type="submit"]', 'button:has-text("Continue")'):
                            try:
                                await page.click(btn, timeout=2000)
                                log(f"[browser] re-clicked {btn}")
                                break
                            except Exception:
                                continue
            except Exception:
                pass
            _reclick_done = True
        elif elapsed > 18.0 and not _js_done:
            log("[browser] OTP UI click không work — thử form.submit() qua JS")
            try:
                await page.evaluate("() => { const f = document.querySelector('form'); if (f) f.submit(); }")
            except Exception as exc:
                log(f"[browser] JS form.submit() failed: {type(exc).__name__}: {exc}")
            _js_done = True
        elif elapsed > 25.0 and not _api_done:
            log("[browser] OTP UI+JS không work — thử validate qua API")
            try:
                otp_input = page.locator('input[name="code"]').first
                otp_val = await otp_input.input_value()
                if otp_val and len(otp_val) == 6:
                    await _submit_otp_via_api(ctx, otp_code=otp_val, log=log)
            except Exception as exc:
                log(f"[browser] API fallback failed: {type(exc).__name__}: {exc}")
            _api_done = True
        await asyncio.sleep(0.5)
    raise BrowserPhaseError(f"timeout {timeout_seconds}s after OTP submit. URL: {page.url}")


async def _check_about_you_extras(page, *, log) -> None:
    """Check + handle các element bổ sung trên /about-you (checkbox TOS, select, etc.)."""
    # Checkbox — check tất cả unchecked checkboxes (TOS, marketing opt-in, etc.)
    try:
        checkboxes = page.locator('input[type="checkbox"]')
        count = await checkboxes.count()
        for i in range(count):
            cb = checkboxes.nth(i)
            if await cb.is_visible(timeout=300) and not await cb.is_checked():
                await cb.check(timeout=2000)
                label = ""
                try:
                    parent = cb.locator("xpath=ancestor::label")
                    label = (await parent.text_content(timeout=500) or "").strip()[:60]
                except Exception:
                    pass
                log(f"[browser] /about-you checked checkbox: {label or f'#{i}'}")
    except Exception:
        pass

    # Select dropdowns — nếu có select chưa chọn, chọn option đầu tiên có value
    try:
        selects = page.locator("select")
        count = await selects.count()
        for i in range(count):
            sel = selects.nth(i)
            if await sel.is_visible(timeout=300):
                val = await sel.input_value()
                if not val:
                    # Chọn option đầu tiên có value thực
                    first_option = await sel.evaluate("""
                        (el) => {
                            const opts = [...el.options].filter(o => o.value && o.value !== '');
                            return opts.length > 0 ? opts[0].value : null;
                        }
                    """)
                    if first_option:
                        await sel.select_option(first_option, timeout=2000)
                        log(f"[browser] /about-you selected option: {first_option}")
    except Exception:
        pass


async def _click_submit_about_you(page, *, log) -> None:
    """Click submit button trên /about-you form."""
    for btn in (
        'button[type="submit"]',
        'button:has-text("Continue")',
        'button:has-text("Agree")',
        'button:has-text("Next")',
        'button:has-text("Submit")',
    ):
        try:
            btn_el = page.locator(btn).first
            if await btn_el.is_visible(timeout=800) and await btn_el.is_enabled(timeout=500):
                await btn_el.click(timeout=3000)
                log(f"[browser] clicked {btn}")
                return
        except Exception:
            continue
    # Fallback: click bất kỳ button nào visible + enabled (trừ modal dismiss)
    try:
        all_btns = page.locator("button")
        count = await all_btns.count()
        for i in range(count):
            b = all_btns.nth(i)
            if await b.is_visible(timeout=300) and await b.is_enabled(timeout=300):
                text = ((await b.text_content(timeout=500)) or "").strip().lower()
                if text and not any(k in text for k in ("cancel", "back", "sign out", "log out")):
                    await b.click(timeout=3000)
                    log(f"[browser] fallback clicked button: {text[:40]}")
                    return
    except Exception:
        pass


async def _detect_about_you_form_error(page) -> str | None:
    """Detect validation error message trên /about-you form. Return message hoặc None."""
    try:
        for sel in (
            '[role="alert"]',
            '[class*="error"]',
            '[class*="Error"]',
            '[aria-invalid="true"]',
            '.field-error',
            '[data-testid*="error"]',
        ):
            el = page.locator(sel).first
            if await el.is_visible(timeout=200):
                text = (await el.text_content(timeout=500) or "").strip()
                if text:
                    return text[:200]
    except Exception:
        pass
    return None


async def _log_about_you_dom(page, *, log) -> None:
    """Log DOM snapshot nhẹ của /about-you form khi hết retry — giúp debug."""
    try:
        snapshot = await page.evaluate("""
            () => {
                const form = document.querySelector('form');
                if (!form) return {form: null, buttons: [], inputs: []};
                const inputs = [...form.querySelectorAll('input, select, textarea')].map(el => ({
                    tag: el.tagName, type: el.type || '', name: el.name || '',
                    value: el.value ? el.value.substring(0, 30) : '',
                    valid: el.validity ? el.validity.valid : true,
                    validationMsg: el.validationMessage || '',
                }));
                const buttons = [...form.querySelectorAll('button')].map(el => ({
                    text: (el.textContent || '').trim().substring(0, 40),
                    type: el.type || '', disabled: el.disabled,
                }));
                return {inputs, buttons};
            }
        """)
        log(f"[browser] /about-you DOM snapshot: {json.dumps(snapshot, ensure_ascii=False)[:500]}")
    except Exception as exc:
        log(f"[browser] /about-you DOM snapshot failed: {exc}")


async def _fill_about_you(page, *, name: str, birthdate: str, timeout_seconds: float, log) -> str:
    """Điền form /about-you (name + age), submit, return callback URL.

    CALLBACK CAPTURE STRATEGY (thay đổi 2026-05):
      - Dùng RESPONSE listener thay vì REQUEST listener để xác nhận
        callback đã thật sự thành công (có Set-Cookie session-token).
      - Lý do: request listener fire NGAY khi request đi ra, chưa biết
        server có response 200/302 hay không, có set cookie hay chưa.
        Đây là root cause của bug "callback URL captured" rồi vẫn
        timeout waiting session-token (page kẹt /about-you).
      - Fail-fast: nếu response status >= 400 → raise (account creation failed).
    """
    log(f"[browser] /about-you: fill name={name!r}")

    # Capture callback URL via RESPONSE listener (xác nhận server đã commit cookie)
    callback_holder: dict[str, Any] = {}

    def _on_resp(response):
        url = response.url
        if "chatgpt.com/api/auth/callback/openai" not in url or "code=" not in url:
            return
        # Đã capture rồi — bỏ qua (chỉ giữ lần đầu)
        if "url" in callback_holder:
            return
        status = response.status
        callback_holder["status"] = status
        # Status 2xx/3xx → callback OK. NextAuth thường trả 302 redirect.
        if 200 <= status < 400:
            callback_holder["url"] = url
            # Probe Set-Cookie từ headers (best-effort, có thể không thấy
            # do Playwright không expose Set-Cookie trên cross-origin redirect).
            try:
                set_cookie = response.headers.get("set-cookie", "")
                has_session = "next-auth.session-token" in set_cookie
                callback_holder["has_session_in_setcookie"] = has_session
                log(
                    f"[browser] callback response OK: HTTP {status} "
                    f"set-cookie-has-session={has_session}"
                )
            except Exception:
                log(f"[browser] callback response OK: HTTP {status}")
        else:
            # 4xx/5xx — log để debug, không raise ngay (background event)
            callback_holder["error_status"] = status
            log(f"[browser] callback response FAILED: HTTP {status}")

    page.on("response", _on_resp)
    try:
        # Name input
        name_input = None
        for sel in ('input[name="name"]', 'input[autocomplete="name"]', 'input[id*="name" i]'):
            try:
                await page.wait_for_selector(sel, state="visible", timeout=5000)
                name_input = sel
                break
            except Exception:
                continue
        if not name_input:
            raise BrowserPhaseError("không tìm thấy name input trên /about-you")

        await page.click(name_input, force=True, timeout=3000)
        await page.fill(name_input, "")
        await page.type(name_input, name, delay=80)
        await asyncio.sleep(0.2)

        # Age (parse from birthdate)
        try:
            year, month, day = birthdate.split("-")
            today = datetime.utcnow()
            age = today.year - int(year) - ((today.month, today.day) < (int(month), int(day)))
        except ValueError as exc:
            raise BrowserPhaseError(f"birthdate format sai: {birthdate}") from exc

        # Try date input first, fallback to age number input
        date_input = None
        try:
            date_input = await page.wait_for_selector('input[type="date"]', state="visible", timeout=1500)
        except Exception:
            pass

        if date_input:
            await page.fill('input[type="date"]', birthdate)
            log(f"[browser] filled birthday={birthdate}")
        else:
            age_input = None
            for sel in ('input[name="age"]', 'input[type="number"]', 'input[inputmode="numeric"]'):
                try:
                    await page.wait_for_selector(sel, state="visible", timeout=1500)
                    age_input = sel
                    break
                except Exception:
                    continue
            if age_input:
                await page.click(age_input, force=True, timeout=3000)
                await page.fill(age_input, "")
                await page.type(age_input, str(age), delay=120)
                log(f"[browser] typed age={age}")
            else:
                await page.keyboard.press("Tab")
                await asyncio.sleep(0.4)
                await page.keyboard.type(str(age), delay=120)
                log(f"[browser] Tab + typed age={age}")

        await asyncio.sleep(0.3)

        # Handle unchecked checkboxes/TOS trước submit — OpenAI có thể thêm field mới
        await _check_about_you_extras(page, log=log)

        # Submit
        await _click_submit_about_you(page, log=log)

        # Đợi callback URL hoặc navigate đến chatgpt.com
        deadline = time.monotonic() + timeout_seconds
        next_retry_at = time.monotonic() + 8.0
        submit_attempts = 1
        max_submit_attempts = 5
        passkey_skip_attempted = False
        dom_logged = False
        while time.monotonic() < deadline:
            # Fail-fast nếu response callback trả error
            if "error_status" in callback_holder and "url" not in callback_holder:
                raise BrowserPhaseError(
                    f"callback /api/auth/callback/openai failed: "
                    f"HTTP {callback_holder['error_status']}"
                )
            if "url" in callback_holder:
                # Sleep ngắn để cookie jar commit (response → cookie store ghi)
                # trước khi return cho caller poll cookies.
                await asyncio.sleep(0.8)
                log(
                    f"[browser] callback URL captured "
                    f"(HTTP {callback_holder.get('status', '?')})"
                )
                return callback_holder["url"]
            cur = page.url
            if "auth/error" in cur:
                raise BrowserPhaseError(f"error page: {cur}")
            # Nếu page đã navigate ra khỏi /about-you → chatgpt.com
            if "chatgpt.com" in cur:
                log("[browser] navigated to chatgpt.com (no explicit callback)")
                return callback_holder.get("url") or cur
            # Detect consent/modal buttons mới
            for accept_btn in (
                'button:has-text("Okay")',
                'button:has-text("I agree")',
                'button:has-text("Accept")',
                'button:has-text("Got it")',
                'button:has-text("Let")',
            ):
                try:
                    btn_el = page.locator(accept_btn).first
                    if await btn_el.is_visible(timeout=200):
                        await btn_el.click(timeout=2000)
                        log(f"[browser] clicked modal button: {accept_btn}")
                        break
                except Exception:
                    continue
            # Passkey enrollment — skip
            if "passkey" in cur.lower():
                if not passkey_skip_attempted:
                    passkey_skip_attempted = True
                    if await _skip_passkey(page, log=log):
                        await asyncio.sleep(1.0)
                        continue
                    log("[browser] passkey skip failed — waiting for natural navigation")
                await asyncio.sleep(1.0)
                continue
            if passkey_skip_attempted and "passkey" not in cur.lower():
                passkey_skip_attempted = False
            # Retry submit nếu vẫn stuck /about-you — mỗi 8s, tối đa max_submit_attempts
            if "about-you" in cur and time.monotonic() > next_retry_at:
                if submit_attempts < max_submit_attempts:
                    submit_attempts += 1
                    # Detect form validation errors trước khi retry
                    form_err = await _detect_about_you_form_error(page)
                    if form_err:
                        log(f"[browser] /about-you form error: {form_err}")
                    # Re-check extras (checkbox/TOS xuất hiện sau render)
                    await _check_about_you_extras(page, log=log)
                    # Thử submit lại với chiến thuật escalating
                    if submit_attempts <= 3:
                        await _click_submit_about_you(page, log=log)
                    else:
                        # Escalate: Enter key + JS dispatch
                        log(f"[browser] /about-you submit attempt {submit_attempts} — trying Enter + JS")
                        try:
                            await page.keyboard.press("Enter")
                        except Exception:
                            pass
                        await asyncio.sleep(1.0)
                        if "about-you" in page.url:
                            try:
                                await page.evaluate("""
                                    () => {
                                        const form = document.querySelector('form');
                                        if (form) {
                                            form.requestSubmit
                                                ? form.requestSubmit()
                                                : form.submit();
                                        }
                                    }
                                """)
                                log("[browser] JS form.requestSubmit() dispatched")
                            except Exception as exc:
                                log(f"[browser] JS submit failed: {exc}")
                    next_retry_at = time.monotonic() + 8.0
                elif not dom_logged:
                    # Hết retry — log DOM snapshot 1 lần để debug
                    dom_logged = True
                    await _log_about_you_dom(page, log=log)
            await asyncio.sleep(0.5)

        # Fallback: page.url nếu đã navigate qua callback hoặc chatgpt.com
        if "chatgpt.com" in page.url:
            return callback_holder.get("url") or page.url
        if "callback" in page.url and "code=" in page.url:
            return page.url

        raise BrowserPhaseError(f"timeout {timeout_seconds}s waiting callback URL. URL: {page.url}")
    finally:
        try:
            page.remove_listener("response", _on_resp)
        except Exception:
            pass


async def _wait_chatgpt_session(ctx, page, *, timeout_seconds: float, log) -> None:
    """Đợi cookie session-token xuất hiện trên chatgpt.com.

    STRATEGY (thay đổi 2026-05):
      - Yêu cầu cứng: `__Secure-next-auth.session-token` (hoặc chunk .0).
        Đây là cookie DUY NHẤT mà Phase 2 (http_phase) cần.
      - BỎ điều kiện `_account` — cookie này chỉ được set khi browser
        navigate top-level tới chatgpt.com, KHÔNG phải lúc nào cũng tự xảy ra
        sau callback (callback OAuth chạy qua fetch background, page có thể
        vẫn ở auth.openai.com/about-you). Yêu cầu `_account` từng gây
        timeout 60s waiting session-token mặc dù callback đã OK.
      - FALLBACK: sau ~8s không thấy session-token → chủ động
        page.goto("https://chatgpt.com/") để force browser load top-level
        (server sẽ set _account + commit cookies). Chỉ goto 1 lần.
      - Sau khi có session-token → return ngay (Phase 2 self-contained).
    """
    deadline = time.monotonic() + timeout_seconds
    fallback_goto_at = time.monotonic() + 8.0
    fallback_done = False
    last_log_at = 0.0
    while time.monotonic() < deadline:
        cookies = await ctx.cookies("https://chatgpt.com/")
        names = {c["name"] for c in cookies}
        has_session = (
            "__Secure-next-auth.session-token" in names
            or "__Secure-next-auth.session-token.0" in names
        )
        if has_session:
            has_account = "_account" in names
            log(
                f"[browser] chatgpt session ready "
                f"({len(cookies)} cookies, _account={has_account})"
            )
            await asyncio.sleep(0.3)
            return

        # Fallback: force navigate top-level để server commit cookies
        if not fallback_done and time.monotonic() > fallback_goto_at:
            fallback_done = True
            log(
                f"[browser] session-token chưa có sau 8s "
                f"(URL={page.url.split('?')[0]}) — force goto chatgpt.com"
            )
            try:
                await page.goto(
                    "https://chatgpt.com/",
                    wait_until="domcontentloaded",
                    timeout=20_000,
                )
                log(f"[browser] goto chatgpt.com done (URL={page.url.split('?')[0]})")
            except Exception as exc:
                log(
                    f"[browser] goto chatgpt.com failed "
                    f"({type(exc).__name__}: {exc}) — tiếp tục poll cookies"
                )

        # Log progress mỗi 5s để debug (không spam)
        now = time.monotonic()
        if now - last_log_at > 5.0:
            last_log_at = now
            chatgpt_names = sorted(n for n in names if not n.startswith("__cf"))[:8]
            log(
                f"[browser] still waiting session-token "
                f"(URL={page.url.split('?')[0]}, "
                f"{len(cookies)} cookies, top: {chatgpt_names})"
            )

        await asyncio.sleep(0.5)
    raise BrowserPhaseError(f"timeout {timeout_seconds}s waiting session-token. URL: {page.url}")


async def _wait_oai_sc(ctx, *, timeout_seconds: float, log) -> None:
    """Đợi cookie oai-sc (Sentinel SDK fired)."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        cookies = await ctx.cookies("https://auth.openai.com/")
        if any(c["name"] == "oai-sc" for c in cookies):
            log("[browser] sentinel cookie oai-sc ready")
            return
        await asyncio.sleep(0.5)
    raise BrowserPhaseError(f"timeout {timeout_seconds}s waiting oai-sc")



def _extract_state_from_authorize(url: str) -> str | None:
    """Parse state query param từ authorize URL."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    return qs["state"][0] if "state" in qs and qs["state"] else None


async def _extract_state_from_url(page, *, log) -> str | None:
    """Lấy state từ navigation history."""
    try:
        entries = await page.evaluate(
            "() => performance.getEntriesByType('navigation').concat(performance.getEntriesByType('resource'))"
            ".map(e => e.name).filter(u => u.includes('state='))"
        )
        for entry in entries or []:
            parsed = urlparse(entry)
            qs = parse_qs(parsed.query)
            if "state" in qs and qs["state"][0]:
                return qs["state"][0]
    except Exception as exc:
        log(f"[browser] state extract failed: {exc}")
    return None


# ─────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────

async def run_browser_phase(
    *,
    request: SignupRequest,
    settings: Settings,
    mail_provider: MailProvider,
    otp_started_at: datetime,
    log,
) -> tuple[BrowserHandoff, float]:
    """Phase 1: browser signup + set password post-login.

    Returns: (handoff, otp_seconds).
    """
    if request.tls_insecure:
        from .config import warn_insecure_tls
        warn_insecure_tls("browser_phase")
        log("[security] TLS verification DISABLED — debug mode")

    engine = settings.browser_engine or "chrome"
    job_id = f"hybrid_{uuid.uuid4().hex[:10]}"

    # Profile
    if engine == "camoufox":
        profile_dir = settings.profiles_dir / f"camoufox_{job_id}"
        template_dir = settings.browser_camoufox_profile_dir
    else:
        profile_dir = settings.profile_dir_for(job_id)
        template_dir = settings.browser_profile_template_dir

    ensure_runtime_dirs(settings, extra=(profile_dir,))
    prepare_profile_dir(
        profile_dir=profile_dir,
        template_dir=template_dir,
        use_template=request.profile_template,
    )

    # HAR capture
    har_kwargs: dict[str, Any] = {}
    if request.har_capture:
        har_dir = settings.runtime_dir / "har_hybrid"
        har_dir.mkdir(parents=True, exist_ok=True)
        har_path = har_dir / f"hybrid-{datetime.now():%Y%m%d-%H%M%S}-{job_id}.har"
        har_kwargs["record_har_path"] = str(har_path)
        har_kwargs["record_har_content"] = "embed"
        har_kwargs["record_har_mode"] = "full"
        log(f"[browser] HAR capture → {har_path}")

    device_id = str(uuid.uuid4())
    logging_id = str(uuid.uuid4())
    log(f"[browser] device_id={device_id} logging_id={logging_id}")

    w, h = settings.browser_viewport_width, settings.browser_viewport_height
    viewport = {"width": w, "height": h}

    proxy_kwargs: dict[str, Any] = {}
    if request.proxy:
        proxy_kwargs["proxy"] = _parse_proxy(request.proxy)
        _ensure_geoip_cache(settings.runtime_dir, log=log)

    state_param: str | None = None
    handoff_cookies: list[dict[str, Any]] = []
    authorize_url: str | None = None
    otp_seconds = 0.0
    callback_url: str | None = None

    # Track xem có đã chạm mốc OTP poll chưa. Sau mốc này, KHÔNG retry kể cả
    # khi driver chết — vì OTP đã được gửi, retry sẽ gây gửi OTP lần 2 và
    # consume mã không cần thiết.
    flow_progress = {"otp_started": False}

    def _mark_otp_started() -> None:
        flow_progress["otp_started"] = True

    # ─── Inner runners (mỗi runner là 1 lần launch + drive flow) ───
    async def _run_camoufox_once() -> tuple[str, float, str, list[dict[str, Any]]]:
        from camoufox.async_api import AsyncCamoufox

        extra_config: dict = {"fonts:spacing_seed": 0} if request.off_font else {}
        screen_kwargs: dict[str, Any] = {}

        if not settings.browser_random_screen:
            from camoufox.utils import Screen as _Screen

            chrome_h = 85
            extra_config["window.innerWidth"] = w
            extra_config["window.innerHeight"] = h
            extra_config["window.outerWidth"] = w
            extra_config["window.outerHeight"] = h + chrome_h
            extra_config["screen.width"] = w
            extra_config["screen.height"] = h + chrome_h
            extra_config["screen.availWidth"] = w
            extra_config["screen.availHeight"] = h + chrome_h
            screen_kwargs["screen"] = _Screen(
                min_width=w, max_width=w, min_height=h + chrome_h, max_height=h + chrome_h
            )
            screen_kwargs["i_know_what_im_doing"] = True

        cf = AsyncCamoufox(
            headless=request.headless,
            persistent_context=True,
            user_data_dir=str(profile_dir),
            viewport=viewport,
            locale="en-US",
            ignore_https_errors=request.tls_insecure,
            geoip=bool(request.proxy),
            config=extra_config,
            **screen_kwargs,
            **proxy_kwargs,
            **har_kwargs,
        )
        ctx = await cf.__aenter__()

        callback_holder: dict[str, str] = {}

        def _capture_callback(req) -> None:
            url = req.url
            if "chatgpt.com/api/auth/callback/openai" in url and "code=" in url:
                callback_holder.setdefault("url", url)

        ctx.on("request", _capture_callback)
        page = None
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()

            await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
            log("[browser] chatgpt.com loaded")
            _authorize_url = await _bootstrap_oauth_url(
                page, email=request.email, device_id=device_id, logging_id=logging_id, log=log,
            )
            await page.goto(_authorize_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(1.0)

            # Mốc check-point: sắp drive flow (sẽ trigger OTP send).
            _mark_otp_started()

            _callback_url, _otp_seconds = await _drive_signup_flow(
                ctx=ctx, page=page, request=request,
                mail_provider=mail_provider,
                callback_holder=callback_holder,
                otp_started_at=otp_started_at,
                log=log,
            )

            _state = (
                _extract_state_from_authorize(_authorize_url)
                or await _extract_state_from_url(page, log=log)
            )
            _cookies = await ctx.cookies()
            return _callback_url, _otp_seconds, _state or "", _cookies
        except BaseException as exc:
            # Plan D: log health snapshot trước khi propagate để debug được
            # lý do page/ctx/browser chết.
            try:
                health = _browser_health(ctx, page) if page is not None else "page=NEVER_CREATED"
            except Exception as health_exc:
                health = f"health-snapshot-failed: {type(health_exc).__name__}: {health_exc}"
            log(
                f"[browser] camoufox runner exception: "
                f"{type(exc).__name__}: {exc} ({health})"
            )
            raise
        finally:
            try:
                ctx.remove_listener("request", _capture_callback)
            except Exception:
                pass
            if request.keep_browser_open and not request.headless:
                log("[browser] debug: giữ browser mở — cancel job để đóng")
            else:
                try:
                    await cf.__aexit__(None, None, None)
                except Exception:
                    pass

    async def _run_chromium_once() -> tuple[str, float, str, list[dict[str, Any]]]:
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        ctx = None
        page = None
        try:
            channel = settings.browser_channel or None
            ctx = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=request.headless,
                channel=channel,
                viewport=viewport,
                locale="en-US",
                ignore_https_errors=request.tls_insecure,
                **proxy_kwargs,
                **har_kwargs,
            )

            callback_holder: dict[str, str] = {}

            def _capture_callback(req) -> None:
                url = req.url
                if "chatgpt.com/api/auth/callback/openai" in url and "code=" in url:
                    callback_holder.setdefault("url", url)

            ctx.on("request", _capture_callback)

            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
            log("[browser] chatgpt.com loaded")
            _authorize_url = await _bootstrap_oauth_url(
                page, email=request.email, device_id=device_id, logging_id=logging_id, log=log,
            )
            await page.goto(_authorize_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(1.0)

            # Mốc check-point: sắp drive flow (sẽ trigger OTP send).
            _mark_otp_started()

            _callback_url, _otp_seconds = await _drive_signup_flow(
                ctx=ctx, page=page, request=request,
                mail_provider=mail_provider,
                callback_holder=callback_holder,
                otp_started_at=otp_started_at,
                log=log,
            )

            _state = (
                _extract_state_from_authorize(_authorize_url)
                or await _extract_state_from_url(page, log=log)
            )
            _cookies = await ctx.cookies()

            if not (request.keep_browser_open and not request.headless):
                await ctx.close()
            return _callback_url, _otp_seconds, _state or "", _cookies
        except BaseException as exc:
            # Plan D: log health snapshot trước khi propagate.
            try:
                if ctx is None:
                    health = "ctx=NEVER_CREATED"
                elif page is None:
                    health = "page=NEVER_CREATED"
                else:
                    health = _browser_health(ctx, page)
            except Exception as health_exc:
                health = f"health-snapshot-failed: {type(health_exc).__name__}: {health_exc}"
            log(
                f"[browser] chromium runner exception: "
                f"{type(exc).__name__}: {exc} ({health})"
            )
            raise
        finally:
            if request.keep_browser_open and not request.headless:
                log("[browser] debug: giữ browser mở — cancel job để đóng")
            else:
                await playwright.stop()

    runner = _run_camoufox_once if engine == "camoufox" else _run_chromium_once

    # Vòng retry: chỉ retry khi bắt được lỗi driver-pipe-dead VÀ flow chưa
    # tới mốc OTP send. Sau OTP send, lỗi driver vẫn fail-fast để tránh
    # spam mã OTP cho user.
    last_exc: BaseException | None = None
    success = False
    # B11 fix: try/finally đảm bảo profile_dir được dọn trên mọi exit path
    # (BrowserPhaseError raise giữa loop, CancelledError, KeyboardInterrupt).
    # Trừ debug mode keep_browser_open + headed (giữ profile để soi).
    try:
        for attempt in range(1, _LAUNCH_RETRY_MAX + 1):
            flow_progress["otp_started"] = False
            try:
                callback_url, otp_seconds, state_param, handoff_cookies = await runner()
                success = True
                last_exc = None
                break
            except BrowserPhaseError:
                raise
            except Exception as exc:
                last_exc = exc
                retryable = (
                    _is_driver_dead_error(exc)
                    or _is_network_error(exc)
                    or _is_navigation_timeout(exc)
                )
                if not retryable:
                    raise BrowserPhaseError(
                        f"browser launch/driver error: {type(exc).__name__}: {exc}"
                    ) from exc
                if flow_progress["otp_started"]:
                    log(
                        f"[browser] lỗi sau khi đã trigger OTP — "
                        f"không retry để tránh gửi OTP lần 2: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    raise BrowserPhaseError(
                        f"lỗi giữa flow (OTP đã gửi, không retry): {exc}"
                    ) from exc
                err_kind = (
                    "network/proxy" if _is_network_error(exc)
                    else "navigation timeout" if _is_navigation_timeout(exc)
                    else "driver pipe"
                )
                log(
                    f"[browser] {err_kind} error "
                    f"(attempt {attempt}/{_LAUNCH_RETRY_MAX}): "
                    f"{type(exc).__name__}: {exc}"
                )
                if attempt >= _LAUNCH_RETRY_MAX:
                    break
                shutil.rmtree(profile_dir, ignore_errors=True)
                prepare_profile_dir(
                    profile_dir=profile_dir,
                    template_dir=template_dir,
                    use_template=request.profile_template,
                )
                await asyncio.sleep(_LAUNCH_RETRY_BACKOFF)
    finally:
        if not (request.keep_browser_open and not request.headless):
            shutil.rmtree(profile_dir, ignore_errors=True)

    if not success:
        if last_exc is not None and (
            _is_driver_dead_error(last_exc)
            or _is_network_error(last_exc)
            or _is_navigation_timeout(last_exc)
        ):
            raise BrowserPhaseError(
                f"retryable error sau {_LAUNCH_RETRY_MAX} lần thử: {last_exc}"
            ) from last_exc
        # Defensive: không bao giờ xảy ra (đã raise trong loop)
        raise BrowserPhaseError("browser launch failed without specific error")

    if not state_param:
        raise BrowserPhaseError("không lấy được oauth state từ navigation history")

    # Sanity check required cookies
    auth_cookies = {c["name"] for c in handoff_cookies if "openai.com" in (c.get("domain") or "")}
    missing = [c for c in _REQUIRED_AUTH_COOKIES if c not in auth_cookies]
    if missing:
        raise BrowserPhaseError(f"thiếu cookies: {missing}. có: {sorted(auth_cookies)}")

    log(f"[browser] handoff: {len(handoff_cookies)} cookies, state={state_param[:20]}...")
    return (
        BrowserHandoff(
            cookies=handoff_cookies,
            state_param=state_param,
            device_id=device_id,
            auth_session_logging_id=logging_id,
            callback_url=callback_url,
        ),
        otp_seconds,
    )
