"""Get Session: Login ChatGPT bằng browser + password + 2FA → trả full session JSON.

Dùng browser thật vì auth.openai.com có Cloudflare JS challenge —
curl_cffi không bypass được.

Flow:
    1. Mở chatgpt.com → bootstrap NextAuth (csrf + signin/openai) → authorize URL
    2. Navigate authorize → /log-in/password
    3. Fill password → submit
    4. Nếu MFA → fill TOTP code → submit
    5. Đợi redirect chatgpt.com + session cookies
    6. Gọi /api/auth/session trong page context → return JSON
"""
from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from typing import Any, Callable

from ._browser_retry import (
    LAUNCH_RETRY_BACKOFF as _LAUNCH_RETRY_BACKOFF,
    LAUNCH_RETRY_MAX as _LAUNCH_RETRY_MAX,
    is_driver_dead_error as _is_driver_dead_error,
    is_network_error as _is_network_error,
    parse_proxy_for_playwright as _parse_proxy,
)
from ._nextauth_bootstrap import bootstrap_authorize_url
from .config import ensure_runtime_dirs, load_settings, prepare_profile_dir
from .totp_helper import generate_code


LogFn = Callable[[str], None]


class SessionError(Exception):
    """Login/session fetch failed."""


# JS: fetch /api/auth/session trong page context chatgpt.com
_FETCH_SESSION_JS = r"""
async () => {
    const r = await fetch('/api/auth/session', {credentials: 'include'});
    if (!r.ok) throw new Error('session HTTP ' + r.status);
    return await r.json();
}
"""


