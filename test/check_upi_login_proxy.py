"""Verify feature ``upi.login_proxy_url`` end-to-end (Step 1+2 override).

Test cases:
    [TC-01] Whitelist key ``upi.login_proxy_url`` trong ``_EXACT_KEYS``.
    [TC-02] ``_is_valid_proxy_url`` accept format hợp lệ + reject format sai.
    [TC-03] ``_validate_type_constraint`` accept string/null/empty + reject
            non-str / quá dài / format invalid.
    [TC-04] ``UpiJobManager`` có property + setter + apply_settings hydrate.
    [TC-05] ``run_upi_qr_probe`` accept kwarg ``login_proxy_url``.
    [TC-06] Pydantic ``SetUpiConfigRequest`` accept field ``login_proxy_url``.
    [TC-07] AST: server.py có write-through key ``"upi.login_proxy_url"``.
    [TC-08] AST: upi.js có DOM ref + load + save logic cho field mới.
    [TC-09] AST: index.html có `<input id="upi-login-proxy-url">`.
    [TC-10] AST: upi_runner.py vẫn giữ luồng cũ khi login_proxy_url=None
            (effective_login_proxy = None → Step 2 fallback proxy_from_step).

Run: python3 test/check_upi_login_proxy.py
"""
from __future__ import annotations

import ast
import sys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

PKG = "gpt_signup_hybrid_new"


def _ok(tc: str, msg: str = "") -> None:
    suffix = f" :: {msg}" if msg else ""
    print(f"[PASS] {tc}{suffix}", flush=True)


def _fail(tc: str, msg: str) -> None:
    print(f"[FAIL] {tc} :: {msg}", flush=True)
    raise AssertionError(f"{tc}: {msg}")


# ── TC-01 ─────────────────────────────────────────────────────────────
def tc01_whitelist() -> None:
    from gpt_signup_hybrid_new.db.repositories import _EXACT_KEYS
    assert "upi.login_proxy_url" in _EXACT_KEYS, \
        "upi.login_proxy_url chưa có trong _EXACT_KEYS"
    _ok("TC-01 whitelist key")


# ── TC-02 ─────────────────────────────────────────────────────────────
def tc02_proxy_url_validator() -> None:
    from gpt_signup_hybrid_new.db.repositories import _is_valid_proxy_url
    valid = [
        "http://1.2.3.4:8080",
        "http://user:pass@1.2.3.4:8080",
        "https://proxy.example.com:443",
        "socks5://user:p@ss@1.2.3.4:1080",
        "socks5h://1.2.3.4:1080",
        "http://user:pass@vn-proxy.example.com:12345",
    ]
    for u in valid:
        if not _is_valid_proxy_url(u):
            _fail("TC-02", f"valid URL bị reject: {u!r}")
    invalid = [
        "",
        "1.2.3.4:8080",          # thiếu scheme
        "ftp://1.2.3.4:21",       # scheme không support
        "http://1.2.3.4",         # thiếu port
        "http://:8080",           # thiếu host
        "not a url",
        "http://1.2.3.4:99999",   # port > 65535
    ]
    for u in invalid:
        if _is_valid_proxy_url(u):
            _fail("TC-02", f"invalid URL bị accept: {u!r}")
    _ok("TC-02 _is_valid_proxy_url accept/reject đúng")


# ── TC-03 ─────────────────────────────────────────────────────────────
def tc03_type_constraint() -> None:
    from gpt_signup_hybrid_new.db.repositories import (
        _validate_type_constraint,
        RepositoryError,
    )
    KEY = "upi.login_proxy_url"

    # Accept: None, empty string, whitespace, hợp lệ
    for v in (None, "", "   ", "http://1.2.3.4:8080",
              "http://user:pass@vn-proxy.example.com:12345"):
        try:
            _validate_type_constraint(KEY, v)
        except RepositoryError as exc:
            _fail("TC-03", f"valid value reject: {v!r} :: {exc}")

    # Reject: non-str
    for v in (123, True, [], {}, 3.14):
        try:
            _validate_type_constraint(KEY, v)
        except RepositoryError:
            continue
        _fail("TC-03", f"non-str {v!r} should reject")

    # Reject: quá dài
    long_url = "http://1.2.3.4:8080?" + "a" * 600
    try:
        _validate_type_constraint(KEY, long_url)
    except RepositoryError:
        pass
    else:
        _fail("TC-03", "URL > 500 ký tự nên reject")

    # Reject: format sai
    for v in ("ftp://1.2.3.4:21", "1.2.3.4:8080", "http://no-port", "garbage"):
        try:
            _validate_type_constraint(KEY, v)
        except RepositoryError:
            continue
        _fail("TC-03", f"format sai {v!r} nên reject")
    _ok("TC-03 type constraint accept/reject đúng")


