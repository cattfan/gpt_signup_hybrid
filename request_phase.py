"""Pure-request registration phase — no browser required.

Implements the full OpenAI signup state machine via HTTP requests (curl_cffi):
  1. chatgpt.com CSRF + signin/openai → authorize URL
  2. OAuth init → device_id
  3. Sentinel token (QuickJS primary, Python PoW fallback)
  4. authorize/continue (email submission)
  5. register password
  6. OTP send → poll via existing mail providers → verify
  7. create_account (name + birthdate)
  8. Follow redirect chain → callback URL
  9. Consume callback → session_token + access_token

Adapts the protocol from github.com/Regert888/gpt-outlook-register to work
with the existing gpt_signup_hybrid mail providers and SignupRequest/SignupResult.

Public API:
    run_request_phase(request, mail_provider, log) -> SignupResult
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from curl_cffi import requests as curl_requests

from mail_providers import MailProvider
from models import SignupRequest, SignupResult
from user_agent_profile import (
    CURL_IMPERSONATE_CANDIDATES as _UA_IMPERSONATE_CANDIDATES,
    CURL_IMPERSONATE_PRIMARY as _UA_IMPERSONATE_PRIMARY,
    SEC_CH_UA,
    SEC_CH_UA_MOBILE,
    SEC_CH_UA_PLATFORM,
    WINDOWS_USER_AGENT,
)

logger = logging.getLogger(__name__)


class RequestPhaseError(Exception):
    """Pure-request registration failed."""


# ─── Constants ────────────────────────────────────────────────────────

# Re-export cho backward compatibility (session_phase + module khác đã import).
USER_AGENT = WINDOWS_USER_AGENT

_FIRST_NAMES = [
    "James", "John", "Robert", "Michael", "William", "David", "Richard",
    "Joseph", "Thomas", "Charles", "Mary", "Patricia", "Jennifer", "Linda",
    "Elizabeth", "Barbara", "Susan", "Jessica", "Sarah", "Karen",
]
_LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Wilson", "Anderson", "Taylor", "Thomas",
]


# ─── Datadog trace headers (critical for OTP delivery) ────────────────


def _datadog_trace_headers() -> dict[str, str]:
    """Generate Datadog APM trace headers.

    OpenAI frontend uses Datadog RUM — all real browser requests carry these.
    Missing headers cause silent OTP drop (200 OK but no email sent).
    """
    trace_id = str(random.getrandbits(64))
    parent_id = str(random.getrandbits(64))
    trace_hex = format(int(trace_id), "016x")
    parent_hex = format(int(parent_id), "016x")
    return {
        "traceparent": f"00-0000000000000000{trace_hex}-{parent_hex}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


# ─── Session factory ─────────────────────────────────────────────────


def _create_session(proxy: str | None, impersonate: str = _UA_IMPERSONATE_PRIMARY) -> curl_requests.Session:
    session = curl_requests.Session(impersonate=impersonate)
    session.trust_env = False
    if proxy:
        normalized = proxy
        if proxy.startswith("socks5://"):
            normalized = "socks5h://" + proxy[len("socks5://"):]
        session.proxies = {"https": normalized, "http": normalized}
    else:
        session.proxies = {"https": "", "http": ""}
    return session


# TLS fingerprint candidates — rotate on TLS handshake failure (from gpt-outlook-register).
# Đồng bộ với UA: cùng Chrome family, version giảm dần. Defined in user_agent_profile
# để khớp với WINDOWS_USER_AGENT (CHROME_MAJOR).
_IMPERSONATE_CANDIDATES = list(_UA_IMPERSONATE_CANDIDATES)


def _is_tls_error(exc: BaseException) -> bool:
    """Detect curl_cffi TLS handshake errors → worth rotating fingerprint."""
    msg = str(exc).lower()
    markers = [
        "curl: (35)", "tls connect error", "openssl_internal", "sslerror",
        "curl: (56)", "curl: (7)", "ssl_error", "handshake",
    ]
    return any(m in msg for m in markers)


# ─── Sentinel ─────────────────────────────────────────────────────────


def _get_sentinel_token(session, device_id: str, flow: str, log: Callable, worker=None) -> str:
    """Get sentinel token: QuickJS primary → Python PoW fallback.

    ``worker`` (SentinelNodeWorker | None): nếu có → dùng persistent Node process
    (warm) thay vì spawn one-shot mỗi action.
    """
    disable_quickjs = os.getenv("OPENAI_SENTINEL_DISABLE_QUICKJS", "0").lower() in (
        "1", "true", "yes",
    )

    if not disable_quickjs:
        try:
            from sentinel_quickjs import get_sentinel_token_via_quickjs
            token = get_sentinel_token_via_quickjs(
                session,
                device_id,
                flow=flow,
                log=log,
                worker=worker,
            )
            if token:
                return token
            log("[sentinel] QuickJS failed, falling back to Python PoW")
        except Exception as e:
            log(f"[sentinel] QuickJS import/call error, fallback: {e}")

    from sentinel_pow import get_sentinel_token as _pow_token
    return _pow_token(session, device_id, flow=flow)


# ─── Common headers ──────────────────────────────────────────────────


def _common_headers(referer: str = "https://chatgpt.com/") -> dict[str, str]:
    origin = "https://chatgpt.com"
    try:
        parsed = urlparse(referer)
        if parsed.scheme and parsed.netloc:
            origin = f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        pass

    headers = {
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
        "Origin": origin,
        "User-Agent": USER_AGENT,
        # Client Hints — Chrome desktop luôn gửi 3 header này (low-entropy hints,
        # không cần Accept-CH). Bắt buộc đồng bộ với USER_AGENT để tránh mismatch.
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": SEC_CH_UA_PLATFORM,
    }
    headers.update(_datadog_trace_headers())
    return headers


# ─── Auth state machine steps ─────────────────────────────────────────


def _step_csrf(session, log: Callable) -> str:
    """Step 1: GET chatgpt.com/api/auth/csrf → csrfToken.

    Retry up to 3x on Cloudflare 403 (transient rate-limit), backoff 5s/10s.
    """
    log("[request] [1/9] Fetching CSRF token...")
    headers = _common_headers("https://chatgpt.com/auth/login")
    resp = None
    for attempt in range(3):
        resp = session.get(
            "https://chatgpt.com/api/auth/csrf",
            headers=headers,
            timeout=30,
        )
        if resp.status_code == 403 and attempt < 2:
            wait = (attempt + 1) * 5
            log(f"[request] Cloudflare 403, retrying in {wait}s ({attempt + 1}/3)...")
            time.sleep(wait)
            continue
        break
    if resp is None or resp.status_code != 200:
        raise RequestPhaseError(f"CSRF fetch failed: HTTP {resp.status_code if resp else '?'}")
    csrf = resp.json().get("csrfToken", "")
    if not csrf:
        raise RequestPhaseError("CSRF token missing from response")
    log(f"[request] CSRF: {csrf[:20]}...")
    return csrf


def _bootstrap_with_tls_rotation(
    proxy: str | None,
    log: Callable,
    *,
    login_hint: str = "",
) -> tuple[Any, str, str]:
    """Bootstrap CSRF + auth_url + OAuth init with TLS fingerprint rotation.

    On TLS handshake error, rotates curl_cffi impersonate fingerprint
    qua các candidate trong ``_IMPERSONATE_CANDIDATES`` (chain Chrome desktop
    Windows: chrome145 → chrome142 → chrome136 — đồng bộ với
    ``user_agent_profile.WINDOWS_USER_AGENT``).
    Bootstrap steps carry no critical session state yet, so restarting is safe.

    Returns: (session, device_id, auth_url)
    """
    last_exc: BaseException | None = None
    for idx, impersonate in enumerate(_IMPERSONATE_CANDIDATES):
        session = _create_session(proxy=proxy, impersonate=impersonate)
        try:
            if idx > 0:
                log(f"[request] TLS rotation: retrying with impersonate={impersonate}")
            device_id = str(uuid.uuid4())
            csrf = _step_csrf(session, log)
            auth_url = _step_auth_url(session, csrf, log, device_id=device_id, login_hint=login_hint)
            oauth_did = _step_oauth_init(session, auth_url, log)
            if oauth_did:
                device_id = oauth_did
            return session, device_id, auth_url
        except Exception as e:
            last_exc = e
            try:
                session.close()
            except Exception:
                pass
            if _is_tls_error(e) and idx < len(_IMPERSONATE_CANDIDATES) - 1:
                continue
            raise
    # Exhausted all fingerprints
    if last_exc and _is_tls_error(last_exc):
        raise RequestPhaseError(
            "TLS handshake failed with all impersonate fingerprints — "
            "network cannot reach chatgpt.com over HTTPS. Try a different proxy."
        ) from last_exc
    if last_exc:
        raise last_exc
    raise RequestPhaseError("bootstrap failed unexpectedly")


def _step_auth_url(session, csrf_token: str, log: Callable, device_id: str = "", login_hint: str = "") -> str:
    """Step 2: POST chatgpt.com/api/auth/signin/openai → authorize URL.

    Must include query params matching browser:
    prompt=login, ext-oai-did, ext-passkey-client-capabilities, screen_hint=login_or_signup
    login_hint={email} for login flow (lets server route to password/verify directly).
    """
    log("[request] [2/8] Getting authorize URL...")
    headers = _common_headers("https://chatgpt.com/auth/login")
    headers["Content-Type"] = "application/x-www-form-urlencoded"

    # Query params matching browser (_nextauth_bootstrap.py)
    params = {
        "prompt": "login",
        "ext-passkey-client-capabilities": "01001",
        "screen_hint": "login_or_signup",
    }
    if device_id:
        params["ext-oai-did"] = device_id
    if login_hint:
        params["login_hint"] = login_hint

    from urllib.parse import urlencode as _urlencode
    url = "https://chatgpt.com/api/auth/signin/openai?" + _urlencode(params)

    resp = session.post(
        url,
        headers=headers,
        data={
            "csrfToken": csrf_token,
            "callbackUrl": "https://chatgpt.com/",
            "json": "true",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RequestPhaseError(f"signin/openai failed: HTTP {resp.status_code}")
    auth_url = resp.json().get("url", "")
    if not auth_url:
        raise RequestPhaseError("signin/openai: no URL in response")
    log(f"[request] Auth URL: {auth_url[:80]}...")
    return auth_url


def _step_oauth_init(session, auth_url: str, log: Callable) -> str:
    """Step 3: Follow authorize URL → extract device_id from oai-did cookie."""
    log("[request] [3/9] OAuth init...")
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://chatgpt.com/auth/login",
        "User-Agent": USER_AGENT,
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": SEC_CH_UA_PLATFORM,
    }
    session.get(auth_url, headers=headers, timeout=30, allow_redirects=True)

    # Extract device_id from cookies
    device_id = ""
    try:
        device_id = session.cookies.get("oai-did", "")
    except Exception:
        pass
    if not device_id:
        device_id = str(uuid.uuid4())
        log(f"[request] Generated device_id: {device_id}")
    else:
        log(f"[request] Device ID: {device_id}")
    return device_id


def _step_authorize_continue(
    session,
    email: str,
    sentinel_token: str,
    screen_hint: str,
    referer: str,
    device_id: str,
    log: Callable,
) -> dict:
    """POST authorize/continue — submit email to auth state machine."""
    headers = _common_headers(referer)
    headers["Content-Type"] = "application/json"
    if sentinel_token:
        headers["openai-sentinel-token"] = sentinel_token
    if device_id:
        headers["oai-device-id"] = device_id

    payload = {
        "username": {"value": email, "kind": "email"},
        "screen_hint": screen_hint,
    }
    resp = session.post(
        "https://auth.openai.com/api/accounts/authorize/continue",
        headers=headers,
        json=payload,
        timeout=30,
    )
    if resp.status_code != 200:
        body = (resp.text or "")[:300]
        raise RequestPhaseError(
            f"authorize/continue failed: HTTP {resp.status_code} - {body}"
        )
    try:
        return resp.json()
    except Exception:
        return {}


def _step_signup(session, email: str, sentinel_token: str, device_id: str, log: Callable) -> bool:
    """Step 4: Submit email → detect new vs existing account.

    Returns True for new account, False for existing.
    """
    log("[request] [4/9] Submitting email...")
    data = _step_authorize_continue(
        session, email, sentinel_token,
        screen_hint="signup",
        referer="https://auth.openai.com/create-account",
        device_id=device_id,
        log=log,
    )

    page = data.get("page", {}) if isinstance(data, dict) else {}
    page_type = (page.get("type") or "").strip()
    continue_url = (data.get("continue_url") or "").strip()

    if page_type == "create_account_password" or "/create-account/password" in continue_url:
        log("[request] New account detected")
        return True

    if page_type in ("email_otp_verification", "login_password"):
        log(f"[request] Existing account detected (page_type={page_type})")
        return False

    log(f"[request] Unknown page_type={page_type!r}, treating as existing")
    return False


def _step_register_password(session, email: str, password: str, device_id: str, log: Callable) -> bool:
    """Step 5: Register password for new account."""
    log("[request] [5/9] Registering password...")

    # Visit password page first (establish server state)
    try:
        session.get(
            "https://auth.openai.com/create-account/password",
            headers=_common_headers("https://auth.openai.com/create-account"),
            timeout=15,
        )
    except Exception:
        pass

    # Refresh sentinel for username_password_create flow
    sentinel = _get_sentinel_token(session, device_id, "username_password_create", log)

    headers = _common_headers("https://auth.openai.com/create-account/password")
    headers["Content-Type"] = "application/json"
    if sentinel:
        headers["openai-sentinel-token"] = sentinel
    if device_id:
        headers["oai-device-id"] = device_id

    resp = session.post(
        "https://auth.openai.com/api/accounts/user/register",
        headers=headers,
        json={"password": password, "username": email},
        timeout=30,
    )
    if resp.status_code != 200:
        log(f"[request] Register password failed: {resp.status_code} - {(resp.text or '')[:200]}")
        return False
    log("[request] Password registered")
    return True


def _step_send_otp(session, device_id: str, log: Callable) -> None:
    """Step 6a: Trigger OTP email delivery."""
    log("[request] [6/9] Sending OTP...")
    headers = _common_headers("https://auth.openai.com/create-account/password")
    if device_id:
        headers["oai-device-id"] = device_id

    resp = session.get(
        "https://auth.openai.com/api/accounts/email-otp/send",
        headers=headers,
        timeout=30,
    )
    if resp.status_code != 200:
        # Fallback: try passwordless send
        headers2 = _common_headers("https://auth.openai.com/create-account/password")
        headers2["Content-Type"] = "application/json"
        if device_id:
            headers2["oai-device-id"] = device_id
        resp2 = session.post(
            "https://auth.openai.com/api/accounts/passwordless/send-otp",
            headers=headers2,
            timeout=30,
        )
        if resp2.status_code != 200:
            raise RequestPhaseError(
                f"OTP send failed: primary={resp.status_code} fallback={resp2.status_code}"
            )
    log("[request] OTP sent")


def _step_resend_otp(session, device_id: str, log: Callable) -> bool:
    """Resend OTP (for existing account flow)."""
    headers = _common_headers("https://auth.openai.com/email-verification")
    headers["Content-Type"] = "application/json"
    if device_id:
        headers["oai-device-id"] = device_id
    resp = session.post(
        "https://auth.openai.com/api/accounts/email-otp/resend",
        headers=headers,
        timeout=30,
    )
    if resp.status_code == 200:
        log("[request] OTP resent")
        return True
    log(f"[request] OTP resend failed: {resp.status_code}")
    return False


def _step_verify_otp(
    session, otp_code: str, device_id: str, log: Callable,
    *, raise_on_fail: bool = True,
) -> dict:
    """Step 7: Verify OTP code.

    raise_on_fail=True (mặc định, cho session_phase): raise RequestPhaseError nếu
    HTTP != 200. raise_on_fail=False (cho retry ở request_phase): trả dict kèm
    metadata ``_ok`` / ``_status`` / ``_body`` để caller tự quyết định retry.
    """
    log("[request] [7/9] Verifying OTP...")
    headers = _common_headers("https://auth.openai.com/email-verification")
    headers["Content-Type"] = "application/json"
    if device_id:
        headers["oai-device-id"] = device_id

    resp = session.post(
        "https://auth.openai.com/api/accounts/email-otp/validate",
        headers=headers,
        json={"code": otp_code},
        timeout=30,
    )
    if resp.status_code != 200:
        body = resp.text or ""
        if raise_on_fail:
            raise RequestPhaseError(
                f"OTP verify failed: HTTP {resp.status_code} - {body[:200]}"
            )
        log(f"[request] OTP verify HTTP {resp.status_code}: {body[:120]}")
        return {"_ok": False, "_status": resp.status_code, "_body": body}
    log("[request] OTP verified")
    try:
        data = resp.json()
    except Exception:
        data = {}
    if isinstance(data, dict):
        data["_ok"] = True
        data["_status"] = 200
    return data


def _step_create_account(
    session, name: str, birthdate: str, device_id: str, log: Callable,
    sentinel_token: str | None = None, worker=None,
) -> str:
    """Step 8: Create account (fill name + birthdate) → continue_url.

    ``sentinel_token``: nếu đã pre-compute sẵn (song song lúc poll OTP) thì dùng
    luôn, bỏ qua bước tính sentinel tại đây. None → tính mới.
    """
    log("[request] [8/9] Creating account...")

    # Refresh sentinel for create_account flow (dùng token pre-computed nếu có)
    sentinel = sentinel_token or _get_sentinel_token(
        session, device_id, "create_account", log, worker=worker,
    )

    headers = _common_headers("https://auth.openai.com/about-you")
    headers["Content-Type"] = "application/json"
    if sentinel:
        headers["openai-sentinel-token"] = sentinel
    if device_id:
        headers["oai-device-id"] = device_id

    resp = session.post(
        "https://auth.openai.com/api/accounts/create_account",
        headers=headers,
        json={"name": name, "birthdate": birthdate},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RequestPhaseError(
            f"create_account failed: HTTP {resp.status_code} - {(resp.text or '')[:300]}"
        )
    data = resp.json()
    continue_url = (data.get("continue_url") or "").strip()
    if not continue_url:
        raise RequestPhaseError("create_account: no continue_url in response")
    log("[request] Account created")
    return continue_url


def _step_follow_redirects(session, start_url: str, log: Callable) -> tuple[str, str]:
    """Step 9: Follow redirect chain → (callback_url, final_url)."""
    log("[request] [9/9] Following redirect chain...")
    current = start_url
    callback_url = ""

    for i in range(12):
        if "/api/auth/callback/openai" in current and "code=" in current:
            callback_url = current
            break

        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://chatgpt.com/",
            "User-Agent": USER_AGENT,
            "sec-ch-ua": SEC_CH_UA,
            "sec-ch-ua-mobile": SEC_CH_UA_MOBILE,
            "sec-ch-ua-platform": SEC_CH_UA_PLATFORM,
        }
        resp = session.get(current, headers=headers, timeout=30, allow_redirects=False)

        if resp.status_code in (301, 302, 303, 307, 308):
            location = (resp.headers.get("Location") or "").strip()
            if not location:
                break
            if location.startswith("/"):
                parsed = urlparse(current)
                location = f"{parsed.scheme}://{parsed.netloc}{location}"
            if "/api/auth/callback/openai" in location and "code=" in location:
                callback_url = location
                current = location
                break
            current = location
        else:
            break

    log(f"[request] Redirect chain done, callback={'found' if callback_url else 'missing'}")
    return callback_url, current


def _consume_callback(session, callback_url: str, log: Callable) -> bool:
    """GET callback URL to trigger NextAuth session cookie set.

    Uses allow_redirects=True so curl follows the whole chain in one call
    (connection reuse, far fewer round-trips than manual hop-by-hop).
    """
    if not callback_url or "code=" not in callback_url:
        return False
    try:
        session.get(
            callback_url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://auth.openai.com/",
                "User-Agent": USER_AGENT,
                "sec-ch-ua": SEC_CH_UA,
                "sec-ch-ua-mobile": SEC_CH_UA_MOBILE,
                "sec-ch-ua-platform": SEC_CH_UA_PLATFORM,
            },
            timeout=30,
            allow_redirects=True,
        )
        st = session.cookies.get("__Secure-next-auth.session-token", "")
        return bool(st)
    except Exception as e:
        log(f"[request] Consume callback error: {e}")
        return False


def _get_session_tokens(session, log: Callable) -> tuple[str, str, str]:
    """GET /api/auth/session → (session_token, access_token, user_id)."""
    headers = _common_headers("https://chatgpt.com/")
    resp = session.get(
        "https://chatgpt.com/api/auth/session",
        headers=headers,
        timeout=30,
    )
    if resp.status_code != 200:
        log(f"[request] /api/auth/session HTTP {resp.status_code}")
        return "", "", ""

    data = resp.json() if resp is not None else {}
    access_token = data.get("accessToken", "") or ""
    user = data.get("user", {}) or {}
    user_id = user.get("id", "") or ""

    # Session token from cookie
    session_token = session.cookies.get("__Secure-next-auth.session-token", "") or ""
    return session_token, access_token, user_id


# ─── OTP polling bridge (async mail provider → sync wait) ─────────────


async def _poll_otp_async(
    provider: MailProvider,
    *,
    recipient: str,
    started_at: datetime,
    timeout_seconds: float,
    poll_interval_seconds: float,
    log: Callable,
) -> str:
    """Async wrapper for mail provider OTP polling."""
    return await provider.poll_otp(
        recipient=recipient,
        started_at=started_at,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        log=log,
    )


# ─── Main orchestrator ────────────────────────────────────────────────


def _run_request_phase_sync(
    request: SignupRequest,
    mail_provider: MailProvider,
    log: Callable,
) -> dict[str, Any]:
    """Synchronous core — runs in thread via asyncio.to_thread.

    Flow (matching browser HAR):
      1. CSRF + signin/openai (login_hint) → auth_url
      2. GET auth_url (OAuth init) → device_id
      3. Sentinel token
      4. POST /api/accounts/user/register (email + password) — DIRECT, no authorize/continue
      5. GET /api/accounts/email-otp/send
      6. Poll OTP (started_at = exact send time) → POST email-otp/validate
      7. POST /api/accounts/create_account
      8. Follow redirect chain → callback → session
    """
    worker = None
    try:
        # Persistent Node worker cho sentinel (warm — tránh cold-start V8 mỗi action).
        # Dùng chung cho cả sentinel #1 (register) và #2 (create_account, pre-computed).
        from sentinel_quickjs import create_worker as _create_sentinel_worker
        try:
            worker = _create_sentinel_worker(log)
        except Exception as _e:
            log(f"[request] sentinel worker init failed, dùng one-shot: {_e}")
            worker = None

        # Step 1-5: Bootstrap + Register, có retry cho HTTP 409 invalid_state.
        #
        # Khi server trả 409 ``invalid_state`` ("Your sign-in session is no
        # longer valid"), state machine OAuth đã desync (CSRF/auth_url/sentinel
        # cũ không còn hợp lệ). Cách fix duy nhất là RE-BOOTSTRAP toàn bộ:
        # session mới, CSRF mới, device_id mới, sentinel mới — KHÔNG được tái
        # dùng artifact cũ. Retry tối đa 3 lần để tránh loop vô tận khi server
        # đang gặp vấn đề thực sự.
        max_register_attempts = 3
        session = None
        device_id = ""
        password = request.password or _default_password(request.email)
        reg_continue = ""
        reg_page_type = ""

        for register_attempt in range(1, max_register_attempts + 1):
            # Đóng session cũ trước khi re-bootstrap (nếu có)
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
                session = None

            if register_attempt > 1:
                log(
                    f"[request] Re-bootstrap mới "
                    f"(lần {register_attempt}/{max_register_attempts}) "
                    f"sau HTTP 409 invalid_state"
                )

            # Step 1-3: Bootstrap with TLS fingerprint rotation on handshake failure.
            # Pass login_hint=email so authorize routes to the correct account context.
            session, device_id, _auth_url = _bootstrap_with_tls_rotation(
                request.proxy, log, login_hint=request.email,
            )

            # Step 4: GET /create-account/password page to establish server signup state.
            # HAR confirms browser does NOT call authorize/continue before user/register.
            # Instead it relies on the authorize URL (with login_hint) + this page visit
            # to set the state machine into "create account" mode.
            try:
                session.get(
                    "https://auth.openai.com/create-account/password",
                    headers=_common_headers("https://auth.openai.com/create-account"),
                    timeout=15,
                )
            except Exception:
                pass

            # Step 5: Sentinel (flow=username_password_create) + user/register
            sentinel = _get_sentinel_token(
                session, device_id, "username_password_create", log, worker=worker,
            )

            log("[request] [4/8] Registering account (password)...")
            reg_headers = _common_headers("https://auth.openai.com/create-account/password")
            reg_headers["Content-Type"] = "application/json"
            if sentinel:
                reg_headers["openai-sentinel-token"] = sentinel
            if device_id:
                reg_headers["oai-device-id"] = device_id

            resp = session.post(
                "https://auth.openai.com/api/accounts/user/register",
                headers=reg_headers,
                json={"password": password, "username": request.email},
                timeout=30,
            )

            if resp.status_code == 200:
                reg_data = resp.json() if resp is not None else {}
                reg_continue = (reg_data.get("continue_url") or "").strip()
                reg_page_type = ((reg_data.get("page") or {}).get("type") or "").strip()
                log(f"[request] Register OK → page_type={reg_page_type!r} continue_url={reg_continue[:80]!r}")
                # LƯU Ý: page_type=email_otp_verification sau user/register LÀ HỢP LỆ cho
                # account MỚI (server yêu cầu verify email vừa nhập). KHÔNG được coi là
                # "đã tồn tại". Signal duy nhất cho email đã đăng ký là HTTP 400 invalid_auth_step.
                break  # success → exit retry loop

            # 400 invalid_auth_step = email đã đăng ký rồi → fail-fast, KHÔNG retry
            if resp.status_code == 400 and "invalid_auth_step" in (resp.text or ""):
                raise RequestPhaseError(
                    f"email {request.email} đã được đăng ký (invalid_auth_step) "
                    f"— cần email mới để reg"
                )

            body = (resp.text or "")[:300]

            # 409 invalid_state = state machine desync → re-bootstrap fresh và retry
            if resp.status_code == 409 and "invalid_state" in body:
                log(
                    f"[request] user/register HTTP 409 invalid_state "
                    f"(lần {register_attempt}/{max_register_attempts}): {body[:200]}"
                )
                if register_attempt >= max_register_attempts:
                    raise RequestPhaseError(
                        f"user/register failed sau {max_register_attempts} lần retry "
                        f"với HTTP 409 invalid_state - {body}"
                    )
                # backoff ngắn để server clear state cũ trước khi bootstrap lại
                time.sleep(1.5)
                continue

            # Lỗi khác (5xx, 401, 422...) → fail-fast, không retry mù quáng
            raise RequestPhaseError(f"user/register failed: HTTP {resp.status_code} - {body}")

        # Step 6: Send OTP
        log("[request] [5/8] Sending OTP...")
        otp_started_at = datetime.now(timezone.utc)

        if reg_continue and "/email-otp/send" in reg_continue:
            otp_headers = _common_headers("https://auth.openai.com/email-verification")
            if device_id:
                otp_headers["oai-device-id"] = device_id
            resp = session.get(reg_continue, headers=otp_headers, timeout=30)
            if resp.status_code not in (200, 302):
                log(f"[request] OTP send via continue_url returned {resp.status_code}")
                _step_send_otp(session, device_id, log)
        else:
            _step_send_otp(session, device_id, log)
        log("[request] OTP sent")

        # Pre-compute sentinel create_account SONG SONG với poll OTP.
        # Trong lúc chờ OTP (~7s idle I/O), curl session KHÔNG được dùng bởi thread
        # chính (poll đi qua mail_provider/httpx riêng), nên thread phụ độc quyền
        # dùng session để fetch challenge. Token bind device_id+flow, không bind OTP
        # → tái dùng được sau verify. Thread được join TRƯỚC verify OTP nên không có
        # race trên curl session (vốn không thread-safe).
        precomputed_sentinel: dict[str, str | None] = {"token": None}

        def _precompute_create_sentinel() -> None:
            try:
                precomputed_sentinel["token"] = _get_sentinel_token(
                    session, device_id, "create_account", log, worker=worker,
                )
            except Exception as exc:  # fallback: tính lại tại _step_create_account
                log(f"[request] pre-compute create_account sentinel lỗi (sẽ tính lại): {exc}")
                precomputed_sentinel["token"] = None

        precompute_thread = threading.Thread(
            target=_precompute_create_sentinel,
            name="precompute-create-sentinel",
            daemon=True,
        )
        precompute_thread.start()

        # Step 6: Poll OTP directly (new event loop in this thread).
        # started_at = send time → only accept codes delivered AFTER this point,
        # avoiding stale codes from previous attempts in the same inbox.
        log("[request] [6/8] Waiting for OTP...")
        import asyncio as _asyncio
        _loop = _asyncio.new_event_loop()
        try:
            otp_code = _loop.run_until_complete(
                mail_provider.poll_otp(
                    recipient=request.source_email or request.email,
                    started_at=otp_started_at,
                    timeout_seconds=request.otp_timeout_seconds,
                    poll_interval_seconds=request.otp_poll_interval_seconds,
                    log=log,
                )
            )
        finally:
            _loop.close()

        # Join pre-compute thread TRƯỚC khi verify OTP → đảm bảo session free +
        # token sẵn sàng. Timeout rộng để không treo nếu sentinel chậm bất thường.
        precompute_thread.join(timeout=45.0)
        if precompute_thread.is_alive():
            log("[request] pre-compute sentinel chưa xong sau 45s → sẽ tính lại tại create_account")

        if not otp_code:
            raise RequestPhaseError("OTP polling returned empty code")

        # Step 7: Verify OTP với retry. Nếu server trả "wrong code" (code stale
        # hoặc đã bị thay) → resend OTP + poll code MỚI rồi verify lại, thay vì
        # fail cứng. Chỉ raise khi hết số lần hoặc gặp lỗi không phải wrong-code.
        max_verify_attempts = 3
        tried_codes: set[str] = {otp_code}
        otp_resp: dict = {}
        verified = False
        for v_attempt in range(1, max_verify_attempts + 1):
            otp_resp = _step_verify_otp(
                session, otp_code, device_id, log, raise_on_fail=False,
            )
            if otp_resp.get("_ok"):
                verified = True
                break

            status = otp_resp.get("_status")
            body = str(otp_resp.get("_body") or "")
            is_wrong_code = (
                status == 401
                or "wrong_email_otp_code" in body
                or "wrong code" in body.lower()
            )
            if not is_wrong_code:
                raise RequestPhaseError(
                    f"OTP verify failed: HTTP {status} - {body[:200]}"
                )
            if v_attempt >= max_verify_attempts:
                raise RequestPhaseError(
                    f"OTP verify vẫn sai sau {max_verify_attempts} lần "
                    f"(HTTP {status}) — code stale/không hợp lệ"
                )

            # Wrong code → resend OTP (email mới) + poll code mới (loại code đã thử).
            # Mirror logic của browser_phase: poll mini-timeout từng vòng, sleep giữa
            # các poll khi worker trả lại code cũ, đếm stale_poll_count → resend lại
            # sau N lần code cũ liên tiếp, dùng poll_all_codes catch mail delay.
            log(f"[request] OTP sai (lần {v_attempt}/{max_verify_attempts}) → resend + chờ code mới")
            retry_started_at = datetime.now(timezone.utc)
            try:
                if not _step_resend_otp(session, device_id, log):
                    _step_send_otp(session, device_id, log)
            except Exception as exc:
                log(f"[request] resend OTP lỗi (vẫn poll tiếp): {exc}")

            _retry_loop = _asyncio.new_event_loop()
            try:
                new_code = ""
                pending_candidates: list[str] = []
                stale_poll_count = 0
                stale_poll_resend_threshold = 5  # 5 lần code cũ liên tiếp → resend lại
                inner_resend_count = 0
                inner_max_resends = 2  # tối đa resend thêm 2 lần trong vòng retry này
                resend_after_seconds = 30.0  # mini-timeout per poll call
                poll_interval = max(5.0, request.otp_poll_interval_seconds)

                _poll_deadline = time.monotonic() + request.otp_timeout_seconds
                while time.monotonic() < _poll_deadline:
                    # Pop pending trước nếu có (sau khi nhận code mới fetch_all)
                    while pending_candidates:
                        c = pending_candidates.pop(0)
                        if c not in tried_codes:
                            new_code = c
                            break
                    if new_code:
                        break

                    remaining = _poll_deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    mini_timeout = min(resend_after_seconds, remaining)

                    try:
                        candidate = _retry_loop.run_until_complete(
                            mail_provider.poll_otp(
                                recipient=request.source_email or request.email,
                                started_at=retry_started_at,
                                timeout_seconds=mini_timeout,
                                poll_interval_seconds=poll_interval,
                                log=log,
                            )
                        )
                    except TimeoutError:
                        candidate = ""

                    if candidate and candidate not in tried_codes:
                        # Nhận code mới → fetch all để catch mail delay (worker iCloud
                        # có thể có nhiều mail OTP cùng lúc, nên thử lần lượt).
                        time.sleep(2.0)
                        all_codes: list[str] = []
                        if hasattr(mail_provider, "poll_all_codes"):
                            try:
                                all_codes = _retry_loop.run_until_complete(
                                    mail_provider.poll_all_codes(
                                        recipient=request.source_email or request.email,
                                        started_at=retry_started_at,
                                        log=log,
                                    )
                                )
                            except Exception:
                                all_codes = []
                        fresh = [c for c in all_codes if c not in tried_codes]
                        if not fresh:
                            fresh = [candidate]
                        elif candidate not in fresh:
                            fresh.insert(0, candidate)
                        if len(fresh) > 1:
                            log(f"[request] nhận {len(fresh)} OTP codes mới: {', '.join(fresh)}")
                        new_code = fresh.pop(0)
                        pending_candidates = fresh
                        break

                    if candidate and candidate in tried_codes:
                        stale_poll_count += 1
                        if (
                            stale_poll_count >= stale_poll_resend_threshold
                            and inner_resend_count < inner_max_resends
                        ):
                            inner_resend_count += 1
                            stale_poll_count = 0
                            log(
                                f"[request] poll {stale_poll_resend_threshold} lần chỉ "
                                f"code cũ → resend lại ({inner_resend_count}/{inner_max_resends})"
                            )
                            try:
                                if not _step_resend_otp(session, device_id, log):
                                    _step_send_otp(session, device_id, log)
                            except Exception as exc:
                                log(f"[request] resend lại lỗi: {exc}")
                            retry_started_at = datetime.now(timezone.utc)
                            time.sleep(2.0)
                        else:
                            log(
                                f"[request] poll trả lại code đã thử ({candidate}) → "
                                f"chờ tiếp ({stale_poll_count}/{stale_poll_resend_threshold})"
                            )
                            time.sleep(poll_interval)
                        continue

                    # candidate rỗng = mini-timeout hết mà không có mail nào → loop tiếp
                    # (vẫn còn deadline). Sleep ngắn để tránh spam khi worker trả rỗng.
                    time.sleep(poll_interval)
            finally:
                _retry_loop.close()

            if not new_code:
                raise RequestPhaseError("OTP retry: không nhận được code mới")
            otp_code = new_code
            tried_codes.add(otp_code)

        if not verified:
            raise RequestPhaseError("OTP verify thất bại")

        # Step 8: Create account (dùng sentinel pre-computed nếu có)
        continue_url = _step_create_account(
            session, request.name, request.birthdate, device_id, log,
            sentinel_token=precomputed_sentinel.get("token"),
            worker=worker,
        )

        # Step 9: Follow redirects + get session
        if not continue_url:
            raise RequestPhaseError("No continue_url after create_account")

        callback_url, final_url = _step_follow_redirects(session, continue_url, log)

        if callback_url:
            _consume_callback(session, callback_url, log)

        session_token, access_token, user_id = _get_session_tokens(session, log)

        if not session_token and not access_token:
            raise RequestPhaseError(
                "Registration completed but no session_token/access_token obtained"
            )

        # Extract all cookies for result
        cookies = []
        try:
            for cookie in session.cookies:
                name = getattr(cookie, "name", "") or ""
                value = getattr(cookie, "value", "") or ""
                domain = getattr(cookie, "domain", "") or ""
                if name and value:
                    cookies.append({
                        "name": name, "value": value,
                        "domain": domain, "path": "/", "secure": True,
                    })
        except Exception:
            pass

        return {
            "session_token": session_token,
            "access_token": access_token,
            "user_id": user_id,
            "password": password,
            "cookies": cookies,
            "device_id": device_id,
        }
    finally:
        try:
            session.close()
        except Exception:
            pass
        if worker is not None:
            try:
                worker.close()
            except Exception:
                pass


def _default_password(email: str) -> str:
    pwd = email.replace("@", "")
    if len(pwd) < 8:
        pwd = f"{pwd}2026OpenAI"
    return pwd


async def run_request_phase(
    *,
    request: SignupRequest,
    mail_provider: MailProvider,
    log: Callable = print,
) -> SignupResult:
    """Run pure-request registration. Returns SignupResult.

    The sync core runs in a worker thread (asyncio.to_thread) and polls OTP
    inline via a fresh event loop, with started_at = exact OTP send time so
    stale codes from previous attempts are never picked up.
    """
    result = SignupResult(success=False, email=request.email)
    t_start = time.monotonic()

    try:
        phase_result = await asyncio.to_thread(
            _run_request_phase_sync, request, mail_provider, log,
        )

        result.success = True
        result.session_token = phase_result.get("session_token")
        result.access_token = phase_result.get("access_token")
        result.user_id = phase_result.get("user_id")
        result.password = phase_result.get("password") or request.password
        result.name = request.name
        result.cookies = phase_result.get("cookies", [])
        result.phase1_seconds = time.monotonic() - t_start
        result.phase2_seconds = 0.0  # No separate phase 2 in pure-request mode

        # Compute age
        try:
            y, m, d = request.birthdate.split("-")
            today = datetime.utcnow()
            result.age = today.year - int(y) - ((today.month, today.day) < (int(m), int(d)))
        except Exception:
            pass

        log(f"[request] Registration complete! session_token={'yes' if result.session_token else 'no'} "
            f"access_token={'yes' if result.access_token else 'no'}")

    except RequestPhaseError as e:
        result.error = f"RequestPhaseError: {e}"
        log(f"[request] FAILED: {result.error}")
    except TimeoutError as e:
        result.error = f"TimeoutError: {e}"
        log(f"[request] TIMEOUT: {result.error}")
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        log(f"[request] ERROR: {result.error}")
    finally:
        total = time.monotonic() - t_start
        log(f"[request] Total time: {total:.2f}s")

    return result