async def _get_session_browser(
    *,
    email: str,
    password: str,
    secret: str | None = None,
    headless: bool = True,
    proxy: str | None = None,
    tls_insecure: bool = False,
    keep_browser_open: bool = False,
    log: LogFn = print,
) -> dict[str, Any]:
    """Login ChatGPT bằng browser thật → return session JSON.

    Retry: nếu driver pipe đóng sớm TRƯỚC khi submit password → relaunch.
    Sau khi đã submit password thì fail-fast để tránh nhiều lần thử login
    (rủi ro lockout / captcha challenge).

    keep_browser_open=True + headless=False → giữ browser mở sau khi xong
    (debug). User cancel job để đóng. Có tác dụng cả ở exit path lỗi
    sau-submit-password (để soi DOM/network).
    """
    debug_keep = keep_browser_open and not headless
    if tls_insecure:
        from .config import warn_insecure_tls
        warn_insecure_tls("session_phase")
        log("[security] TLS verification DISABLED — debug mode")

    settings = load_settings()
    job_id = f"session_{uuid.uuid4().hex[:10]}"
    preferred_engine = (
        "camoufox"
        if (settings.browser_engine or "camoufox").lower() == "camoufox"
        else "chromium"
    )
    engine_order = [preferred_engine]
    if preferred_engine == "camoufox":
        engine_order.append("chromium")
    w, h = settings.browser_viewport_width, settings.browser_viewport_height
    viewport = {"width": w, "height": h}
    proxy_kwargs: dict[str, Any] = {}
    if proxy:
        proxy_kwargs["proxy"] = _parse_proxy(proxy)
        from .browser_phase import _ensure_geoip_cache
        _ensure_geoip_cache(settings.runtime_dir, log=log)

    progress = {"password_submitted": False}

    def _profile_bundle(engine: str) -> tuple[Any, Any]:
        if engine == "camoufox":
            return (
                settings.profiles_dir / f"camoufox_{job_id}",
                settings.browser_camoufox_profile_dir,
            )
        return (
            settings.profile_dir_for(job_id),
            settings.browser_profile_template_dir,
        )

    async def _drive_session_flow(ctx: Any, page: Any) -> dict[str, Any]:
        device_id = str(uuid.uuid4())
        logging_id = str(uuid.uuid4())

        # Step 1: bootstrap
        log("[session] loading chatgpt.com...")
        await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
        log("[session] bootstrapping NextAuth...")
        authorize_url = await bootstrap_authorize_url(
            page,
            email=email,
            device_id=device_id,
            logging_id=logging_id,
        )
        log("[session] authorize URL ready")

        # Step 2: navigate authorize → login page
        await page.goto(authorize_url, wait_until="domcontentloaded")
        await asyncio.sleep(3.0)
        log(f"[session] at: {page.url.split('?')[0]}")

        # Có thể ở /log-in (email step) → cần fill email trước
        # Hoặc ở /log-in/password → fill password luôn
        if "/log-in/password" not in page.url:
            email_input = None
            for sel in (
                'input[name="email"]',
                'input[type="email"]',
                'input[inputmode="email"]',
            ):
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=8000):
                        email_input = loc
                        break
                except Exception:
                    continue

            if email_input:
                log("[session] filling email...")
                await email_input.click(force=True, timeout=3000)
                await email_input.fill("")
                await email_input.type(email, delay=30)
                await asyncio.sleep(0.3)
                for btn_sel in (
                    'button[type="submit"]',
                    'button:has-text("Continue")',
                ):
                    try:
                        await page.click(btn_sel, timeout=3000)
                        log(f"[session] submitted email ({btn_sel})")
                        break
                    except Exception:
                        continue
                await asyncio.sleep(3.0)
                log(f"[session] after email: {page.url.split('?')[0]}")

        # Step 3: fill password
        pwd_input = None
        for sel in ('input[type="password"]', 'input[name="password"]'):
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=15000):
                    pwd_input = loc
                    break
            except Exception:
                continue

        if not pwd_input:
            raise SessionError(f"password input not found. URL: {page.url}")

        log("[session] filling password...")
        await pwd_input.click(force=True, timeout=3000)
        await pwd_input.fill("")
        await pwd_input.type(password, delay=40)
        await asyncio.sleep(0.3)

        # Submit password — sau đây không retry để tránh login spam
        for btn_sel in (
            'button[type="submit"]',
            'button:has-text("Continue")',
            'button:has-text("Log in")',
        ):
            try:
                await page.click(btn_sel, timeout=3000)
                log(f"[session] clicked {btn_sel}")
                break
            except Exception:
                continue
        progress["password_submitted"] = True

        # Step 4: poll chờ terminal state sau submit password
        # Terminal states:
        #   - MFA: URL chứa "mfa" hoặc content chứa "mfa"
        #   - Logged in: URL chứa "chatgpt.com" (không còn auth.openai.com)
        #   - Login error: trang vẫn ở /log-in/password + có error message
        _POST_PASSWORD_DEADLINE = 20.0  # đủ cho proxy chậm
        _POLL_INTERVAL = 1.0
        poll_end = time.monotonic() + _POST_PASSWORD_DEADLINE
        terminal_state: str | None = None  # "mfa" | "logged_in" | "login_error"

        while time.monotonic() < poll_end:
            await asyncio.sleep(_POLL_INTERVAL)
            current_url = page.url.lower()

            # Terminal: đã redirect về chatgpt.com (no MFA account hoặc MFA đã xong)
            if "chatgpt.com" in current_url and "auth.openai.com" not in current_url:
                terminal_state = "logged_in"
                log(f"[session] after password: redirected to chatgpt.com")
                break

            # Terminal: MFA challenge page
            if "mfa" in current_url:
                terminal_state = "mfa"
                log(f"[session] after password: MFA page detected (URL)")
                break

            # Check content cho trường hợp URL không chứa "mfa" nhưng page content có
            try:
                content_snippet = (await page.content())[:5000].lower()
            except Exception:
                content_snippet = ""

            if "mfa" in content_snippet:
                terminal_state = "mfa"
                log(f"[session] after password: MFA page detected (content)")
                break

            # Terminal: login error (vẫn ở password page + có thông báo lỗi)
            if "/log-in/password" in current_url:
                _error_selectors = (
                    '[data-testid="error-message"]',
                    '.error-message',
                    '[role="alert"]',
                )
                for err_sel in _error_selectors:
                    try:
                        if await page.locator(err_sel).first.is_visible(timeout=300):
                            terminal_state = "login_error"
                            break
                    except Exception:
                        continue
                if terminal_state == "login_error":
                    err_text = ""
                    try:
                        err_text = await page.locator(err_sel).first.inner_text(timeout=1000)
                    except Exception:
                        pass
                    raise SessionError(
                        f"login failed (password error): {err_text or 'unknown'}. "
                        f"URL: {page.url}"
                    )

        if terminal_state is None:
            # Deadline hết mà chưa đạt terminal state → check lần cuối
            current_url = page.url.lower()
            if "mfa" in current_url:
                terminal_state = "mfa"
            elif "chatgpt.com" in current_url and "auth.openai.com" not in current_url:
                terminal_state = "logged_in"
            else:
                raise SessionError(
                    f"timeout waiting for post-password redirect "
                    f"({_POST_PASSWORD_DEADLINE}s). URL: {page.url}"
                )

        log(f"[session] post-password state: {terminal_state}")

        # Handle MFA
        if terminal_state == "mfa":
            if not secret:
                raise SessionError("account yêu cầu 2FA nhưng không có secret")

            log("[session] generating TOTP...")
            code = generate_code(secret)

            otp_input = None
            for sel in (
                'input[name="code"]',
                'input[inputmode="numeric"]',
                'input[autocomplete="one-time-code"]',
                'input[maxlength="6"]',
            ):
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=8000):
                        otp_input = loc
                        break
                except Exception:
                    continue

            if not otp_input:
                raise SessionError(f"TOTP input not found. URL: {page.url}")

            await otp_input.click(force=True, timeout=3000)
            await otp_input.fill("")
            await otp_input.type(code, delay=60)
            log(f"[session] TOTP code entered")
            await asyncio.sleep(0.5)

            for btn_sel in (
                'button[type="submit"]',
                'button:has-text("Continue")',
                'button:has-text("Verify")',
            ):
                try:
                    await page.click(btn_sel, timeout=3000)
                    log(f"[session] clicked {btn_sel}")
                    break
                except Exception:
                    continue

            await asyncio.sleep(3.0)
            log(f"[session] after MFA submit: {page.url.split('?')[0]}")

        # Step 5: đợi redirect về chatgpt.com + session cookies
        deadline = time.monotonic() + 30.0
        session_ready = False
        while time.monotonic() < deadline:
            cookies = await ctx.cookies("https://chatgpt.com/")
            names = {c["name"] for c in cookies}
            has_session = (
                "__Secure-next-auth.session-token" in names
                or "__Secure-next-auth.session-token.0" in names
            )
            if has_session:
                log("[session] session cookies ready")
                session_ready = True
                break
            if "chatgpt.com" in page.url and "auth.openai.com" not in page.url:
                await asyncio.sleep(1.0)
                continue
            await asyncio.sleep(1.0)
        if not session_ready:
            raise SessionError(f"timeout waiting session cookies. URL: {page.url}")

        # Đảm bảo đang ở chatgpt.com
        if "chatgpt.com" not in page.url:
            await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
            await asyncio.sleep(2.0)

        # Step 6: fetch session JSON
        log("[session] fetching /api/auth/session...")
        session_data = await page.evaluate(_FETCH_SESSION_JS)
        if not isinstance(session_data, dict) or not session_data.get("accessToken"):
            raise SessionError(
                f"session response invalid: {str(session_data)[:200]}"
            )

        log(
            f"[session] ✓ done — user: "
            f"{session_data.get('user', {}).get('email', '?')}"
        )
        return session_data

    async def _run_camoufox_once(profile_dir: Any) -> dict[str, Any]:
        from camoufox.async_api import AsyncCamoufox

        extra_config: dict = {}
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
                min_width=w, max_width=w, min_height=h + chrome_h, max_height=h + chrome_h,
            )
            screen_kwargs["i_know_what_im_doing"] = True

        cf = AsyncCamoufox(
            headless=headless,
            persistent_context=True,
            user_data_dir=str(profile_dir),
            viewport=viewport,
            locale="en-US",
            ignore_https_errors=tls_insecure,
            geoip=bool(proxy),
            config=extra_config,
            **screen_kwargs,
            **proxy_kwargs,
        )
        ctx = await cf.__aenter__()
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            return await _drive_session_flow(ctx, page)
        finally:
            if debug_keep:
                log("[session] debug: giữ browser mở — cancel job để đóng")
            else:
                try:
                    await cf.__aexit__(None, None, None)
                except Exception:
                    pass

    async def _run_chromium_once(profile_dir: Any) -> dict[str, Any]:
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        try:
            channel = settings.browser_channel or None
            ctx = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=headless,
                channel=channel,
                viewport=viewport,
                locale="en-US",
                ignore_https_errors=tls_insecure,
                **proxy_kwargs,
            )
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                return await _drive_session_flow(ctx, page)
            finally:
                if debug_keep:
                    log("[session] debug: giữ browser mở — cancel job để đóng")
                else:
                    try:
                        await ctx.close()
                    except Exception:
                        pass
        finally:
            if not debug_keep:
                await playwright.stop()

    runners = {
        "camoufox": _run_camoufox_once,
        "chromium": _run_chromium_once,
    }
    last_exc: BaseException | None = None
    try:
        for engine_index, engine in enumerate(engine_order):
            profile_dir, template_dir = _profile_bundle(engine)
            ensure_runtime_dirs(settings, extra=(profile_dir,))
            prepare_profile_dir(
                profile_dir=profile_dir,
                template_dir=template_dir,
                use_template=settings.browser_use_profile_template,
            )
            if engine_index > 0:
                log(f"[session] fallback browser engine: {engine}")

            for attempt in range(1, _LAUNCH_RETRY_MAX + 1):
                progress["password_submitted"] = False
                try:
                    return await runners[engine](profile_dir)
                except SessionError:
                    raise
                except Exception as exc:
                    last_exc = exc
                    retryable = _is_driver_dead_error(exc) or _is_network_error(exc)
                    if not retryable:
                        raise SessionError(
                            f"browser launch/driver error: {type(exc).__name__}: {exc}"
                        ) from exc
                    if progress["password_submitted"]:
                        log(
                            f"[session] lỗi sau khi đã submit password — "
                            f"không retry để tránh login spam: "
                            f"{type(exc).__name__}: {exc}"
                        )
                        raise SessionError(
                            f"lỗi sau submit password (không retry): {exc}"
                        ) from exc
                    err_kind = "network/proxy" if _is_network_error(exc) else "driver pipe"
                    log(
                        f"[session] {err_kind} error "
                        f"(attempt {attempt}/{_LAUNCH_RETRY_MAX}): "
                        f"{type(exc).__name__}: {exc}"
                    )
                    if attempt >= _LAUNCH_RETRY_MAX:
                        break
                    shutil.rmtree(profile_dir, ignore_errors=True)
                    prepare_profile_dir(
                        profile_dir=profile_dir,
                        template_dir=template_dir,
                        use_template=settings.browser_use_profile_template,
                    )
                    await asyncio.sleep(_LAUNCH_RETRY_BACKOFF)

            if engine == "camoufox" and engine_index + 1 < len(engine_order):
                log(
                    "[session] camoufox chết sớm ở auth redirect — "
                    "thử fallback sang chromium"
                )
                continue

            if last_exc is not None and (_is_driver_dead_error(last_exc) or _is_network_error(last_exc)):
                raise SessionError(
                    f"retryable error sau {_LAUNCH_RETRY_MAX} lần thử: {last_exc}"
                ) from last_exc

        raise SessionError("browser launch failed without specific error")
    finally:
        # Debug mode + headed → giữ profile để soi (không cleanup).
        # Fail mode (raise) trên 1 engine vẫn cần dọn profile của engine đó
        # nếu không giữ browser mở.
        if not debug_keep:
            for engine in engine_order:
                profile_dir, _ = _profile_bundle(engine)
                shutil.rmtree(profile_dir, ignore_errors=True)


