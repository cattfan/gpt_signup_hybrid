"""UPI QR runner — reusable async function lấy QR cho 1 ChatGPT Plus IN account.

Tách từ ``test/probe_upi_qr.py`` để ``UpiJobManager`` (web UI) gọi cho từng
account. Logic giống probe nhưng:
    - Không in stdout / không tạo artifact JSON.
    - Trả dict result + log qua callback.
    - Hardcoded các knob theo yêu cầu UI:
        promo=True, proxy_from_step=3, do_confirm=True, do_approve=True,
        approve_delay=3.0, approve_proxy_batch=3,
        approve_backend_exception_consecutive=5  (LIÊN TIẾP, reset khi gặp
            non-exception result — tránh dừng sớm vì server flaky),
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
# Số lần `result=exception http=200` LIÊN TIẾP để dừng. Reset về 0 khi gặp
# bất kỳ result khác (approved/blocked/unknown/error_type/...). Server đôi khi
# flaky trả exception 1-2 lần rồi tự khỏi — đếm tổng (như cũ) sẽ làm job dừng
# sớm dù user cấu hình approve_retries cao.
# Set 0 để disable hoàn toàn (chỉ dừng khi approved hoặc hết retry).
APPROVE_BACKEND_EXCEPTION_CONSECUTIVE: int = 15
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
    qr_expires_at: int | None = None  # unix seconds — QR hết hạn lúc này (None nếu không rõ)
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
            "qr_expires_at": self.qr_expires_at,
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


def _find_qr_expires_at(matches: list[dict[str, Any]]) -> int | None:
    """Tìm `expires_at` (unix seconds) của QR trong next_action.

    Stripe trả object ``qr_code: {expires_at, image_url_png, image_url_svg}``
    trong ``next_action.upi_handle_redirect_or_display_qr_code``. ``_find_matches``
    bắt key ``qr_code`` (match term "qr") với value là cả dict → đọc trực tiếp.
    """
    for match in matches:
        value = match.get("value")
        if not isinstance(value, dict):
            continue
        expires_at = value.get("expires_at")
        if (
            isinstance(expires_at, int)
            and not isinstance(expires_at, bool)
            and expires_at > 0
            and ("image_url_png" in value or "image_url_svg" in value)
        ):
            return expires_at
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
# Log formatters — 1 dòng / bước, cột căn đều + icon status.
#
# Format chuẩn:
#   "[step] label[16ch] icon detail"
#
# Vd:
#   [2/6] checkout         ✓  cs=cs_live_a1gMta…  ui=custom
#   [6/6] approve loop     ▸  retries=500 delay=3s batch=3
#         attempt 001/500  ✗  http=403  unknown    proxy=103.116.38.17:8003
# ─────────────────────────────────────────────────────────────────────

_STEP_WIDTH = 5     # "[N/6]"
_LABEL_WIDTH = 16   # đủ chứa "confirm flow_qr" (15)
_STATUS_ICONS: dict[str, str] = {
    "ok": "✓",
    "fail": "✗",
    "warn": "⚠",
    "blocked": "⊘",
    "start": "▸",
    "retry": "↻",
    "info": "·",
    "skip": "—",
}


def _fmt_step(step: str, label: str, status: str = "info", detail: str = "") -> str:
    """Format 1 dòng log step. ``step`` không cần [..], tự thêm + pad.

    Vd: _fmt_step("2/6", "checkout", "ok", "cs=...") →
        "[2/6] checkout         ✓  cs=..."
    """
    icon = _STATUS_ICONS.get(status, " ")
    step_token = f"[{step}]".ljust(_STEP_WIDTH + 2)
    label_token = label.ljust(_LABEL_WIDTH)
    line = f"{step_token}{label_token}{icon}"
    if detail:
        line += f"  {detail}"
    return line


def _fmt_attempt(
    *, idx: int, total: int,
    http_status: Any, result: Any, proxy_mask: str,
    elapsed: float | None = None,
) -> str:
    """Format dòng 1 attempt approve/refresh.

    Vd: _fmt_attempt(idx=1, total=500, http_status=403, result="unknown", ...) →
        "      attempt 001/500  ✗  http=403  unknown     proxy=..."
    """
    icons = {
        "approved": "ok", "blocked": "blocked", "exception": "warn",
        None: "fail", "unknown": "fail",
    }
    status_key = str(result) if result is not None else None
    icon = _STATUS_ICONS.get(icons.get(status_key, "fail"), "✗")
    n = f"{idx:0>3}/{total}"
    h = f"http={'---' if http_status is None else http_status}"
    r = (str(result) if result is not None else "—").ljust(10)
    base = f"      attempt {n:<8}  {icon}  {h:<9}  {r}  proxy={proxy_mask}"
    if elapsed is not None:
        base += f"  ({elapsed:.1f}s)"
    return base


def _fmt_kv(*pairs: tuple[str, Any]) -> str:
    """Format key=value dạng inline gọn, bỏ qua None."""
    return "  ".join(f"{k}={v}" for k, v in pairs if v not in (None, ""))


def _short(value: str | None, head: int = 12) -> str:
    """Rút gọn chuỗi dài (vd cs_live_xxx…) cho dễ đọc."""
    if not value:
        return "-"
    return value if len(value) <= head else value[:head] + "…"


def _silent(_: str) -> None:
    """Nuốt log — dùng để tắt log nội bộ của pay_upi_http khi runner tự log."""


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
    resp = await sess.get(
        _STRIPE_ELEMENTS_URL, headers=headers, params=params, timeout=30, proxies=proxies,
    )
    if resp.status_code != 200:
        raise PayUpiError(f"elements/sessions HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    if not data.get("session_id"):
        raise PayUpiError(f"elements/sessions missing session_id: keys={list(data)[:20]}")
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

    _safe_log(_fmt_step("upi", "account", "info",
                        f"{masked_email}  proxy_pool={len(proxy_pool)}"))
    _safe_log(_fmt_step("upi", "config", "info", _fmt_kv(
        ("approve_retries", approve_retries),
        ("delay", f"{APPROVE_DELAY:g}s"),
        ("batch", APPROVE_PROXY_BATCH),
        ("be_consec", APPROVE_BACKEND_EXCEPTION_CONSECUTIVE),
        ("variants", ",".join(CONFIRM_VARIANTS)),
    )))

    # Lazy import → chỉ khi job thật sự chạy.
    from curl_cffi.requests import AsyncSession
    from .. import stripe_token as _st
    from ..pay_upi_http import _stripe_init
    from ..random_profile import random_india_profile
    from ..session_phase import SessionError, get_session_pure_request

    # ─────────────────────────────────────────────────────────────────
    # Step 1 — login với retry. SessionError dạng "WARNING_BANNER" /
    # "no accessToken" / "session-token cookie" / "callback URL" là transient
    # (Cloudflare/proxy/server flaky, callback set-cookie chậm) → retry tối đa
    # 3 lần (initial + 2 retry). Lỗi vĩnh viễn (wrong password, MFA fail,
    # no mail provider) raise ngay không retry — tránh login spam → lockout.
    # DIRECT: no proxy để giảm captcha trên ChatGPT.
    # ─────────────────────────────────────────────────────────────────
    LOGIN_MAX_ATTEMPTS = 3
    LOGIN_RETRY_DELAY = 3.0
    NON_RETRYABLE_PATTERNS = (
        "password verify failed",
        "mfa verify failed",
        "no mail_provider available",
        "no secret provided",
        "yêu cầu 2fa nhưng không có",
        "otp polling returned empty",
        "passwordless otp login but no mail_provider",
    )

    def _is_login_error_retryable(exc_msg: str) -> bool:
        lower = exc_msg.lower()
        return not any(pat in lower for pat in NON_RETRYABLE_PATTERNS)

    _safe_log(_fmt_step("1/6", "login", "start", "pure-HTTP request_phase"))
    session_data: dict[str, Any] | None = None
    last_login_error: str | None = None
    for login_attempt in range(1, LOGIN_MAX_ATTEMPTS + 1):
        try:
            session_data = await get_session_pure_request(
                email=email,
                password=password,
                secret=secret,
                proxy=None,
                log=_safe_log,
            )
            if login_attempt > 1:
                _safe_log(
                    f"[upi-qr] login OK ở attempt {login_attempt}/{LOGIN_MAX_ATTEMPTS}"
                )
            break
        except SessionError as exc:
            last_login_error = str(exc)
            retryable = _is_login_error_retryable(last_login_error)
            if not retryable:
                _safe_log(
                    f"[upi-qr] login fail (non-retryable): {last_login_error[:200]}"
                )
                break
            if login_attempt >= LOGIN_MAX_ATTEMPTS:
                _safe_log(
                    f"[upi-qr] login fail after {LOGIN_MAX_ATTEMPTS} attempts: "
                    f"{last_login_error[:200]}"
                )
                break
            _safe_log(
                f"[upi-qr] login transient error "
                f"(attempt {login_attempt}/{LOGIN_MAX_ATTEMPTS}): "
                f"{last_login_error[:140]} — retry sau {LOGIN_RETRY_DELAY:g}s..."
            )
            await asyncio.sleep(LOGIN_RETRY_DELAY)

    if session_data is None:
        _safe_log(_fmt_step("1/6", "login", "fail", last_login_error or "unknown"))
        return UpiQrResult(
            ok=False, email=masked_email,
            error=f"login fail: {last_login_error or 'unknown'}",
            elapsed_seconds=monotonic() - started,
        )

    access_token = session_data.get("accessToken")
    if not isinstance(access_token, str) or not access_token:
        _safe_log(_fmt_step("1/6", "login", "fail", "không có accessToken trong response"))
        return UpiQrResult(
            ok=False, email=masked_email,
            error="login OK nhưng không có accessToken",
            elapsed_seconds=monotonic() - started,
        )
    user_email = (session_data.get("user") or {}).get("email") or masked_email
    _safe_log(_fmt_step("1/6", "login", "ok", f"user={user_email}"))

    stripe_js_id = str(uuid.uuid4())
    confirm_attempts: list[dict[str, Any]] = []
    approve_attempts: list[dict[str, Any]] = []
    page_refresh_attempts: list[dict[str, Any]] = []
    backend_exception_count = 0
    consecutive_backend_exception = 0
    fatal_approve_error: str | None = None
    amount = 0
    return_url = ""
    session_id = ""
    qr_image_url: str | None = None
    upi_uri: str | None = None
    qr_expires_at: int | None = None

    async with AsyncSession(impersonate="chrome136") as sess:
        # Step 2 — checkout creation (DIRECT - chatgpt API).
        checkout = await _create_chatgpt_checkout(
            sess, access_token=access_token, log=_silent,
            proxies=_proxy_dict(first_proxy if PROXY_FROM_STEP <= 2 else None),
        )
        session_id = checkout["checkout_session_id"]
        return_url = _stripe_return_url(session_id)
        publishable_key = checkout["publishable_key"]
        _safe_log(_fmt_step("2/6", "checkout", "ok",
                            f"cs={_short(session_id, 14)}  ui={checkout.get('checkout_ui_mode') or '-'}"))

        # Step 3 — Stripe init.
        init_data = await _stripe_init(
            sess,
            session_id=session_id,
            publishable_key=publishable_key,
            stripe_js_id=stripe_js_id,
            log=_silent,
            proxies=_proxy_for_step(first_proxy, from_step=PROXY_FROM_STEP, step=3),
        )
        amount = _extract_amount(init_data)
        _safe_log(_fmt_step("3/6", "init", "ok",
                            f"amount={amount}  ppage={_short(init_data.get('id') or '', 12)}"))
        if PROMO and amount > 0:
            _safe_log(_fmt_step("upi", "no free offer", "fail",
                                f"amount={amount} (promo bật nhưng > 0)"))
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
            log=_silent,
            proxies=_proxy_for_step(first_proxy, from_step=PROXY_FROM_STEP, step=4),
        )
        _safe_log(_fmt_step("4/6", "elements", "ok",
                            f"session={_short(elements_data.get('session_id') or '', 14)}"))

        # Step 5a — extract Stripe token config (best-effort).
        token_config = None
        try:
            token_config = await _st.extract_config_live(
                sess, log=_silent, use_cache=True,
                fallback_dir=Path(__file__).resolve().parents[1]
                / "runtime" / "cache" / "stripe_bundles_default",
                proxies=None,
            )
            _safe_log(_fmt_step("5a", "token-config", "ok",
                                f"shift={token_config.shift}  rv={_short(token_config.rv, 8)}"))
        except _st.StripeTokenExtractError as exc:
            _safe_log(_fmt_step("5a", "token-config", "warn", f"extract fail: {str(exc)[:120]}"))

        # Step 5b — confirm variants.
        profile = random_india_profile()
        final_confirmed = False    # ít nhất 1 variant confirm OK
        final_approved = False     # approve trả result=approved trong loop
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
                log=_silent,
                token_config=token_config,
                proxies=_proxy_for_step(first_proxy, from_step=PROXY_FROM_STEP, step=5),
            )
            confirm_attempts.append(attempt)
            confirm_status = "ok" if attempt.get("ok") else "fail"
            confirm_detail = f"variant={variant}  http={attempt.get('http_status')}"
            err = attempt.get("error")
            if err and isinstance(err, dict):
                code = err.get("code") or err.get("type") or ""
                if code:
                    confirm_detail += f"  err={code}"
            _safe_log(_fmt_step("5b", "confirm", confirm_status, confirm_detail))
            if not attempt.get("ok"):
                continue
            final_confirmed = True

            # Confirm OK → refresh + approve loop.
            refresh_attempt = await _stripe_payment_page_refresh_retry(
                sess,
                session_id=session_id,
                publishable_key=publishable_key,
                stripe_js_id=stripe_js_id,
                elements_data=elements_data,
                log=_silent,
                proxy_pool=proxy_pool if PROXY_FROM_STEP <= 5 else [],
            )
            page_refresh_attempts.append(refresh_attempt)
            _safe_log(_fmt_step(
                "5c", "page-refresh",
                "ok" if refresh_attempt.get("ok") else "fail",
                f"http={refresh_attempt.get('http_status')}  proxy={refresh_attempt.get('proxy')}",
            ))

            _safe_log(_fmt_step("6/6", "approve loop", "start",
                                f"retries={approve_retries}  delay={APPROVE_DELAY:g}s  batch={APPROVE_PROXY_BATCH}"))

            approved = False
            approve_started = monotonic()
            # Virtual attempt counter dành riêng cho proxy selection (tách
            # khỏi approve_index). Mỗi attempt += 1; khi gặp backend_exception
            # ta advance qua hết phần còn lại của batch hiện tại để skip sang
            # proxy kế ngay — tránh "đốt" threshold cho 1 proxy đang bị
            # throttle. Indexing vẫn dùng `_proxy_url_for_retry` để giữ
            # contract: floor((va-1)/batch) % len(pool).
            proxy_virtual_attempt = 0
            _proxy_advance_enabled = (
                PROXY_FROM_STEP <= 6
                and APPROVE_PROXY_BATCH > 1
                and len(proxy_pool) > 1
            )
            for approve_index in range(1, approve_retries + 1):
                proxy_virtual_attempt += 1
                approve_proxy = _proxy_url_for_retry(
                    proxy_pool,
                    from_step=PROXY_FROM_STEP,
                    step=6,
                    attempt=proxy_virtual_attempt,
                    per_proxy_attempts=APPROVE_PROXY_BATCH,
                )
                try:
                    approve_attempt = await _chatgpt_approve_checkout(
                        sess,
                        access_token=access_token,
                        session_id=session_id,
                        log=_silent,
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
                _safe_log(_fmt_attempt(
                    idx=approve_index, total=approve_retries,
                    http_status=approve_attempt.get("http_status"),
                    result=approve_attempt.get("result") or approve_attempt.get("error_type"),
                    proxy_mask=_mask_proxy(approve_proxy),
                ))
                if approve_attempt.get("ok"):
                    approved = True
                    break
                if _is_backend_exception(approve_attempt):
                    backend_exception_count += 1
                    consecutive_backend_exception += 1
                    if (
                        APPROVE_BACKEND_EXCEPTION_CONSECUTIVE > 0
                        and consecutive_backend_exception
                        >= APPROVE_BACKEND_EXCEPTION_CONSECUTIVE
                    ):
                        fatal_approve_error = (
                            f"approve consecutive backend_exception threshold "
                            f"({consecutive_backend_exception}/"
                            f"{APPROVE_BACKEND_EXCEPTION_CONSECUTIVE}) "
                            f"total_exceptions={backend_exception_count}"
                        )
                        _safe_log(_fmt_step("6/6", "approve", "fail",
                                            f"consec be_excpt {consecutive_backend_exception}/{APPROVE_BACKEND_EXCEPTION_CONSECUTIVE}"))
                        break
                    # Advance proxy ngay khi gặp backend_exception: nếu vẫn
                    # còn trong batch hiện tại, jump tới đầu batch kế. Lần
                    # sau += 1 sẽ trỏ vào proxy mới. Giúp tránh đốt threshold
                    # cho 1 proxy đang bị Stripe throttle.
                    if _proxy_advance_enabled:
                        current_batch = (proxy_virtual_attempt - 1) // APPROVE_PROXY_BATCH
                        position_in_batch = proxy_virtual_attempt - current_batch * APPROVE_PROXY_BATCH
                        if position_in_batch < APPROVE_PROXY_BATCH:
                            proxy_virtual_attempt = (current_batch + 1) * APPROVE_PROXY_BATCH
                else:
                    # Reset chuỗi consecutive khi gặp result không-exception
                    # (server không stuck) — log gọn 1 dòng nếu reset thật sự xảy ra.
                    if consecutive_backend_exception > 0:
                        _safe_log(_fmt_step("6/6", "approve", "info",
                                            f"reset consec be_excpt ({consecutive_backend_exception} → 0)"))
                        consecutive_backend_exception = 0
                if approve_index < approve_retries:
                    await asyncio.sleep(APPROVE_DELAY)

            approve_elapsed = monotonic() - approve_started
            if approved:
                final_approved = True
                _safe_log(_fmt_step("6/6", "approve", "ok",
                                    f"approved at {approve_index}/{approve_retries}  ({approve_elapsed:.1f}s)"))
            elif not fatal_approve_error:
                _safe_log(_fmt_step("6/6", "approve", "fail",
                                    f"không approved sau {approve_retries} attempts ({approve_elapsed:.1f}s)"))

            if not fatal_approve_error and (approved or approve_attempts):
                refresh2 = await _stripe_payment_page_refresh_retry(
                    sess,
                    session_id=session_id,
                    publishable_key=publishable_key,
                    stripe_js_id=stripe_js_id,
                    elements_data=elements_data,
                    log=_silent,
                    proxy_pool=proxy_pool if PROXY_FROM_STEP <= 5 else [],
                )
                page_refresh_attempts.append(refresh2)
                _safe_log(_fmt_step(
                    "5c", "page-refresh", "ok" if refresh2.get("ok") else "fail",
                    f"http={refresh2.get('http_status')}  proxy={refresh2.get('proxy')}",
                ))
            break  # variant đầu tiên confirm OK → dừng vòng variants.

        # Aggregate matches từ mọi response (kể cả khi approve fail — QR có thể
        # đã có từ confirm response để user scan thủ công).
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
        qr_expires_at = _find_qr_expires_at(matches)

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

        # QR final log + final summary
        if qr_path:
            qr_detail = _fmt_kv(
                ("source", qr_source),
                ("expires_at", qr_expires_at),
            )
            _safe_log(_fmt_step("qr", "saved", "ok", qr_detail))
        else:
            _safe_log(_fmt_step("qr", "saved", "fail", qr_reason or "unknown"))

    elapsed = monotonic() - started

    # Determine success: cần CẢ approve approved + qr file rendered.
    # Ưu tiên error: fatal (consec be_excpt threshold) > confirm fail > approve fail > qr fail.
    if fatal_approve_error:
        error_msg = fatal_approve_error
    elif not final_confirmed:
        error_msg = "confirm thất bại với mọi variant"
    elif not final_approved:
        error_msg = (
            f"approve không thành công sau {len(approve_attempts)} attempts "
            f"(retries={approve_retries})"
        )
    elif not qr_path:
        error_msg = qr_reason or "no QR generated"
    else:
        error_msg = None

    ok = error_msg is None
    _safe_log(_fmt_step(
        "upi", "done", "ok" if ok else "fail",
        (f"qr={'yes' if qr_path else 'no'}  approved={'yes' if final_approved else 'no'}  "
         f"total={elapsed:.1f}s") + (f"  error={error_msg}" if error_msg else ""),
    ))

    return UpiQrResult(
        ok=ok,
        email=masked_email,
        amount=amount,
        return_url=return_url,
        checkout_session=str(session_id)[:18] + "..." if session_id else "",
        qr_path=qr_path,
        qr_source=qr_source,
        qr_source_url=qr_image_url,
        qr_reason=qr_reason,
        qr_expires_at=qr_expires_at,
        has_upi_uri=bool(upi_uri),
        has_qr_image_url=bool(qr_image_url),
        confirm_attempts=_summarize_confirm(confirm_attempts),
        approve_attempts=_summarize_approve(approve_attempts),
        page_refresh_attempts=_summarize_refresh(page_refresh_attempts),
        backend_exception_count=backend_exception_count,
        error=error_msg,
        elapsed_seconds=elapsed,
    )


__all__ = [
    "PROMO", "PROXY_FROM_STEP", "DO_CONFIRM", "DO_APPROVE",
    "APPROVE_DELAY", "APPROVE_PROXY_BATCH",
    "APPROVE_BACKEND_EXCEPTION_CONSECUTIVE", "CONFIRM_VARIANTS",
    "UpiQrResult", "UpiQrError", "run_upi_qr_probe",
]
