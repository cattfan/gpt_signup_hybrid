"""UPI QR runner — reusable async function lấy QR cho 1 ChatGPT Plus IN account.

Tách từ ``test/probe_upi_qr.py`` để ``UpiJobManager`` (web UI) gọi cho từng
account. Logic giống probe nhưng:
    - Không in stdout / không tạo artifact JSON.
    - Trả dict result + log qua callback.
    - Hardcoded các knob theo yêu cầu UI:
        promo=True, proxy_from_step=3, do_confirm=True, do_approve=True,
        approve_delay=3.0, approve_proxy_batch=3,
        approve_backend_exception_fails=2,
        confirm_variants=("qr_code", "empty", "flow_qr", "intent")
    - Configurable: approve_retries (caller truyền vào).

Public:
    run_upi_qr_probe(...)           — entry point per-job
    UpiQrResult                     — dataclass kết quả
    UpiQrError                      — fatal error
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from time import monotonic
from typing import Any, Callable

# Hardcoded knobs — fix cứng theo spec UI (không expose ra Settings).
PROMO: bool = True
PROXY_FROM_STEP: int = 3
DO_CONFIRM: bool = True
DO_APPROVE: bool = True
APPROVE_DELAY: float = 3.0
APPROVE_PROXY_BATCH: int = 3
APPROVE_BACKEND_EXCEPTION_FAILS: int = 2
CONFIRM_VARIANTS: tuple[str, ...] = ("qr_code", "empty", "flow_qr", "intent")

LogFn = Callable[[str], None]


# ─────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────


@dataclass
class UpiQrResult:
    """Kết quả 1 lần probe — đủ để render UI (có QR file + summary)."""

    ok: bool
    email: str
    amount: int = 0
    return_url: str = ""
    checkout_session: str = ""
    qr_path: str | None = None       # absolute path tới PNG (None nếu render fail)
    qr_source: str | None = None     # "stripe_image" | "upi_uri" | "hosted_html"
    qr_source_url: str | None = None
    qr_reason: str | None = None     # nếu không có QR
    has_upi_uri: bool = False
    has_qr_image_url: bool = False
    confirm_attempts: list[dict[str, Any]] = field(default_factory=list)
    approve_attempts: list[dict[str, Any]] = field(default_factory=list)
    page_refresh_attempts: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    backend_exception_count: int = 0
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "email": self.email,
            "amount": self.amount,
            "return_url": self.return_url,
            "checkout_session": self.checkout_session,
            "qr_path": self.qr_path,
            "qr_source": self.qr_source,
            "qr_source_url": self.qr_source_url,
            "qr_reason": self.qr_reason,
            "has_upi_uri": self.has_upi_uri,
            "has_qr_image_url": self.has_qr_image_url,
            "confirm_attempts": self.confirm_attempts,
            "approve_attempts": self.approve_attempts,
            "page_refresh_attempts": self.page_refresh_attempts,
            "error": self.error,
            "backend_exception_count": self.backend_exception_count,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
        }


class UpiQrError(Exception):
    """Fatal error trong flow probe (login fail, no free offer, approve threshold...)."""


# ─────────────────────────────────────────────────────────────────────
# Constants & helpers (giữ nguyên semantics từ probe)
# ─────────────────────────────────────────────────────────────────────

_MATCH_TERMS = (
    "qr",
    "upi",
    "intent",
    "collect",
    "vpa",
    "next_action",
    "hosted_instructions",
    "image_url",
    "display_qr",
)
_SENSITIVE_PATH_TERMS = (
    "access",
    "authorization",
    "client_secret",
    "cookie",
    "key",
    "password",
    "secret",
    "token",
)


def _mask_email(email: str) -> str:
    local, sep, domain = email.partition("@")
    if not sep:
        return "***"
    if len(local) <= 3:
        return f"{local[:1]}***@{domain}"
    return f"{local[:3]}***{local[-2:]}@{domain}"


def _mask_proxy(proxy: str | None) -> str:
    if not proxy:
        return "direct"
    if "@" not in proxy:
        return proxy
    scheme, sep, rest = proxy.partition("://")
    host_part = rest.rsplit("@", 1)[-1]
    return f"{scheme}://***@{host_part}" if sep else "***@" + host_part


def _proxy_dict(proxy: str | None) -> dict[str, str] | None:
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _proxy_for_step(proxy: str | None, *, from_step: int, step: int) -> dict[str, str] | None:
    if proxy and step >= from_step:
        return _proxy_dict(proxy)
    return None


def _proxy_url_for_retry(
    proxies: list[str],
    *,
    from_step: int,
    step: int,
    attempt: int,
    per_proxy_attempts: int,
) -> str | None:
    if step < from_step or not proxies:
        return None
    proxy_index = ((attempt - 1) // per_proxy_attempts) % len(proxies)
    return proxies[proxy_index]


def _is_sensitive_path(path: str) -> bool:
    lower = path.lower()
    return any(term in lower for term in _SENSITIVE_PATH_TERMS)


def _short_value(value: Any, path: str) -> Any:
    if _is_sensitive_path(path):
        return "[redacted]"
    if not isinstance(value, str):
        return value
    if len(value) <= 500:
        return value
    return f"{value[:260]}...{value[-120:]}"


def _find_matches(value: Any, *, source: str, path: str = "$") -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}"
            key_lower = str(key).lower()
            if any(term in key_lower for term in _MATCH_TERMS):
                matches.append({
                    "source": source,
                    "path": child_path,
                    "kind": "key",
                    "value": _short_value(item, child_path),
                })
            matches.extend(_find_matches(item, source=source, path=child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            matches.extend(_find_matches(item, source=source, path=f"{path}[{index}]"))
    elif isinstance(value, str):
        value_lower = value.lower()
        if any(term in value_lower for term in _MATCH_TERMS):
            matches.append({
                "source": source,
                "path": path,
                "kind": "value",
                "value": _short_value(value, path),
            })
    return matches


def _find_upi_uri(matches: list[dict[str, Any]]) -> str | None:
    for match in matches:
        value = match.get("value")
        if isinstance(value, str) and value.lower().startswith("upi://"):
            return value
    return None


def _find_qr_image_url(matches: list[dict[str, Any]]) -> str | None:
    for match in matches:
        value = match.get("value")
        path = str(match.get("path") or "").lower()
        if (
            isinstance(value, str)
            and value.startswith("https://")
            and "qr" in path
            and (value.endswith(".png") or value.endswith(".svg") or "qr" in value.lower())
        ):
            return value
    return None


class _PayloadMetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.payload_message: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta":
            return
        values = {key.lower(): value for key, value in attrs if value is not None}
        if values.get("id") == "payload":
            self.payload_message = values.get("data-message")


def _extract_hosted_instruction_upi_uri(html_text: str) -> str | None:
    parser = _PayloadMetaParser()
    parser.feed(html_text)
    message = parser.payload_message
    if not message:
        return None
    padded = message + ("=" * (-len(message) % 4))
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except Exception:
        return None
    uri = payload.get("mobile_auth_url") if isinstance(payload, dict) else None
    return uri if isinstance(uri, str) and uri.startswith("upi:") else None


def _redact_error(error: Any) -> Any:
    if not isinstance(error, dict):
        return str(error)[:500]
    allowed = {}
    for key in ("type", "code", "decline_code", "message", "param", "payment_intent"):
        if key in error:
            allowed[key] = _short_value(error.get(key), f"error.{key}")
    return allowed


def _upi_payload_for_variant(variant: str) -> dict[str, Any]:
    if variant == "flow_qr":
        return {"flow": "qr_code"}
    if variant == "qr_code":
        return {"qr_code": {}}
    if variant == "intent":
        return {"intent": "qr_code"}
    return {}


def _stripe_return_url(session_id: str) -> str:
    return f"https://checkout.stripe.com/c/pay/{session_id}"


def _extract_amount(init_data: dict[str, Any]) -> int:
    elements_options = init_data.get("elements_options")
    if isinstance(elements_options, dict) and isinstance(elements_options.get("amount"), int):
        return elements_options["amount"]
    total_summary = init_data.get("total_summary")
    if isinstance(total_summary, dict):
        for key in ("due", "total"):
            value = total_summary.get(key)
            if isinstance(value, int):
                return value
    invoice = init_data.get("invoice")
    if isinstance(invoice, dict):
        for key in ("amount_due", "total"):
            value = invoice.get(key)
            if isinstance(value, int):
                return value
    value = init_data.get("amount_total")
    return value if isinstance(value, int) else 0


def _render_qr_png(payload: str, out_path: Path) -> None:
    """Render UPI URI thành PNG. Raise nếu qrcode chưa cài."""
    import qrcode  # type: ignore[import-untyped]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    image = qrcode.make(payload)
    image.save(out_path)


def _summarize_confirm(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: a.get(key) for key in ("variant", "http_status", "ok", "keys", "error")}
        for a in attempts
    ]


def _summarize_approve(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: a.get(key)
            for key in (
                "variant", "attempt", "proxy", "http_status", "ok",
                "result", "error_type", "error", "keys",
            )
        }
        for a in attempts
    ]


def _summarize_refresh(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: a.get(key)
            for key in (
                "attempt", "proxy", "http_status", "ok", "error_type", "error", "keys",
            )
        }
        for a in attempts
    ]


# ─────────────────────────────────────────────────────────────────────
# Stripe / ChatGPT calls (clone từ probe — KHÔNG dùng pay_upi_http chính
# để tách dependency build_token_fields khỏi flow chính, đồng thời giữ
# variant logic riêng cho QR mode).
# ─────────────────────────────────────────────────────────────────────


async def _create_chatgpt_checkout(
    sess: Any,
    *,
    access_token: str,
    log: LogFn,
    proxies: dict[str, str] | None,
) -> dict[str, Any]:
    from ..pay_upi_http import _CHATGPT_CHECKOUT_URL, _USER_AGENT, PayUpiError

    body: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": "IN", "currency": "INR"},
        "checkout_ui_mode": "custom",
    }
    referer = "https://chatgpt.com/?promo_campaign=plus-1-month-free"
    body["promo_campaign"] = {
        "promo_campaign_id": "plus-1-month-free",
        "is_coupon_from_query_param": False,
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Origin": "https://chatgpt.com",
        "Referer": referer,
        "User-Agent": _USER_AGENT,
        "x-openai-target-path": "/backend-api/payments/checkout",
        "x-openai-target-route": "/backend-api/payments/checkout",
        "OAI-Language": "en-IN",
    }
    log(f"  [2/6] POST /backend-api/payments/checkout promo={PROMO} proxy={'yes' if proxies else 'no'}")
    resp = await sess.post(
        _CHATGPT_CHECKOUT_URL, headers=headers, json=body, timeout=30, proxies=proxies,
    )
    if resp.status_code != 200:
        raise PayUpiError(f"checkout HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    needed = ("checkout_session_id", "publishable_key")
    miss = [key for key in needed if not data.get(key)]
    if miss:
        raise PayUpiError(f"checkout response missing {miss}: {data}")
    log(f"        ok cs={str(data['checkout_session_id'])[:18]}...")
    return data


async def _stripe_elements_session(
    sess: Any,
    *,
    session_id: str,
    publishable_key: str,
    stripe_js_id: str,
    amount: int,
    log: LogFn,
    proxies: dict[str, str] | None,
) -> dict[str, Any]:
    from ..pay_upi_http import (
        _STRIPE_ELEMENTS_URL, _STRIPE_VERSION, _USER_AGENT, PayUpiError,
    )

    params = {
        "client_betas[0]": "custom_checkout_server_updates_1",
        "client_betas[1]": "custom_checkout_manual_approval_1",
        "deferred_intent[mode]": "subscription",
        "deferred_intent[amount]": str(amount),
        "deferred_intent[currency]": "inr",
        "deferred_intent[setup_future_usage]": "off_session",
        "deferred_intent[payment_method_types][0]": "card",
        "deferred_intent[payment_method_types][1]": "link",
        "deferred_intent[payment_method_types][2]": "upi",
        "currency": "inr",
        "key": publishable_key,
        "_stripe_version": _STRIPE_VERSION,
        "elements_init_source": "custom_checkout",
        "referrer_host": "chatgpt.com",
        "stripe_js_id": stripe_js_id,
        "locale": "en",
        "type": "deferred_intent",
        "checkout_session_id": session_id,
    }
    headers = {
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
        "User-Agent": _USER_AGENT,
        "Accept-Language": "en-IN,en;q=0.9",
    }
    log(f"  [4/6] GET /v1/elements/sessions amount={amount} proxy={'yes' if proxies else 'no'}")
    resp = await sess.get(
        _STRIPE_ELEMENTS_URL, headers=headers, params=params, timeout=30, proxies=proxies,
    )
    if resp.status_code != 200:
        raise PayUpiError(f"elements/sessions HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    if not data.get("session_id"):
        raise PayUpiError(f"elements/sessions missing session_id: keys={list(data)[:20]}")
    log(f"        ok elements_session={str(data['session_id'])[:22]}...")
    return data


async def _stripe_confirm_upi_qr(
    sess: Any,
    *,
    session_id: str,
    publishable_key: str,
    stripe_js_id: str,
    init_data: dict[str, Any],
    elements_data: dict[str, Any],
    profile: dict[str, Any],
    email: str,
    amount: int,
    variant: str,
    log: LogFn,
    token_config: Any | None,
    proxies: dict[str, str] | None,
) -> dict[str, Any]:
    from ..pay_upi_http import (
        _STRIPE_CONFIRM_URL, _STRIPE_VERSION, _USER_AGENT,
        _stripe_guid, _to_form,
    )

    elements_session_id = elements_data.get("session_id")
    elements_session_config_id = elements_data.get("config_id") or ""
    init_config_id = init_data.get("config_id") or ""
    ppage_id = init_data.get("id") or ""
    init_checksum = init_data["init_checksum"]

    if token_config is not None:
        from .. import stripe_token as _st

        tokens = _st.build_token_fields(ppage_id=ppage_id, config=token_config)
        js_checksum = tokens["js_checksum"]
        rv_timestamp = tokens["rv_timestamp"]
    else:
        js_checksum = None
        rv_timestamp = None

    client_attribution_metadata = {
        "checkout_config_id": init_config_id,
        "checkout_session_id": session_id,
        "client_session_id": stripe_js_id,
        "elements_session_config_id": elements_session_config_id,
        "elements_session_id": elements_session_id,
        "merchant_integration_additional_elements": [
            "expressCheckout", "payment", "address",
        ],
        "merchant_integration_source": "checkout",
        "merchant_integration_subtype": "payment-element",
        "merchant_integration_version": "custom",
        "payment_intent_creation_flow": "deferred",
        "payment_method_selection_flow": "merchant_specified",
    }
    pmd_client_attribution = dict(client_attribution_metadata)
    pmd_client_attribution["merchant_integration_source"] = "elements"
    pmd_client_attribution["merchant_integration_version"] = "2021"

    form = _to_form({
        "_stripe_version": _STRIPE_VERSION,
        "client_attribution_metadata": client_attribution_metadata,
        "elements_options_client": {
            "saved_payment_method": {"enable_redisplay": "auto", "enable_save": "auto"},
        },
        "elements_session_client": {
            "client_betas": [
                "custom_checkout_server_updates_1", "custom_checkout_manual_approval_1",
            ],
            "elements_init_source": "custom_checkout",
            "is_aggregation_expected": "false",
            "locale": "en",
            "referrer_host": "chatgpt.com",
            "session_id": elements_session_id,
            "stripe_js_id": stripe_js_id,
        },
        "expected_amount": amount,
        "expected_payment_method_type": "upi",
        "guid": _stripe_guid(),
        "init_checksum": init_checksum,
        "js_checksum": js_checksum,
        "rv_timestamp": rv_timestamp,
        "passive_captcha_ekey": None,
        "passive_captcha_token": None,
        "key": publishable_key,
        "muid": _stripe_guid(),
        "sid": _stripe_guid(),
        "payment_method_data": {
            "billing_details": {
                "address": {
                    "city": profile["city"],
                    "country": "IN",
                    "line1": profile["address_line1"],
                    "postal_code": profile["postal_code"],
                    "state": profile["state"],
                },
                "email": email,
                "name": profile["name"],
            },
            "client_attribution_metadata": pmd_client_attribution,
            "payment_user_agent": (
                "stripe.js/e5ebd5e1e6; stripe-js-v3/e5ebd5e1e6; "
                "payment-element; deferred-intent"
            ),
            "referrer": "https://chatgpt.com",
            "time_on_page": int(time.time() * 1000) % 100000,
            "type": "upi",
            "upi": _upi_payload_for_variant(variant),
        },
        "return_url": _stripe_return_url(session_id),
        "version": "e5ebd5e1e6",
    })
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
        "User-Agent": _USER_AGENT,
        "Accept-Language": "en-IN,en;q=0.9",
    }
    log(f"  [5/6] POST /v1/payment_pages/{{cs}}/confirm variant={variant}")
    resp = await sess.post(
        _STRIPE_CONFIRM_URL.format(id=session_id),
        headers=headers, data=form, timeout=30, proxies=proxies,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"_raw": (resp.text or "")[:1000]}
    return {
        "variant": variant,
        "http_status": resp.status_code,
        "ok": resp.status_code == 200,
        "keys": list(data)[:30] if isinstance(data, dict) else [],
        "error": _redact_error(data.get("error")) if isinstance(data, dict) and data.get("error") else None,
        "data": data if resp.status_code == 200 else None,
    }


async def _stripe_payment_page_refresh(
    sess: Any,
    *,
    session_id: str,
    publishable_key: str,
    stripe_js_id: str,
    elements_data: dict[str, Any],
    log: LogFn,
    proxies: dict[str, str] | None,
) -> dict[str, Any]:
    from ..pay_upi_http import (
        _STRIPE_PAGE_URL, _STRIPE_VERSION, _USER_AGENT, _to_form,
    )

    params = _to_form({
        "elements_session_client": {
            "client_betas": [
                "custom_checkout_server_updates_1", "custom_checkout_manual_approval_1",
            ],
            "elements_init_source": "custom_checkout",
            "referrer_host": "chatgpt.com",
            "stripe_js_id": stripe_js_id,
            "locale": "en",
            "is_aggregation_expected": "false",
            "session_id": elements_data.get("session_id") or "",
        },
        "elements_options_client": {
            "saved_payment_method": {"enable_save": "auto", "enable_redisplay": "auto"},
        },
        "key": publishable_key,
        "_stripe_version": _STRIPE_VERSION,
    })
    headers = {
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
        "User-Agent": _USER_AGENT,
        "Accept-Language": "en-IN,en;q=0.9",
    }
    log("  [5r/6] GET /v1/payment_pages/{cs} refresh")
    resp = await sess.get(
        _STRIPE_PAGE_URL.format(id=session_id),
        headers=headers, params=params, timeout=30, proxies=proxies,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"_raw": (resp.text or "")[:1000]}
    return {
        "http_status": resp.status_code,
        "ok": resp.status_code == 200,
        "keys": list(data)[:30] if isinstance(data, dict) else [],
        "error": _redact_error(data.get("error")) if isinstance(data, dict) and data.get("error") else None,
        "data": data if resp.status_code == 200 else None,
    }


async def _stripe_payment_page_refresh_retry(
    sess: Any,
    *,
    session_id: str,
    publishable_key: str,
    stripe_js_id: str,
    elements_data: dict[str, Any],
    log: LogFn,
    proxy_pool: list[str],
) -> dict[str, Any]:
    candidates = proxy_pool if proxy_pool else [None]
    last_attempt: dict[str, Any] | None = None
    for index, proxy_url in enumerate(candidates, start=1):
        log(f"        refresh attempt {index}/{len(candidates)} proxy={_mask_proxy(proxy_url)}")
        try:
            attempt = await _stripe_payment_page_refresh(
                sess,
                session_id=session_id,
                publishable_key=publishable_key,
                stripe_js_id=stripe_js_id,
                elements_data=elements_data,
                log=log,
                proxies=_proxy_dict(proxy_url),
            )
        except Exception as exc:  # noqa: BLE001
            attempt = {
                "http_status": None,
                "ok": False,
                "keys": [],
                "error_type": type(exc).__name__,
                "error": str(exc)[:300],
                "data": None,
            }
        attempt["proxy"] = _mask_proxy(proxy_url)
        attempt["attempt"] = index
        last_attempt = attempt
        if attempt.get("ok"):
            return attempt
    return last_attempt or {
        "http_status": None,
        "ok": False,
        "keys": [],
        "error_type": "NoRefreshAttempt",
        "error": "no proxy candidates available",
        "data": None,
    }


async def _download_qr_image(
    sess: Any,
    *,
    url: str,
    out_path: Path,
    proxies: dict[str, str] | None,
) -> dict[str, Any]:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        resp = await sess.get(url, timeout=30, proxies=proxies)
    except Exception as exc:  # noqa: BLE001
        return {
            "downloaded": False,
            "error_type": type(exc).__name__,
            "error": str(exc)[:300],
        }
    if resp.status_code != 200:
        return {"downloaded": False, "status": resp.status_code}
    content_type = str(resp.headers.get("content-type") or "").lower()
    content = resp.content
    looks_like_html = "text/html" in content_type or content.lstrip().lower().startswith(b"<html")
    if looks_like_html:
        html_path = out_path.with_suffix(".html")
        html_path.write_bytes(content)
        html_text = content.decode("utf-8", errors="replace")
        upi_uri = _extract_hosted_instruction_upi_uri(html_text)
        if not upi_uri:
            return {
                "downloaded": False,
                "rendered": False,
                "reason": "hosted instructions HTML did not contain mobile_auth_url",
                "html_path": str(html_path),
            }
        _render_qr_png(upi_uri, out_path)
        result = {
            "downloaded": False,
            "rendered": True,
            "path": str(out_path),
            "source": "hosted_instructions_html",
            "html_path": str(html_path),
        }
        if out_path.exists():
            result["bytes"] = out_path.stat().st_size
        return result

    out_path.write_bytes(content)
    return {
        "downloaded": True,
        "rendered": True,
        "path": str(out_path),
        "bytes": len(content),
    }


async def _chatgpt_approve_checkout(
    sess: Any,
    *,
    access_token: str,
    session_id: str,
    log: LogFn,
    proxies: dict[str, str] | None,
) -> dict[str, Any]:
    from ..pay_upi_http import _CHATGPT_APPROVE_URL, _USER_AGENT

    body = {"checkout_session_id": session_id, "processor_entity": "openai_llc"}
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Origin": "https://chatgpt.com",
        "Referer": f"https://chatgpt.com/checkout/openai_llc/{session_id}",
        "User-Agent": _USER_AGENT,
        "x-openai-target-path": "/backend-api/payments/checkout/approve",
        "x-openai-target-route": "/backend-api/payments/checkout/approve",
        "OAI-Language": "en-IN",
    }
    log(f"  [6/6] POST /backend-api/payments/checkout/approve proxy={'yes' if proxies else 'no'}")
    resp = await sess.post(
        _CHATGPT_APPROVE_URL, headers=headers, json=body, timeout=30, proxies=proxies,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"_raw": (resp.text or "")[:1000]}
    result = data.get("result") if isinstance(data, dict) else None
    return {
        "http_status": resp.status_code,
        "ok": resp.status_code == 200 and result == "approved",
        "result": result,
        "keys": list(data)[:30] if isinstance(data, dict) else [],
        "data": data if resp.status_code == 200 else None,
    }


def _is_backend_exception(attempt: dict[str, Any]) -> bool:
    return attempt.get("http_status") == 200 and attempt.get("result") == "exception"


# ─────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────


async def run_upi_qr_probe(
    *,
    email: str,
    password: str,
    secret: str | None,
    proxy_pool: list[str],
    approve_retries: int,
    qr_out_path: Path,
    log: LogFn,
    db_path: str | None = None,  # noqa: ARG001 — proxy pool truyền trực tiếp
) -> UpiQrResult:
    """Login + checkout + confirm UPI + approve loop → save QR PNG.

    Args:
        email/password/secret: ChatGPT credentials. ``secret`` = TOTP secret nếu
            account có 2FA.
        proxy_pool: list proxy URL (đã normalize) để xoay vòng. Empty = direct.
        approve_retries: số lần retry approve (>=1).
        qr_out_path: PNG file path để lưu QR (sẽ tự tạo parent dir).
        log: callable(str) — mỗi dòng log gọi callback này.

    Returns:
        UpiQrResult — luôn trả (kể cả khi fail), KHÔNG raise. Caller check
        ``result.ok`` để biết success.
    """
    if approve_retries < 1:
        raise UpiQrError(f"approve_retries phải >= 1, got {approve_retries}")

    started = monotonic()
    masked_email = _mask_email(email)
    masked_proxy_pool = [_mask_proxy(p) for p in proxy_pool]
    first_proxy = proxy_pool[0] if proxy_pool else None
    masked_first_proxy = _mask_proxy(first_proxy)

    def _safe_log(msg: str) -> None:
        # Mask email + proxy trước khi log để không leak credential vào job log.
        safe = msg.replace(email, masked_email)
        for raw, masked in zip(proxy_pool, masked_proxy_pool):
            safe = safe.replace(raw, masked)
        log(safe)

    _safe_log(f"[upi-qr] account={masked_email}")
    _safe_log(f"[upi-qr] proxy_pool={len(proxy_pool)} proxies first={masked_first_proxy}")
    _safe_log(f"[upi-qr] proxy_from_step={PROXY_FROM_STEP} promo={PROMO}")
    _safe_log(f"[upi-qr] confirm={DO_CONFIRM} approve={DO_APPROVE}")
    _safe_log(
        f"[upi-qr] approve_retries={approve_retries} delay={APPROVE_DELAY:g}s "
        f"proxy_batch={APPROVE_PROXY_BATCH} backend_excpt_threshold={APPROVE_BACKEND_EXCEPTION_FAILS}"
    )
    _safe_log(f"[upi-qr] confirm_variants={list(CONFIRM_VARIANTS)}")

    # Lazy import → chỉ khi job thật sự chạy.
    from curl_cffi.requests import AsyncSession
    from .. import stripe_token as _st
    from ..pay_upi_http import _stripe_init
    from ..random_profile import random_india_profile
    from ..session_phase import get_session_pure_request

    # Step 1 — login (DIRECT: no proxy, để giảm captcha trên ChatGPT).
    session_data = await get_session_pure_request(
        email=email,
        password=password,
        secret=secret,
        proxy=None,
        log=_safe_log,
    )
    access_token = session_data.get("accessToken")
    if not isinstance(access_token, str) or not access_token:
        return UpiQrResult(
            ok=False, email=masked_email,
            error="login OK nhưng không có accessToken",
            elapsed_seconds=monotonic() - started,
        )

    stripe_js_id = str(uuid.uuid4())
    confirm_attempts: list[dict[str, Any]] = []
    approve_attempts: list[dict[str, Any]] = []
    page_refresh_attempts: list[dict[str, Any]] = []
    backend_exception_count = 0
    fatal_approve_error: str | None = None
    amount = 0
    return_url = ""
    session_id = ""
    qr_image_url: str | None = None
    upi_uri: str | None = None

    async with AsyncSession(impersonate="chrome136") as sess:
        # Step 2 — checkout creation (DIRECT - chatgpt API).
        checkout = await _create_chatgpt_checkout(
            sess, access_token=access_token, log=_safe_log,
            proxies=_proxy_dict(first_proxy if PROXY_FROM_STEP <= 2 else None),
        )
        session_id = checkout["checkout_session_id"]
        return_url = _stripe_return_url(session_id)
        publishable_key = checkout["publishable_key"]

        # Step 3 — Stripe init.
        init_data = await _stripe_init(
            sess,
            session_id=session_id,
            publishable_key=publishable_key,
            stripe_js_id=stripe_js_id,
            log=_safe_log,
            proxies=_proxy_for_step(first_proxy, from_step=PROXY_FROM_STEP, step=3),
        )
        amount = _extract_amount(init_data)
        _safe_log(f"[upi-qr] amount={amount}")
        if PROMO and amount > 0:
            return UpiQrResult(
                ok=False, email=masked_email, amount=amount, return_url=return_url,
                checkout_session=str(session_id)[:18] + "...",
                error="no free offer (promo enabled but amount > 0)",
                elapsed_seconds=monotonic() - started,
            )

        # Step 4 — elements/sessions.
        elements_data = await _stripe_elements_session(
            sess,
            session_id=session_id,
            publishable_key=publishable_key,
            stripe_js_id=stripe_js_id,
            amount=amount,
            log=_safe_log,
            proxies=_proxy_for_step(first_proxy, from_step=PROXY_FROM_STEP, step=4),
        )

        # Step 5 — extract Stripe token config (best-effort).
        token_config = None
        try:
            token_config = await _st.extract_config_live(
                sess, log=_safe_log, use_cache=True,
                fallback_dir=Path(__file__).resolve().parents[1]
                / "runtime" / "cache" / "stripe_bundles_default",
                proxies=None,
            )
            _safe_log("[upi-qr] token_config=ok")
        except _st.StripeTokenExtractError as exc:
            _safe_log(f"[upi-qr] token_config=fail {str(exc)[:180]}")

        # Step 5 — confirm variants.
        profile = random_india_profile()
        for variant in CONFIRM_VARIANTS:
            attempt = await _stripe_confirm_upi_qr(
                sess,
                session_id=session_id,
                publishable_key=publishable_key,
                stripe_js_id=stripe_js_id,
                init_data=init_data,
                elements_data=elements_data,
                profile=profile,
                email=email,
                amount=amount,
                variant=variant,
                log=_safe_log,
                token_config=token_config,
                proxies=_proxy_for_step(first_proxy, from_step=PROXY_FROM_STEP, step=5),
            )
            confirm_attempts.append(attempt)
            if not attempt.get("ok"):
                continue

            # Confirm OK → refresh + approve loop.
            page_refresh_attempts.append(
                await _stripe_payment_page_refresh_retry(
                    sess,
                    session_id=session_id,
                    publishable_key=publishable_key,
                    stripe_js_id=stripe_js_id,
                    elements_data=elements_data,
                    log=_safe_log,
                    proxy_pool=proxy_pool if PROXY_FROM_STEP <= 5 else [],
                )
            )

            approved = False
            for approve_index in range(1, approve_retries + 1):
                approve_proxy = _proxy_url_for_retry(
                    proxy_pool,
                    from_step=PROXY_FROM_STEP,
                    step=6,
                    attempt=approve_index,
                    per_proxy_attempts=APPROVE_PROXY_BATCH,
                )
                _safe_log(
                    f"        approve attempt {approve_index}/{approve_retries} "
                    f"proxy={_mask_proxy(approve_proxy)}"
                )
                try:
                    approve_attempt = await _chatgpt_approve_checkout(
                        sess,
                        access_token=access_token,
                        session_id=session_id,
                        log=_safe_log,
                        proxies=_proxy_dict(approve_proxy),
                    )
                except Exception as exc:  # noqa: BLE001
                    approve_attempt = {
                        "http_status": None,
                        "ok": False,
                        "result": None,
                        "keys": [],
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:300],
                        "data": None,
                    }
                approve_attempt["variant"] = variant
                approve_attempt["attempt"] = approve_index
                approve_attempt["proxy"] = _mask_proxy(approve_proxy)
                approve_attempts.append(approve_attempt)
                _safe_log(
                    f"        approve result={approve_attempt.get('result') or approve_attempt.get('error_type') or 'unknown'} "
                    f"http={approve_attempt.get('http_status')}"
                )
                if approve_attempt.get("ok"):
                    approved = True
                    break
                if _is_backend_exception(approve_attempt):
                    backend_exception_count += 1
                    if backend_exception_count >= APPROVE_BACKEND_EXCEPTION_FAILS:
                        fatal_approve_error = (
                            f"approve backend_exception threshold "
                            f"({backend_exception_count}/{APPROVE_BACKEND_EXCEPTION_FAILS})"
                        )
                        _safe_log(f"        approve fail={fatal_approve_error}")
                        break
                if approve_index < approve_retries:
                    await asyncio.sleep(APPROVE_DELAY)

            if not fatal_approve_error and (approved or approve_attempts):
                page_refresh_attempts.append(
                    await _stripe_payment_page_refresh_retry(
                        sess,
                        session_id=session_id,
                        publishable_key=publishable_key,
                        stripe_js_id=stripe_js_id,
                        elements_data=elements_data,
                        log=_safe_log,
                        proxy_pool=proxy_pool if PROXY_FROM_STEP <= 5 else [],
                    )
                )
            break  # variant đầu tiên confirm OK → dừng vòng variants.

        if fatal_approve_error:
            return UpiQrResult(
                ok=False, email=masked_email, amount=amount, return_url=return_url,
                checkout_session=str(session_id)[:18] + "...",
                error=fatal_approve_error,
                backend_exception_count=backend_exception_count,
                confirm_attempts=_summarize_confirm(confirm_attempts),
                approve_attempts=_summarize_approve(approve_attempts),
                page_refresh_attempts=_summarize_refresh(page_refresh_attempts),
                elapsed_seconds=monotonic() - started,
            )

        # Aggregate matches từ mọi response.
        matches: list[dict[str, Any]] = []
        matches.extend(_find_matches(checkout, source="chatgpt_checkout"))
        matches.extend(_find_matches(init_data, source="stripe_init"))
        matches.extend(_find_matches(elements_data, source="stripe_elements"))
        for attempt in confirm_attempts:
            if attempt.get("data") is not None:
                matches.extend(_find_matches(attempt["data"], source=f"confirm:{attempt['variant']}"))
        for attempt in approve_attempts:
            if attempt.get("data") is not None:
                matches.extend(_find_matches(attempt["data"], source=f"approve:{attempt['variant']}"))
        for index, attempt in enumerate(page_refresh_attempts, start=1):
            if attempt.get("data") is not None:
                matches.extend(_find_matches(attempt["data"], source=f"payment_page_refresh:{index}"))
        upi_uri = _find_upi_uri(matches)
        qr_image_url = _find_qr_image_url(matches)

        # QR rendering (download Stripe image hoặc render từ upi:// URI).
        qr_path: str | None = None
        qr_source: str | None = None
        qr_reason: str | None = None
        if qr_image_url:
            extension = ".svg" if qr_image_url.lower().endswith(".svg") else ".png"
            target = qr_out_path.with_suffix(extension)
            qr_dl = await _download_qr_image(
                sess, url=qr_image_url, out_path=target,
                proxies=_proxy_for_step(first_proxy, from_step=PROXY_FROM_STEP, step=5),
            )
            if qr_dl.get("rendered") and qr_dl.get("path"):
                qr_path = qr_dl["path"]
                qr_source = qr_dl.get("source") or "stripe_image"
            else:
                qr_reason = qr_dl.get("reason") or qr_dl.get("error") or "stripe image download fail"
        elif upi_uri:
            try:
                _render_qr_png(upi_uri, qr_out_path)
                qr_path = str(qr_out_path)
                qr_source = "upi_uri"
            except Exception as exc:  # noqa: BLE001
                qr_reason = f"qrcode render fail: {type(exc).__name__}: {exc}"
        else:
            qr_reason = "no upi:// URI or QR image URL found in any response"

    return UpiQrResult(
        ok=True,
        email=masked_email,
        amount=amount,
        return_url=return_url,
        checkout_session=str(session_id)[:18] + "...",
        qr_path=qr_path,
        qr_source=qr_source,
        qr_source_url=qr_image_url,
        qr_reason=qr_reason,
        has_upi_uri=bool(upi_uri),
        has_qr_image_url=bool(qr_image_url),
        confirm_attempts=_summarize_confirm(confirm_attempts),
        approve_attempts=_summarize_approve(approve_attempts),
        page_refresh_attempts=_summarize_refresh(page_refresh_attempts),
        backend_exception_count=backend_exception_count,
        elapsed_seconds=monotonic() - started,
    )


__all__ = [
    "PROMO", "PROXY_FROM_STEP", "DO_CONFIRM", "DO_APPROVE",
    "APPROVE_DELAY", "APPROVE_PROXY_BATCH",
    "APPROVE_BACKEND_EXCEPTION_FAILS", "CONFIRM_VARIANTS",
    "UpiQrResult", "UpiQrError", "run_upi_qr_probe",
]