async def get_session(
    *,
    email: str,
    password: str,
    secret: str | None = None,
    headless: bool = True,
    proxy: str | None = None,
    tls_insecure: bool = False,
    keep_browser_open: bool = False,
    log: LogFn = print,
) -> dict[str, Any]:
    """Async: login ChatGPT → return full /api/auth/session JSON."""
    return await _get_session_browser(
        email=email,
        password=password,
        secret=secret,
        headless=headless,
        proxy=proxy,
        tls_insecure=tls_insecure,
        keep_browser_open=keep_browser_open,
        log=log,
    )


def get_session_sync(
    *,
    email: str,
    password: str,
    secret: str | None = None,
    headless: bool = True,
    proxy: str | None = None,
    tls_insecure: bool = False,
    keep_browser_open: bool = False,
    log: LogFn = print,
) -> dict[str, Any]:
    """Sync wrapper."""
    return asyncio.run(get_session(
        email=email,
        password=password,
        secret=secret,
        headless=headless,
        proxy=proxy,
        tls_insecure=tls_insecure,
        keep_browser_open=keep_browser_open,
        log=log,
    ))


# ─────────────────────────────────────────────────────────────────────
# HTTP-only session fetch (no browser) — dùng khi đã có cookies sẵn từ Phase 2.
# ─────────────────────────────────────────────────────────────────────


