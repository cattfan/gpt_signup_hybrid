#!/usr/bin/env python3
"""Probe luồng UPI HOSTED (checkout_ui_mode=hosted) để xem Stripe trả gì.

Mục tiêu: KHÔNG đụng production code. Chỉ chạy thật từng bước hosted-mode UPI,
dump full response (đã redact secret) để xác minh:
    - hosted checkout có expose UPI không (payment_method_types)
    - amount = 0 (promo free month) ?
    - confirm UPI trả next_action.upi_handle_redirect_or_display_qr_code (QR) ?
    - payment link = pay.openai.com/c/pay/cs_...

Flow (tái dùng helper payment_link.py cho DRY):
    1. login pure-HTTP  → accessToken            (DIRECT)
    2. checkout(hosted) → cs_live + publishable  (proxy nếu có)
    3. payment_pages/{cs}/init → config_id/init_checksum/eid/amount/pm_types
    4. payment_methods (type=upi, billing IN)   → pm_...
    5. payment_pages/{cs}/confirm (upi)          → next_action (QR) | error

Chạy:
    ACCOUNT_LINE='email|password|secret' \
    UPI_PROXY='host:port:user:pass' \
    python3 test/probe_hosted_upi.py

UPI_PROXY optional (DIRECT nếu thiếu) nhưng UPI cần IP India để không bị
geo-block — nên truyền proxy IN.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _log(step: str, msg: str) -> None:
    print(f"[{step}] {msg}", flush=True)


def _parse_account(line: str) -> tuple[str, str, str | None]:
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 2 or "@" not in parts[0]:
        raise ValueError("ACCOUNT_LINE phải là email|password|totp_secret")
    return parts[0], parts[1], parts[2] if len(parts) >= 3 and parts[2] else None


def _materialize_proxy(raw: str | None) -> str | None:
    """host:port[:user[:pass]] → http://user:pass@host:port (qua proxy_format)."""
    if not raw:
        return None
    if raw.strip().startswith("http://") or raw.strip().startswith("https://"):
        return raw.strip()  # đã là URL concrete (token export)
    from web.proxy_format import materialize_proxy
    return materialize_proxy(raw.strip())


