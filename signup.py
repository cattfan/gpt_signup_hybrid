"""Orchestrator: Phase 1 (browser) → poll OTP → Phase 2 (HTTP) → SignupResult.

Supports two registration modes:
  - "browser" (default): Camoufox/Playwright browser Phase 1 + HTTP Phase 2
  - "pure_request": Full HTTP-only flow via curl_cffi (no browser)
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .browser_phase import BrowserPhaseError, run_browser_phase
from .config import load_settings, runtime_session_dir
from .http_phase import HttpPhaseError, run_http_phase
from .mail_providers import (
    MailProvider,
    OutlookComboError,
    OutlookProviderUnavailable,
    build_provider_dongvanfb,
    build_provider_gmail_advanced,
    build_provider_outlook,
    build_provider_worker,
)
from .models import SignupRequest, SignupResult
from .random_profile import random_profile
from .request_phase import RequestPhaseError, run_request_phase

if TYPE_CHECKING:
    from .db.repositories import ComboRepository


def _build_mail_provider(
    request: SignupRequest,
    *,
    settings,
    combo_repo: "ComboRepository | None" = None,
) -> MailProvider:
    """Chọn provider theo request.mail_provider.

    NOTE: OTP polling cố tình KHÔNG đi qua ``request.proxy``. Proxy chỉ áp cho
    browser register (Phase 1) + curl_cffi /api/auth/session (Phase 2). Mail
    provider luôn poll direct để tránh proxy datacenter bị Microsoft / mail
    API chặn, và để fingerprint mail-poll không bị ràng vào IP exit register.
    """
    if request.mail_provider == "outlook":
        if not request.outlook_combo:
            raise ValueError("mail_provider='outlook' yêu cầu --outlook-combo")
        return build_provider_outlook(
            combo=request.outlook_combo,
            state_dir=settings.runtime_dir / "outlook_state",
            proxy=None,
            combo_repo=combo_repo,
        )
    if request.mail_provider == "dongvanfb":
        if not request.outlook_combo:
            raise ValueError("mail_provider='dongvanfb' yêu cầu --outlook-combo")
        return build_provider_dongvanfb(
            combo=request.outlook_combo,
            proxy=None,
        )
    if request.mail_provider == "gmail_advanced":
        if not request.gmail_api_url:
            raise ValueError("mail_provider='gmail_advanced' yêu cầu gmail_api_url")
        provider_email = request.email
        if provider_email == "pending@gmail-advanced.local":
            provider_email = ""
        return build_provider_gmail_advanced(
            email=provider_email,
            api_url=request.gmail_api_url,
        )
    if request.mail_provider == "worker":
        return build_provider_worker(
            logs_url=request.email_logs_url,
            api_key=request.email_api_key,
            insecure_tls=request.email_insecure_tls,
        )
    raise ValueError(f"unknown mail_provider: {request.mail_provider}")


async def run_signup(
    request: SignupRequest,
    *,
    log=print,
    combo_repo: "ComboRepository | None" = None,
) -> SignupResult:
    """Chạy signup, return SignupResult.

    Routing:
      - reg_mode="pure_request" → full HTTP-only (curl_cffi + sentinel)
      - reg_mode="browser" (default) → Camoufox/Playwright Phase 1 + HTTP Phase 2
    """
    settings = load_settings()

    t_total_start = time.monotonic()
    result = SignupResult(success=False, email=request.email)

    try:
        # ── Random profile nếu chưa set ──────────────────────────
        if not request.password or request.name == "ChatGPT User" or request.birthdate == "2000-01-01":
            profile = random_profile()
            if not request.password:
                request = request.model_copy(update={"password": profile["password"]})
            if request.name == "ChatGPT User":
                request = request.model_copy(update={"name": profile["name"]})
            if request.birthdate == "2000-01-01":
                request = request.model_copy(update={"birthdate": profile["birthdate"]})
            log(f"[signup] profile: name={request.name} age={profile['age']}")

        # ── Build mail provider (shared for both modes) ───────────
        provider = _build_mail_provider(request, settings=settings, combo_repo=combo_repo)

        # ── Pre-check cho Gmail Advanced ──────────────────────────
        if hasattr(provider, "pre_check"):
            try:
                await provider.pre_check(log=log)
            finally:
                if provider.email and provider.email != request.email:
                    request = request.model_copy(update={"email": provider.email})
                    result.email = provider.email
                    log(f"[signup] email updated from API: {request.email}")
            if not provider.email or provider.email == "pending@gmail-advanced.local":
                raise ValueError(
                    "Gmail Advanced: API không trả email, không thể tiếp tục signup"
                )

        # ═══════════════════════════════════════════════════════════
        # MODE: pure_request — full HTTP-only registration
        # ═══════════════════════════════════════════════════════════
        if request.reg_mode == "pure_request":
            log(f"[signup] mode=pure_request → HTTP-only registration (email={request.email})")
            result = await run_request_phase(
                request=request,
                mail_provider=provider,
                log=log,
            )
            # Ensure email is set correctly
            if not result.email:
                result.email = request.email

        # ═══════════════════════════════════════════════════════════
        # MODE: browser — Camoufox/Playwright Phase 1 + HTTP Phase 2
        # ═══════════════════════════════════════════════════════════
        else:
            # ── Phase 1: browser → poll OTP → submit OTP → /about-you ──
            t_p1 = time.monotonic()
            log(f"[signup] phase 1: browser → email-verification → submit OTP → /about-you (email={request.email})")
            otp_started_at = datetime.now(timezone.utc).replace(microsecond=0)

            handoff, otp_seconds = await run_browser_phase(
                request=request,
                settings=settings,
                mail_provider=provider,
                otp_started_at=otp_started_at,
                log=log,
            )
            result.phase1_seconds = time.monotonic() - t_p1
            result.otp_seconds = otp_seconds
            log(f"[signup] phase 1 done in {result.phase1_seconds:.2f}s (OTP {otp_seconds:.2f}s)")

            # ── Phase 2: HTTP extract session + access_token ──
            t_p2 = time.monotonic()
            log(f"[signup] phase 2: HTTP extract session + access_token")
            phase2_result = await run_http_phase(
                request=request, handoff=handoff, log=log,
            )
            result.phase2_seconds = time.monotonic() - t_p2
            log(f"[signup] phase 2 done in {result.phase2_seconds:.2f}s")

            result.success = True
            result.session_token = phase2_result["session_token"]
            result.access_token = phase2_result.get("access_token")
            result.user_id = phase2_result.get("user_id")
            result.account_id = phase2_result.get("account_id")
            result.cookies = phase2_result["cookies"]
            result.password = request.password
            result.name = request.name
            # Compute age
            try:
                y, m, d = request.birthdate.split("-")
                from datetime import datetime as _dt
                today = _dt.utcnow()
                result.age = today.year - int(y) - ((today.month, today.day) < (int(m), int(d)))
            except Exception:
                pass

    except (BrowserPhaseError, HttpPhaseError, RequestPhaseError, TimeoutError, ValueError, OutlookComboError, OutlookProviderUnavailable) as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        log(f"[signup] FAILED: {result.error}")
    except Exception as exc:  # pragma: no cover — unexpected
        result.error = f"unexpected {type(exc).__name__}: {exc}"
        log(f"[signup] UNEXPECTED FAILURE: {result.error}")
        raise
    finally:
        log(f"[signup] total {time.monotonic() - t_total_start:.2f}s")

    return result