def _cookies_to_header(cookies: Any) -> str:
    """Convert cookies (list[dict] | dict | None) → "name=value; name=value" string.

    Hỗ trợ 2 format:
      - list[dict]: Playwright/SignupResult format [{"name":..., "value":..., "domain":...}, ...]
        → chỉ giữ cookies thuộc domain chatgpt.com (hoặc rỗng).
      - dict: {name: value} flat.
    """
    if not cookies:
        return ""
    pairs: list[str] = []
    if isinstance(cookies, list):
        for c in cookies:
            if not isinstance(c, dict):
                continue
            name = c.get("name")
            value = c.get("value")
            if not name or value is None:
                continue
            domain = (c.get("domain") or "").lstrip(".").lower()
            # Chỉ giữ cookies dùng được cho chatgpt.com
            if domain and "chatgpt.com" not in domain:
                continue
            pairs.append(f"{name}={value}")
    elif isinstance(cookies, dict):
        for name, value in cookies.items():
            if value is None:
                continue
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)


async def fetch_session_via_http(
    *,
    cookies: Any,
    proxy: str | None = None,
    timeout: float = 30.0,
    impersonate: str = "chrome136",
) -> dict[str, Any]:
    """GET https://chatgpt.com/api/auth/session bằng curl_cffi với cookies có sẵn.

    Args:
        cookies: list[dict] (Playwright format) hoặc dict {name: value}.
        proxy: HTTP/HTTPS proxy URL.
        timeout: Request timeout (seconds).
        impersonate: curl_cffi browser impersonation key.

    Returns:
        Full session JSON (dict) với accessToken không rỗng.

    Raises:
        SessionError: HTTP non-200, JSON parse fail, hoặc accessToken thiếu/rỗng.
    """
    from curl_cffi.requests import AsyncSession

    cookie_header = _cookies_to_header(cookies)
    if not cookie_header:
        raise SessionError("không có cookie chatgpt.com để fetch session")

    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {
        "Cookie": cookie_header,
        "Accept": "application/json",
        "Referer": "https://chatgpt.com/",
    }

    async with AsyncSession(impersonate=impersonate, proxies=proxies) as sess:
        try:
            resp = await sess.get(
                "https://chatgpt.com/api/auth/session",
                headers=headers,
                timeout=timeout,
            )
        except Exception as exc:
            raise SessionError(f"network error: {exc}") from exc

    if resp.status_code != 200:
        body = (resp.text or "")[:200]
        raise SessionError(f"HTTP {resp.status_code}: {body}")

    try:
        data = resp.json()
    except Exception as exc:
        raise SessionError(f"JSON parse fail: {exc}") from exc

    if not isinstance(data, dict):
        raise SessionError(f"response không phải JSON object: {type(data).__name__}")

    token = data.get("accessToken")
    if not isinstance(token, str) or not token.strip():
        raise SessionError("accessToken thiếu hoặc rỗng")

    return data