def _load_latest_token() -> dict | None:
    """Pick token export mới nhất trong runtime/session_cache/<instance>/ để reuse access_token.

    Cho phép probe Stripe hosted-UPI mà KHÔNG cần login lại (token ChatGPT JWT
    thường sống vài giờ). TOKEN_FILE env override file cụ thể.
    """
    override = os.environ.get("TOKEN_FILE", "").strip()
    from session_store import resolve_instance_id
    tok_dir = ROOT / "runtime" / "session_cache" / resolve_instance_id()
    if override:
        p = Path(override)
        if not p.is_absolute():
            p = tok_dir / override
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    if not tok_dir.exists():
        return None
    files = sorted(tok_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        return None
    return json.loads(files[0].read_text(encoding="utf-8"))


def _redact(obj, _depth=0):
    """Redact value của key nhạy cảm để dump an toàn."""
    SENSITIVE = ("secret", "token", "key", "cookie", "authorization", "password")
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(s in str(k).lower() for s in SENSITIVE):
                out[k] = "[redacted]"
            else:
                out[k] = _redact(v, _depth + 1)
        return out
    if isinstance(obj, list):
        return [_redact(x, _depth + 1) for x in obj[:20]]
    if isinstance(obj, str) and len(obj) > 300:
        return obj[:200] + f"...(+{len(obj) - 200})"
    return obj


def _dump(label: str, data) -> None:
    print(f"\n----- {label} -----", flush=True)
    try:
        print(json.dumps(_redact(data), indent=2, ensure_ascii=False)[:6000], flush=True)
    except Exception:  # noqa: BLE001
        print(repr(data)[:2000], flush=True)


# ── UPI hosted confirm — model trên nhánh GoPay của payment_link.py ──────────
_STRIPE_PM_URL = "https://api.stripe.com/v1/payment_methods"
_STRIPE_CONFIRM_URL_TPL = "https://api.stripe.com/v1/payment_pages/{session_id}/confirm"
_STRIPE_VERSION = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"


async def _create_upi_payment_method(session, *, publishable_key, profile, email, proxies):
    from user_agent_profile import (
        SEC_CH_UA as _SEC_CH_UA,
        SEC_CH_UA_MOBILE as _SEC_CH_UA_MOBILE,
        SEC_CH_UA_PLATFORM as _SEC_CH_UA_PLATFORM,
        WINDOWS_USER_AGENT as _UA,
    )
    guid, muid, sid = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
    data = {
        "type": "upi",
        "billing_details[name]": profile["name"],
        "billing_details[email]": email,
        "billing_details[address][country]": "IN",
        "billing_details[address][line1]": profile["address_line1"],
        "billing_details[address][city]": profile["city"],
        "billing_details[address][postal_code]": profile["postal_code"],
        "billing_details[address][state]": profile["state"],
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "_stripe_version": _STRIPE_VERSION,
        "key": publishable_key,
        "payment_user_agent": "stripe.js/922d612e68; stripe-js-v3/922d612e68; checkout",
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://pay.openai.com",
        "Referer": "https://pay.openai.com/",
        "User-Agent": _UA,
        "sec-ch-ua": _SEC_CH_UA,
        "sec-ch-ua-mobile": _SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": _SEC_CH_UA_PLATFORM,
    }
    resp = await session.post(_STRIPE_PM_URL, headers=headers, data=data, timeout=30, proxies=proxies)
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {"_raw": resp.text[:1000]}
    return resp.status_code, body, (guid, muid, sid)


async def _confirm_upi(session, *, cs, publishable_key, context, pm_id, ids, proxies):
    from user_agent_profile import (
        SEC_CH_UA as _SEC_CH_UA,
        SEC_CH_UA_MOBILE as _SEC_CH_UA_MOBILE,
        SEC_CH_UA_PLATFORM as _SEC_CH_UA_PLATFORM,
        WINDOWS_USER_AGENT as _UA,
    )
    guid, muid, sid = ids
    client_session_id = str(uuid.uuid4())
    return_url = (
        f"https://pay.openai.com/c/pay/{cs}"
        f"?redirect_pm_type=upi&lid={uuid.uuid4()}&ui_mode=hosted"
    )
    data = {
        "eid": context["eid"],
        "payment_method": pm_id,
        "expected_amount": context["expected_amount"],
        "expected_payment_method_type": "upi",
        "return_url": return_url,
        "_stripe_version": _STRIPE_VERSION,
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "key": publishable_key,
        "version": "922d612e68",
        "init_checksum": context["init_checksum"],
        "client_attribution_metadata[client_session_id]": client_session_id,
        "client_attribution_metadata[checkout_session_id]": cs,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "hosted_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[checkout_config_id]": context["config_id"],
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://pay.openai.com",
        "Referer": "https://pay.openai.com/",
        "User-Agent": _UA,
        "sec-ch-ua": _SEC_CH_UA,
        "sec-ch-ua-mobile": _SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": _SEC_CH_UA_PLATFORM,
    }
    url = _STRIPE_CONFIRM_URL_TPL.format(session_id=cs)
    resp = await session.post(url, headers=headers, data=data, timeout=30, proxies=proxies)
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {"_raw": resp.text[:2000]}
    return resp.status_code, body


async def main_async() -> int:
    account_line = os.environ.get("ACCOUNT_LINE", "").strip()
    access_token: str | None = None
    email = ""
    raw_proxy = os.environ.get("UPI_PROXY", "").strip() or None

    from curl_cffi.requests import AsyncSession
    from user_agent_profile import CURL_IMPERSONATE_PRIMARY as _IMP
    from random_profile import random_india_profile
    from payment_link import _call_stripe_init_data

    if account_line:
        email, password, secret = _parse_account(account_line)
        _log("setup", f"mode=LOGIN  email={email[:3]}***")
        from session_phase import get_session_pure_request, SessionError
        _log("1/5", "login pure-HTTP (DIRECT)...")
        try:
            session_data = await get_session_pure_request(
                email=email, password=password, secret=secret, proxy=None, log=lambda s: None,
            )
        except SessionError as exc:
            _log("1/5", f"login FAIL: {exc}")
            return 1
        access_token = session_data.get("accessToken")
        if not access_token:
            _log("1/5", "login OK nhưng thiếu accessToken")
            return 1
        _log("1/5", "login OK — có accessToken")
    else:
        tok = _load_latest_token()
        if not tok or not tok.get("access_token"):
            _log("setup", "THIẾU ACCOUNT_LINE và không tìm thấy token export → không chạy được.")
            _log("setup", "Chạy: ACCOUNT_LINE='email|pass|secret' UPI_PROXY='host:port:user:pass' python3 test/probe_hosted_upi.py")
            return 2
        access_token = tok["access_token"]
        email = tok.get("email") or "token@reuse"
        if raw_proxy is None and tok.get("proxy"):
            raw_proxy = tok["proxy"]
        _log("setup", f"mode=REUSE-TOKEN  email={email[:3]}***  exported_at={tok.get('exported_at')}")
        _log("1/5", "skip login — dùng access_token từ token export")

    proxy_url = _materialize_proxy(raw_proxy)
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    _log("setup", f"stripe proxy={'yes' if proxy_url else 'DIRECT'}")

    async with AsyncSession(impersonate=_IMP, proxies=proxies) as session:
        # ── Step 2 — checkout HOSTED (raw POST để xem full response) ──────
        _log("2/5", "POST checkout (checkout_ui_mode=hosted, region=IN) — RAW...")
        from user_agent_profile import (
            SEC_CH_UA as _SEC_CH_UA, SEC_CH_UA_MOBILE as _M,
            SEC_CH_UA_PLATFORM as _P, WINDOWS_USER_AGENT as _UA,
        )
        raw_body = {
            "entry_point": "all_plans_pricing_modal",
            "plan_name": "chatgptplusplan",
            "billing_details": {"country": "IN", "currency": "INR"},
            "checkout_ui_mode": "hosted",
            "promo_campaign": {
                "promo_campaign_id": "plus-1-month-free",
                "is_coupon_from_query_param": False,
            },
        }
        raw_headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/?promo_campaign=plus-1-month-free",
            "User-Agent": _UA,
            "sec-ch-ua": _SEC_CH_UA, "sec-ch-ua-mobile": _M, "sec-ch-ua-platform": _P,
            "x-openai-target-path": "/backend-api/payments/checkout",
            "x-openai-target-route": "/backend-api/payments/checkout",
            "OAI-Language": "en-IN",
        }
        resp = await session.post(
            "https://chatgpt.com/backend-api/payments/checkout",
            headers=raw_headers, json=raw_body, timeout=30,
        )
        try:
            raw_checkout = resp.json()
        except Exception:  # noqa: BLE001
            raw_checkout = {"_raw": resp.text[:1500]}
        _log("2/5", f"http={resp.status_code}  "
                    f"REQUESTED ui_mode=hosted  →  RETURNED ui_mode={raw_checkout.get('checkout_ui_mode')!r}")
        _dump("RAW checkout response (full)", raw_checkout)
        cs = raw_checkout.get("checkout_session_id")
        pk = raw_checkout.get("publishable_key")
        if not cs or not pk:
            _log("2/5", "thiếu cs/pk → stop")
            return 1
        _log("2/5", f"url field = {raw_checkout.get('url')!r}  (hosted mode mới có)")

        # ── Step 3 — Stripe payment_pages init ───────────────────────────
        _log("3/5", f"POST payment_pages/{cs[:18]}.../init ...")
        init_data = await _call_stripe_init_data(session, cs, pk)
        _log("3/5", f"payment_method_types={init_data.get('payment_method_types')}")
        eo = init_data.get("elements_options") or {}
        _log("3/5", f"amount={eo.get('amount') if isinstance(eo, dict) else '?'}  "
                     f"approval_method={init_data.get('approval_method')!r}  "
                     f"automatic_pmt={init_data.get('automatic_payment_method_types')!r}")
        _log("3/5", f"stripe_hosted_url={init_data.get('stripe_hosted_url')!r}  "
                     f"url={init_data.get('url')!r}")
        _log("3/5", f"config_id={'yes' if init_data.get('config_id') else 'NO'}  "
                     f"init_checksum={'yes' if init_data.get('init_checksum') else 'NO'}  "
                     f"eid={init_data.get('eid')!r}")
        pm_types = init_data.get("payment_method_types")
        if not (isinstance(pm_types, list) and "upi" in pm_types):
            _log("3/5", "⚠ hosted checkout KHÔNG expose 'upi'.")
            return 1
        context = {
            "config_id": init_data.get("config_id"),
            "init_checksum": init_data.get("init_checksum"),
            "eid": init_data.get("eid"),
            "expected_amount": str(eo.get("amount") if isinstance(eo, dict) else 0),
        }
        missing = [k for k, v in context.items() if not v and k != "expected_amount"]
        if missing:
            _log("3/5", f"⚠ init thiếu field cho confirm: {missing}")
            _dump("init full", init_data)
            return 1

        # ── Step 4 — create UPI payment_method ───────────────────────────
        _log("4/5", "POST payment_methods (type=upi, billing IN)...")
        profile = random_india_profile()
        pm_status, pm_body, ids = await _create_upi_payment_method(
            session, publishable_key=pk, profile=profile, email=email, proxies=proxies,
        )
        _log("4/5", f"http={pm_status}  pm_id={pm_body.get('id') if isinstance(pm_body, dict) else '?'}")
        if pm_status != 200 or not (isinstance(pm_body, dict) and pm_body.get("id")):
            _dump("payment_methods response", pm_body)
            return 1
        pm_id = pm_body["id"]

        # ── Step 5 — confirm UPI → next_action (QR) ──────────────────────
        _log("5/5", "POST payment_pages/{cs}/confirm (upi)...")
        cf_status, cf_body = await _confirm_upi(
            session, cs=cs, publishable_key=pk, context=context, pm_id=pm_id, ids=ids, proxies=proxies,
        )
        _log("5/5", f"http={cf_status}  keys={list(cf_body)[:25] if isinstance(cf_body, dict) else '?'}")
        _dump("confirm response", cf_body)

        # Highlight next_action / QR nếu có.
        if isinstance(cf_body, dict):
            na = cf_body.get("next_action") or (cf_body.get("payment_intent") or {}).get("next_action")
            if na:
                _dump(">>> next_action (QR ở đây)", na)
                _log("result", "✓ Có next_action — kiểm tra qr_code.image_url_png / hosted_instructions_url ở trên.")
            else:
                _log("result", "✗ KHÔNG có next_action trong confirm response.")
            payment_link = f"https://pay.openai.com/c/pay/{cs}"
            _log("result", f"payment link = {payment_link}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))
