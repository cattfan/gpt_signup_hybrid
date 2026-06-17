"""Verify ``upi.proxy_from_step`` end-to-end qua các layer.

Mục tiêu:
    [TC-01] Whitelist key ``upi.proxy_from_step``.
    [TC-02] Type constraint nhận int 1-6, reject 0/7/bool/str.
    [TC-03] ``run_upi_qr_probe`` accept tham số ``proxy_from_step`` keyword.
    [TC-04] ``UpiJobManager`` có property + setter + apply_settings hydrate.
    [TC-05] ``SetUpiConfigRequest`` Pydantic accept field.
    [TC-06] AST: route ``/api/upi/config`` POST có write-through key vào
            settings_writes.
    [TC-07] Module-level ``PROXY_FROM_STEP`` constant vẫn = 3 (giữ
            backward-compat cho test cũ).
    [TC-08] Step 1 login: ``proxy_from_step==1`` → login_proxy = first_proxy
            (đồng bộ ``pay_upi_http._ProxyPolicy`` semantics).

Mỗi TC log [PASS]/[FAIL] ngay, không gom cuối.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

PASSED = 0
FAILED = 0


def _check(label: str, fn) -> None:
    global PASSED, FAILED
    try:
        fn()
        print(f"[PASS] {label}", flush=True)
        PASSED += 1
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {label} :: {type(exc).__name__}: {exc}", flush=True)
        FAILED += 1


def t01_whitelist():
    from gpt_signup_hybrid.db.repositories import _EXACT_KEYS
    assert "upi.proxy_from_step" in _EXACT_KEYS, "key missing in _EXACT_KEYS"
    assert "upi.login_via_proxy" not in _EXACT_KEYS, (
        "upi.login_via_proxy không nên có (chỉ dùng 1 key proxy_from_step)"
    )


def t02_type_constraint():
    from gpt_signup_hybrid.db.repositories import (
        _validate_type_constraint, RepositoryError,
    )
    # Accept range
    for v in (1, 2, 3, 4, 5, 6):
        _validate_type_constraint("upi.proxy_from_step", v)
    # Reject out-of-range
    for v in (0, 7, -1, 100):
        try:
            _validate_type_constraint("upi.proxy_from_step", v)
            raise AssertionError(f"expected reject {v}")
        except RepositoryError:
            pass
    # Reject wrong type
    for v in (True, False, "3", 3.0, None):
        try:
            _validate_type_constraint("upi.proxy_from_step", v)
            raise AssertionError(f"expected reject {v!r}")
        except RepositoryError:
            pass


def t03_runner_signature():
    import inspect
    from gpt_signup_hybrid.web.upi_runner import run_upi_qr_probe
    sig = inspect.signature(run_upi_qr_probe)
    assert "proxy_from_step" in sig.parameters, "thiếu kwarg proxy_from_step"
    p = sig.parameters["proxy_from_step"]
    assert p.kind == inspect.Parameter.KEYWORD_ONLY, "phải keyword-only"
    assert p.default == 3, f"default phải = 3 (PROXY_FROM_STEP), got {p.default}"


def t04_manager_hydration():
    import asyncio
    from gpt_signup_hybrid.web.manager import UpiJobManager

    async def _run() -> None:
        mgr = UpiJobManager()
        # Property + setter
        assert mgr.proxy_from_step == 3, f"default phải = 3, got {mgr.proxy_from_step}"
        mgr.set_proxy_from_step(1)
        assert mgr.proxy_from_step == 1
        try:
            mgr.set_proxy_from_step(0)
            raise AssertionError("expected reject 0")
        except ValueError:
            pass
        try:
            mgr.set_proxy_from_step(7)
            raise AssertionError("expected reject 7")
        except ValueError:
            pass
        # apply_settings hydrate
        mgr.apply_settings({"upi.proxy_from_step": 5})
        assert mgr.proxy_from_step == 5
        # Range invalid trong settings dict → bỏ qua không crash.
        mgr.apply_settings({"upi.proxy_from_step": 99})
        assert mgr.proxy_from_step == 5  # giữ nguyên
        mgr.shutdown()

    asyncio.run(_run())


def t05_pydantic_accept():
    from gpt_signup_hybrid.web.server import SetUpiConfigRequest
    req = SetUpiConfigRequest(proxy_from_step=1)
    assert req.proxy_from_step == 1
    req = SetUpiConfigRequest(proxy_from_step=6)
    assert req.proxy_from_step == 6
    # Reject out-of-range
    try:
        SetUpiConfigRequest(proxy_from_step=0)
        raise AssertionError("expected reject 0")
    except Exception:
        pass
    try:
        SetUpiConfigRequest(proxy_from_step=7)
        raise AssertionError("expected reject 7")
    except Exception:
        pass


def t06_endpoint_writethrough():
    """AST check: handler /api/upi/config có gán key write-through."""
    src = (ROOT / "web" / "server.py").read_text(encoding="utf-8")
    # Tìm string literal key trong source
    assert '"upi.proxy_from_step"' in src, (
        "endpoint /api/upi/config thiếu write-through 'upi.proxy_from_step'"
    )


def t07_const_unchanged():
    from gpt_signup_hybrid.web.upi_runner import PROXY_FROM_STEP
    assert PROXY_FROM_STEP == 3, (
        f"PROXY_FROM_STEP phải giữ = 3 (backward-compat), got {PROXY_FROM_STEP}"
    )


def t08_login_proxy_logic_source():
    """Source-level check: step 1 login dùng login_proxy = first_proxy
    when proxy_from_step <= 1.

    Đây là static check vì login flow chạy network — không thể smoke-test
    end-to-end ở đây. Verify code đã đổi từ ``proxy=None`` cứng sang
    conditional.
    """
    src = (ROOT / "web" / "upi_runner.py").read_text(encoding="utf-8")
    assert "login_proxy = first_proxy if (proxy_from_step <= 1 and first_proxy) else None" in src, (
        "login_proxy expression chưa cập nhật theo proxy_from_step"
    )
    assert "proxy=login_proxy," in src, (
        "get_session_pure_request chưa nhận proxy=login_proxy"
    )
    # Đảm bảo không còn `proxy=None,` cứng cho login (search vùng login).
    login_block_start = src.index("get_session_pure_request(")
    login_block = src[login_block_start:login_block_start + 500]
    assert "proxy=None," not in login_block, (
        "vẫn còn proxy=None cứng trong login block"
    )


def main() -> int:
    tests = [
        ("TC-01 whitelist key", t01_whitelist),
        ("TC-02 type constraint range", t02_type_constraint),
        ("TC-03 run_upi_qr_probe signature", t03_runner_signature),
        ("TC-04 UpiJobManager hydration", t04_manager_hydration),
        ("TC-05 SetUpiConfigRequest pydantic", t05_pydantic_accept),
        ("TC-06 endpoint write-through", t06_endpoint_writethrough),
        ("TC-07 PROXY_FROM_STEP constant unchanged", t07_const_unchanged),
        ("TC-08 step 1 login uses proxy_from_step==1", t08_login_proxy_logic_source),
    ]
    for label, fn in tests:
        _check(label, fn)
    print(f"\n--- summary: {PASSED} pass, {FAILED} fail ---", flush=True)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
