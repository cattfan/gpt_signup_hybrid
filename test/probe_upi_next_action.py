#!/usr/bin/env python3
"""Probe CONFIRM custom-mode để dump next_action — xác nhận có
`hosted_instructions_url` (payments.stripe.com/upi/instructions/...) + QR.

Tái dùng internal của web.upi_runner (DRY, không viết lại confirm). Reuse
access_token + proxy từ token export mới nhất (skip login).

Flow:
    checkout(custom) → stripe_init → elements/sessions → token_config
    → confirm(variant) tới khi có next_action → dump.

Chạy:
    .venv/bin/python3 test/probe_upi_next_action.py
    (TOKEN_FILE=<name.json> để chỉ định account; mặc định lấy file mới nhất)
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


def _load_latest_token() -> dict | None:
    override = os.environ.get("TOKEN_FILE", "").strip()
    from session_store import resolve_instance_id
    tok_dir = ROOT / "runtime" / "session_cache" / resolve_instance_id()
    if override:
        p = Path(override)
        if not p.is_absolute():
            p = tok_dir / override
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    if not tok_dir.exists():
        return None
    files = sorted(tok_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    return json.loads(files[0].read_text(encoding="utf-8")) if files else None


def _redact(obj, _d=0):
    SENS = ("secret", "token", "key", "cookie", "authorization", "password")
    if isinstance(obj, dict):
        return {k: ("[redacted]" if any(s in str(k).lower() for s in SENS) else _redact(v, _d + 1))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(x, _d + 1) for x in obj[:30]]
    if isinstance(obj, str) and len(obj) > 400:
        return obj[:300] + f"...(+{len(obj) - 300})"
    return obj


def _dump(label, data):
    print(f"\n----- {label} -----", flush=True)
    print(json.dumps(_redact(data), indent=2, ensure_ascii=False)[:8000], flush=True)


async def main_async() -> int:
    tok = _load_latest_token()
    if not tok or not tok.get("access_token"):
        _log("setup", "không có token export → cần ACCOUNT login (probe này chỉ reuse token).")
        return 2
    access_token = tok["access_token"]
    email = tok.get("email") or "token@reuse"
    raw_proxy = tok.get("proxy")
    proxy_url = raw_proxy if (raw_proxy and raw_proxy.startswith("http")) else None
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    _log("setup", f"email={email[:3]}***  proxy={'yes' if proxy_url else 'DIRECT'}  exported={tok.get('exported_at')}")

    from curl_cffi.requests import AsyncSession
    from user_agent_profile import CURL_IMPERSONATE_PRIMARY as _IMP
    from random_profile import random_india_profile
    import stripe_token as _st
    from pay_upi_http import _stripe_init
    from web.upi_runner import (
        _create_chatgpt_checkout, _stripe_elements_session,
        _stripe_confirm_upi_qr, _extract_amount, CONFIRM_VARIANTS,
    )

    def _silent(_):
        return None

    stripe_js_id = str(uuid.uuid4())
    profile = random_india_profile()

    async with AsyncSession(impersonate=_IMP, proxies=proxies) as sess:
        _log("2/6", "checkout (custom)...")
        checkout = await _create_chatgpt_checkout(
            sess, access_token=access_token, log=_silent, proxies=proxies,
        )
        cs = checkout["checkout_session_id"]
        pk = checkout["publishable_key"]
        _log("2/6", f"cs={cs[:20]}...  ui={checkout.get('checkout_ui_mode')}")

        _log("3/6", "stripe_init...")
        init_data = await _stripe_init(
            sess, session_id=cs, publishable_key=pk, stripe_js_id=stripe_js_id,
            log=_silent, proxies=proxies,
        )
        amount = _extract_amount(init_data)
        _log("3/6", f"amount={amount}  ppage={init_data.get('id')}")
        if amount != 0:
            _log("3/6", f"⚠ amount={amount} != 0 (promo không áp?) — vẫn thử confirm.")

        _log("4/6", "elements/sessions...")
        elements_data = await _stripe_elements_session(
            sess, session_id=cs, publishable_key=pk, stripe_js_id=stripe_js_id,
            amount=amount, log=_silent, proxies=proxies,
        )
        _log("4/6", f"elements session={elements_data.get('session_id')}")

        _log("5a", "token_config extract...")
        token_config = None
        try:
            token_config = await _st.extract_config_live(
                sess, log=_silent, use_cache=True,
                fallback_dir=ROOT / "runtime" / "cache" / "stripe_bundles_default",
                proxies=None,
            )
            _log("5a", f"shift={token_config.shift}")
        except Exception as exc:  # noqa: BLE001
            _log("5a", f"token_config fail (vẫn thử confirm): {type(exc).__name__}: {exc}")

        _log("5b", f"confirm variants {CONFIRM_VARIANTS}...")
        from web.upi_runner import _find_matches
        na = None
        used_variant = None
        last_ok_data = None
        for variant in CONFIRM_VARIANTS:
            attempt = await _stripe_confirm_upi_qr(
                sess, session_id=cs, publishable_key=pk, stripe_js_id=stripe_js_id,
                init_data=init_data, elements_data=elements_data, profile=profile,
                email=email, amount=amount, variant=variant, log=_silent,
                token_config=token_config, proxies=proxies,
            )
            ok = attempt.get("ok")
            data = attempt.get("data") or {}
            if ok and isinstance(data, dict):
                last_ok_data = data
            na_candidate = None
            if isinstance(data, dict):
                na_candidate = (
                    data.get("next_action")
                    or (data.get("payment_intent") or {}).get("next_action")
                )
            _log("5b", f"variant={variant}  http={attempt.get('http_status')}  ok={ok}  "
                       f"next_action={'YES' if na_candidate else 'no'}  err={attempt.get('error')}")
            if na_candidate:
                na = na_candidate
                used_variant = variant
                break

        if not na:
            _log("5b", "next_action không ở path quen → dump full confirm data + matchers.")
            if last_ok_data is not None:
                _log("5b", f"confirm data top-level keys = {list(last_ok_data)}")
                _dump("confirm data (ok variant, full)", last_ok_data)
                matches = _find_matches(last_ok_data, source="confirm")
                _log("matches", f"tổng {len(matches)} match (qr/upi/instructions/...)")
                for m in matches:
                    print(f"   path={m.get('path')}  kind={m.get('kind')}  value={str(m.get('value'))[:200]}", flush=True)
            # cũng refresh page xem QR có pop ở page refresh không
            from web.upi_runner import _stripe_payment_page_refresh
            ref = await _stripe_payment_page_refresh(
                sess, session_id=cs, publishable_key=pk, stripe_js_id=stripe_js_id,
                elements_data=elements_data, log=_silent, proxies=proxies,
            )
            rdata = ref.get("data") or {}
            rmatches = _find_matches(rdata, source="refresh") if isinstance(rdata, dict) else []
            _log("refresh", f"http={ref.get('http_status')}  matches={len(rmatches)}")
            for m in rmatches:
                print(f"   path={m.get('path')}  value={str(m.get('value'))[:200]}", flush=True)
            return 1

        _dump(f">>> next_action (variant={used_variant})", na)

        # Trích các field quan trọng.
        upi_block = na.get("upi_handle_redirect_or_display_qr_code") or {}
        hosted_url = upi_block.get("hosted_instructions_url")
        qr = upi_block.get("qr_code") or {}
        _log("result", f"hosted_instructions_url = {hosted_url}")
        _log("result", f"qr_code.image_url_png   = {qr.get('image_url_png')}")
        _log("result", f"qr_code.image_url_svg   = {qr.get('image_url_svg')}")
        _log("result", f"qr_code.expires_at      = {qr.get('expires_at')}")
        if hosted_url and "payments.stripe.com/upi/instructions/" in str(hosted_url):
            _log("result", "✓ ĐÚNG dạng link user yêu cầu (payments.stripe.com/upi/instructions/...)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))