# ─────────────────────────────────────────────────────────────────────
# Pure-request login (no browser) — full protocol for existing accounts
# ─────────────────────────────────────────────────────────────────────


async def get_session_pure_request(
    *,
    email: str,
    password: str,
    secret: str | None = None,
    proxy: str | None = None,
    mail_provider: Any = None,
    log: LogFn = print,
) -> dict[str, Any]:
    """Login ChatGPT via pure HTTP requests → return /api/auth/session JSON.

    Protocol flow (no browser):
      1. chatgpt.com CSRF + signin/openai → authorize URL
      2. OAuth init → device_id
      3. Sentinel token
      4. authorize/continue (email)
      5. IF passwordless → OTP verify (requires mail_provider)
         IF password → password/verify
      6. MFA verify (if needed, using TOTP secret)
      7. Follow redirect chain → callback
      8. Consume callback → session_token
      9. GET /api/auth/session → full JSON
    """
    from .request_phase import (
        _create_session,
        _step_csrf,
        _step_auth_url,
        _get_sentinel_token,
        _common_headers,
        _step_authorize_continue,
        _step_follow_redirects,
        _consume_callback,
        _get_session_tokens,
        _step_resend_otp,
        _step_verify_otp,
        _is_tls_error,
        _IMPERSONATE_CANDIDATES,
        RequestPhaseError,
        USER_AGENT,
    )
    from .totp_helper import generate_code as _generate_totp
    from urllib.parse import urljoin
    import asyncio as _asyncio
    from datetime import datetime, timezone

    def _run_async_poll(provider, recipient, started_at, log) -> str:
        """Poll OTP from async mail provider inside a sync thread (new event loop)."""
        loop = _asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                provider.poll_otp(
                    recipient=recipient,
                    started_at=started_at,
                    timeout_seconds=180.0,
                    poll_interval_seconds=4.0,
                    log=log,
                )
            )
        finally:
            loop.close()

    def _sync() -> dict[str, Any]:
        # Bootstrap (CSRF + signin/openai + OAuth init) with TLS rotation.
        session = None
        device_id = ""
        auth_url = ""
        landing = ""
        _last_exc = None
        for _idx, _imp in enumerate(_IMPERSONATE_CANDIDATES):
            session = _create_session(proxy=proxy, impersonate=_imp)
            try:
                if _idx > 0:
                    log(f"[session-req] TLS rotation: retrying with impersonate={_imp}")
                device_id = str(__import__('uuid').uuid4())
                csrf = _step_csrf(session, log)
                auth_url = _step_auth_url(session, csrf, log, device_id=device_id, login_hint=email)
                # OAuth init: GET authorize MIMIC top-level navigation của browser
                # (sec-fetch-* + upgrade-insecure-requests + referer chatgpt.com/).
                # Đây là khác biệt KEY so với request_phase: browser auth.openai.com
                # bot-detect signature header — GET authorize KHÔNG có
                # sec-fetch-mode=navigate sẽ bị xử như XHR và trả thẳng SPA 200
                # thay vì 302 sang /log-in/password (xác minh qua HAR thật, RID 424).
                _r_init = session.get(
                    auth_url,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.5",
                        "Accept-Encoding": "gzip, deflate, br, zstd",
                        "Referer": "https://chatgpt.com/",
                        "Connection": "keep-alive",
                        "Upgrade-Insecure-Requests": "1",
                        "Sec-Fetch-Dest": "document",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Site": "cross-site",
                        "Sec-Fetch-User": "?1",
                        "User-Agent": USER_AGENT,
                    },
                    timeout=30,
                    allow_redirects=True,
                )
                landing = str(getattr(_r_init, "url", "") or auth_url)
                device_id = session.cookies.get("oai-did", "") or device_id
                break
            except Exception as _e:
                _last_exc = _e
                try:
                    session.close()
                except Exception:
                    pass
                if _is_tls_error(_e) and _idx < len(_IMPERSONATE_CANDIDATES) - 1:
                    continue
                raise
        if not auth_url:
            if _last_exc:
                raise _last_exc
            raise SessionError("bootstrap failed (no auth_url)")

        try:
            log(f"[session-req] landing: {landing[:90]!r}")

            page_type = ""
            continue_url = ""

            # Resolve login flow. oauth_init ở trên đã theo hết redirect chain:
            # - Nếu server đưa thẳng tới trang login → landing cho biết luôn.
            # - Nếu server trả trang authorize/SPA (không redirect) → drive state
            #   machine qua authorize/continue (state đã được oauth_init thiết lập)
            #   để lấy page.type. KHÔNG đoán mò theo URL, KHÔNG retry ở ngọn.
            flow = ""
            if "/log-in/password" in landing:
                flow = "password"
            elif "/email-verification" in landing:
                flow = "otp"
            else:
                log("[session-req] authorize không tự redirect — resolve qua authorize/continue...")
                ac_sentinel = _get_sentinel_token(session, device_id, "login", log)
                ac_data = _step_authorize_continue(
                    session,
                    email,
                    ac_sentinel,
                    screen_hint="login",
                    referer="https://auth.openai.com/log-in",
                    device_id=device_id,
                    log=log,
                )
                ac_page = ac_data.get("page", {}) if isinstance(ac_data, dict) else {}
                page_type = (ac_page.get("type") or "").strip()
                continue_url = (ac_data.get("continue_url") or "").strip()
                log(f"[session-req] authorize/continue → page_type={page_type!r} continue_url={continue_url[:80]!r}")
                if page_type == "login_password" or "/log-in/password" in continue_url:
                    flow = "password"
                elif page_type in ("email_otp_verification", "email_verification") or "/email-verification" in continue_url:
                    flow = "otp"
                else:
                    raise SessionError(
                        f"unexpected login state: page_type={page_type!r} landing={landing[:80]!r}"
                    )

            # ── Branch A: password login ──
            if flow == "password":
                log("[session-req] password login flow...")
                headers = _common_headers("https://auth.openai.com/log-in/password")
                headers["Content-Type"] = "application/json"
                if device_id:
                    headers["oai-device-id"] = device_id
                sentinel_pw = _get_sentinel_token(session, device_id, "login", log)
                if sentinel_pw:
                    headers["openai-sentinel-token"] = sentinel_pw

                resp = session.post(
                    "https://auth.openai.com/api/accounts/password/verify",
                    headers=headers,
                    json={"password": password},
                    timeout=30,
                )
                if resp.status_code != 200:
                    body = (resp.text or "")[:300]
                    raise SessionError(f"password verify failed: HTTP {resp.status_code} - {body}")
                log("[session-req] password verified")
                pw_data = resp.json() if resp is not None else {}
                page_type = ((pw_data.get("page") or {}).get("type") or "").strip()
                continue_url = (pw_data.get("continue_url") or "").strip()
                log(f"[session-req] post-password → page_type={page_type!r} continue_url={continue_url[:80]!r}")

            # ── Branch B: passwordless OTP login ──
            elif flow == "otp":
                if mail_provider is None:
                    raise SessionError(
                        "account uses passwordless OTP login but no mail_provider available. "
                        "Use an Outlook/mail combo so OTP can be polled, or use browser mode."
                    )
                log("[session-req] passwordless OTP login flow...")
                # Trigger OTP send
                otp_send_headers = _common_headers("https://auth.openai.com/email-verification")
                if device_id:
                    otp_send_headers["oai-device-id"] = device_id
                session.get(
                    "https://auth.openai.com/api/accounts/email-otp/send",
                    headers=otp_send_headers,
                    timeout=30,
                )
                log("[session-req] OTP sent, polling mail provider...")

                # Poll OTP via mail provider (sync bridge)
                otp_started = datetime.now(timezone.utc)
                otp_code = _run_async_poll(
                    mail_provider, email, otp_started, log,
                )
                if not otp_code:
                    raise SessionError("OTP polling returned empty code")

                otp_resp = _step_verify_otp(session, otp_code, device_id, log)
                page_type = ((otp_resp.get("page") or {}).get("type") or "").strip()
                continue_url = (otp_resp.get("continue_url") or "").strip()
                log(f"[session-req] post-OTP → page_type={page_type!r} continue_url={continue_url[:80]!r}")

            # Step 6: MFA (if needed)
            if "mfa" in page_type or "mfa" in (continue_url or ""):
                if not secret:
                    raise SessionError("account requires 2FA but no secret provided")
                log("[session-req] MFA challenge detected...")

                # Extract challenge ID from continue_url
                # e.g. https://auth.openai.com/mfa-challenge/6a2f85296d588191...
                import re as _re
                challenge_id = ""
                _m = _re.search(r"/mfa-challenge/([a-f0-9]+)", continue_url or "")
                if _m:
                    challenge_id = _m.group(1)

                if not challenge_id:
                    raise SessionError(f"MFA challenge ID not found in continue_url: {continue_url[:100]}")

                mfa_headers = _common_headers("https://auth.openai.com/mfa-challenge")
                mfa_headers["Content-Type"] = "application/json"
                if device_id:
                    mfa_headers["oai-device-id"] = device_id

                # Step 6a: Issue challenge
                log("[session-req] issuing MFA challenge...")
                resp = session.post(
                    "https://auth.openai.com/api/accounts/mfa/issue_challenge",
                    headers=mfa_headers,
                    json={"id": challenge_id, "type": "totp", "force_fresh_challenge": False},
                    timeout=30,
                )
                if resp.status_code != 200:
                    log(f"[session-req] issue_challenge returned {resp.status_code}: {(resp.text or '')[:200]}")
                    # Non-fatal: some accounts may not need issue_challenge

                # Step 6b: Verify TOTP
                code = _generate_totp(secret)
                log("[session-req] verifying TOTP...")
                resp = session.post(
                    "https://auth.openai.com/api/accounts/mfa/verify",
                    headers=mfa_headers,
                    json={"id": challenge_id, "type": "totp", "code": code},
                    timeout=30,
                )
                if resp.status_code != 200:
                    body = (resp.text or "")[:300]
                    raise SessionError(f"MFA verify failed: HTTP {resp.status_code} - {body}")

                mfa_data = resp.json() if resp is not None else {}
                log("[session-req] MFA verified!")
                continue_url = (mfa_data.get("continue_url") or "").strip()

            # Normalize continue_url
            if continue_url and continue_url.startswith("/"):
                continue_url = urljoin("https://auth.openai.com", continue_url)

            # Step 7-8: Follow redirects + consume callback
            # If continue_url points to auth.openai.com (not a callback), we need to
            # reauthorize to get the actual callback with code parameter.
            if continue_url and "auth.openai.com" in continue_url and "code=" not in continue_url:
                log("[session-req] continue_url is auth page, attempting reauthorize for callback...")
                try:
                    csrf2 = _step_csrf(session, log)
                    auth_url2 = _step_auth_url(session, csrf2, log)
                    # Follow authorize URL (should redirect to callback since we're now authenticated)
                    callback_url, _ = _step_follow_redirects(session, auth_url2, log)
                    if callback_url:
                        _consume_callback(session, callback_url, log)
                except Exception as e:
                    log(f"[session-req] reauthorize attempt failed: {e}")

            elif continue_url:
                _t_cb = time.monotonic()
                callback_url, _ = _step_follow_redirects(session, continue_url, log)
                log(f"[session-req] follow_redirects {time.monotonic() - _t_cb:.2f}s")
                if callback_url:
                    _t_cc = time.monotonic()
                    _consume_callback(session, callback_url, log)
                    log(f"[session-req] consume_callback {time.monotonic() - _t_cc:.2f}s")

            elif not continue_url:
                # Try reauthorize (session cookie may already be set)
                log("[session-req] no continue_url, attempting reauthorize...")
                try:
                    csrf2 = _step_csrf(session, log)
                    auth_url2 = _step_auth_url(session, csrf2, log)
                    resp = session.get(
                        auth_url2,
                        headers={
                            "Accept": "text/html,*/*",
                            "Referer": "https://chatgpt.com/",
                            "User-Agent": USER_AGENT,
                        },
                        timeout=30,
                        allow_redirects=False,
                    )
                    loc = (resp.headers.get("Location") or "").strip()
                    if loc:
                        redir_url = loc if loc.startswith("http") else urljoin(auth_url2, loc)
                        callback_url, _ = _step_follow_redirects(session, redir_url, log)
                        if callback_url:
                            _consume_callback(session, callback_url, log)
                except Exception as e:
                    log(f"[session-req] reauthorize failed: {e}")

            # Step 9: Get FULL /api/auth/session JSON (same as browser mode)
            _t_sess = time.monotonic()
            sess_headers = _common_headers("https://chatgpt.com/")
            sess_resp = session.get(
                "https://chatgpt.com/api/auth/session",
                headers=sess_headers,
                timeout=30,
            )
            log(f"[session-req] GET /api/auth/session {time.monotonic() - _t_sess:.2f}s")
            if sess_resp.status_code != 200:
                raise SessionError(
                    f"/api/auth/session failed: HTTP {sess_resp.status_code} - {(sess_resp.text or '')[:200]}"
                )
            try:
                session_data = sess_resp.json()
            except Exception as e:
                raise SessionError(f"/api/auth/session JSON parse failed: {e}")

            if not isinstance(session_data, dict) or not session_data.get("accessToken"):
                raise SessionError(
                    f"login completed but /api/auth/session has no accessToken: {str(session_data)[:200]}"
                )

            user_email = (session_data.get("user", {}) or {}).get("email", "") or email
            log(f"[session-req] ✓ done — user: {user_email}")
            # Capture cookies cho hybrid flow (caller có thể inject vào browser).
            # curl_cffi expose cookies qua jar (.cookies) — list các Cookie object.
            try:
                cookies_export: list[dict[str, Any]] = []
                for ck in session.cookies.jar:
                    cookies_export.append({
                        "name": ck.name,
                        "value": ck.value,
                        "domain": ck.domain,
                        "path": ck.path or "/",
                        "secure": bool(ck.secure),
                        # httpOnly không lộ qua API curl_cffi → mặc định True cho
                        # cookie auth (an toàn hơn).
                        "httpOnly": ck.name.startswith("__Host-") or ck.name.startswith("__Secure-"),
                        "sameSite": "Lax",
                        "expires": ck.expires if ck.expires else -1,
                    })
                session_data["__cookies"] = cookies_export
            except Exception as exc:
                log(f"[session-req] cookie export failed: {exc}")
            return session_data
        finally:
            try:
                session.close()
            except Exception:
                pass

    return await asyncio.to_thread(_sync)