# ── TC-04 ─────────────────────────────────────────────────────────────
def tc04_manager_hydration() -> None:
    import asyncio
    from gpt_signup_hybrid_new.web.manager import UpiJobManager

    async def _run():
        mgr = UpiJobManager(max_concurrent=1)
        # Default
        assert mgr.login_proxy_url == "", f"default phải empty, got {mgr.login_proxy_url!r}"

        # Setter accept hợp lệ
        mgr.set_login_proxy_url("http://1.2.3.4:8080")
        assert mgr.login_proxy_url == "http://1.2.3.4:8080"

        # Setter accept None + empty + whitespace → clear
        mgr.set_login_proxy_url(None)
        assert mgr.login_proxy_url == ""
        mgr.set_login_proxy_url("http://1.2.3.4:8080")
        mgr.set_login_proxy_url("")
        assert mgr.login_proxy_url == ""
        mgr.set_login_proxy_url("http://1.2.3.4:8080")
        mgr.set_login_proxy_url("   ")
        assert mgr.login_proxy_url == ""

        # Setter reject format sai
        try:
            mgr.set_login_proxy_url("not a url")
        except ValueError:
            pass
        else:
            _fail("TC-04", "format sai phải raise ValueError")

        try:
            mgr.set_login_proxy_url(123)  # type: ignore[arg-type]
        except ValueError:
            pass
        else:
            _fail("TC-04", "non-str phải raise ValueError")

        # apply_settings hydrate
        mgr.apply_settings({"upi.login_proxy_url": "http://5.6.7.8:9999"})
        assert mgr.login_proxy_url == "http://5.6.7.8:9999"

        # apply_settings với value invalid → giữ default (log warning, không crash)
        mgr.set_login_proxy_url("")  # reset
        mgr.apply_settings({"upi.login_proxy_url": "garbage"})
        assert mgr.login_proxy_url == "", "invalid hydrate value nên bị bỏ qua"

        mgr.shutdown()

    asyncio.run(_run())
    _ok("TC-04 UpiJobManager.login_proxy_url property/setter/hydrate")


# ── TC-05 ─────────────────────────────────────────────────────────────
def tc05_runner_signature() -> None:
    import inspect
    from gpt_signup_hybrid_new.web.upi_runner import run_upi_qr_probe
    sig = inspect.signature(run_upi_qr_probe)
    assert "login_proxy_url" in sig.parameters, \
        "run_upi_qr_probe thiếu kwarg login_proxy_url"
    param = sig.parameters["login_proxy_url"]
    assert param.default is None, \
        f"login_proxy_url default phải = None, got {param.default!r}"
    _ok("TC-05 run_upi_qr_probe signature accept login_proxy_url=None")


# ── TC-06 ─────────────────────────────────────────────────────────────
def tc06_pydantic_field() -> None:
    from gpt_signup_hybrid_new.web.server import SetUpiConfigRequest

    # Accept None (field optional)
    obj = SetUpiConfigRequest()
    assert obj.login_proxy_url is None

    # Accept hợp lệ (format validate ở backend setter, không ở Pydantic)
    obj = SetUpiConfigRequest(login_proxy_url="http://1.2.3.4:8080")
    assert obj.login_proxy_url == "http://1.2.3.4:8080"

    # Accept empty string (clear flow)
    obj = SetUpiConfigRequest(login_proxy_url="")
    assert obj.login_proxy_url == ""

    # Reject quá dài (max_length=500)
    try:
        SetUpiConfigRequest(login_proxy_url="x" * 600)
    except Exception:  # pydantic ValidationError
        pass
    else:
        _fail("TC-06", "max_length=500 phải reject string 600 ký tự")
    _ok("TC-06 Pydantic SetUpiConfigRequest accept login_proxy_url")


# ── TC-07 ─────────────────────────────────────────────────────────────
def tc07_server_writethrough() -> None:
    src = (ROOT / "web" / "server.py").read_text(encoding="utf-8")
    assert '"upi.login_proxy_url"' in src, (
        "endpoint /api/upi/config thiếu write-through key 'upi.login_proxy_url'"
    )
    # set_login_proxy_url phải được gọi
    assert "set_login_proxy_url" in src, (
        "endpoint POST /api/upi/config chưa gọi um.set_login_proxy_url"
    )
    _ok("TC-07 server.py write-through + setter wiring")


# ── TC-08 ─────────────────────────────────────────────────────────────
def tc08_js_wiring() -> None:
    js = (ROOT / "web" / "static" / "upi.js").read_text(encoding="utf-8")
    # DOM ref
    assert "loginProxyUrl: $('upi-login-proxy-url')" in js, "thiếu DOM ref"
    # Save handler
    assert "login_proxy_url:" in js, "thiếu key login_proxy_url trong save body"
    # Load init
    assert "cfg.login_proxy_url" in js, "thiếu load cfg.login_proxy_url"
    _ok("TC-08 upi.js wiring DOM/save/load")


# ── TC-09 ─────────────────────────────────────────────────────────────
def tc09_html_input() -> None:
    html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="upi-login-proxy-url"' in html, "thiếu <input id=upi-login-proxy-url>"
    _ok("TC-09 index.html input login proxy URL")


# ── TC-10 ─────────────────────────────────────────────────────────────
def tc10_runner_fallback_logic() -> None:
    """AST verify upi_runner giữ luồng cũ khi login_proxy_url=None."""
    src = (ROOT / "web" / "upi_runner.py").read_text(encoding="utf-8")

    # Có biến effective_login_proxy
    assert "effective_login_proxy" in src, \
        "thiếu biến effective_login_proxy"

    # Step 2 phải dùng effective_login_proxy với fallback first_proxy
    # khi proxy_from_step <= 2 (luồng cũ).
    pattern = re.compile(
        r"effective_login_proxy\s+is\s+not\s+None.*?"
        r"first_proxy\s+if\s+proxy_from_step\s*<=\s*2",
        re.DOTALL,
    )
    assert pattern.search(src), \
        "Step 2 không có branch fallback first_proxy theo proxy_from_step"

    # Có constant LOGIN_PROXY_RETRY_QUOTA
    assert "LOGIN_PROXY_RETRY_QUOTA" in src, \
        "thiếu LOGIN_PROXY_RETRY_QUOTA constant cho retry/fallback"

    # Hardcoded `login_proxy = None` cũ phải bị xóa
    # (regex match standalone `login_proxy` để không nhầm với `effective_login_proxy`).
    if re.search(r"(^|[^_])login_proxy\s*=\s*None\b", src):
        _fail("TC-10", "hardcoded `login_proxy = None` cũ chưa được thay")

    _ok("TC-10 upi_runner.py giữ luồng cũ + fallback DIRECT khi dead")


# ── Main ──────────────────────────────────────────────────────────────
def main() -> int:
    cases = [
        ("TC-01", tc01_whitelist),
        ("TC-02", tc02_proxy_url_validator),
        ("TC-03", tc03_type_constraint),
        ("TC-04", tc04_manager_hydration),
        ("TC-05", tc05_runner_signature),
        ("TC-06", tc06_pydantic_field),
        ("TC-07", tc07_server_writethrough),
        ("TC-08", tc08_js_wiring),
        ("TC-09", tc09_html_input),
        ("TC-10", tc10_runner_fallback_logic),
    ]
    failures = []
    for i, (name, fn) in enumerate(cases, start=1):
        print(f"\n[{i}/{len(cases)}] {name} — {fn.__doc__ or fn.__name__}", flush=True)
        try:
            fn()
        except AssertionError as exc:
            failures.append((name, str(exc)))
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {name} :: unexpected {type(exc).__name__}: {exc}", flush=True)
            failures.append((name, f"unexpected: {exc}"))

    print("\n========== SUMMARY ==========", flush=True)
    if failures:
        print(f"[FAIL] {len(failures)}/{len(cases)} failed", flush=True)
        for n, m in failures:
            print(f"  - {n}: {m}", flush=True)
        return 1
    print(f"[PASS] all {len(cases)} cases OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
