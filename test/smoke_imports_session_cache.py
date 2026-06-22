"""Smoke import: các module đã sửa import được + validator 3 key mới hoạt động.

Chạy: python3 test/smoke_imports_session_cache.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GSH_DB_PATH", str(ROOT / "runtime" / "data.db"))
os.environ.setdefault("GPT_SIGNUP_WEB_TOKEN", "smoke-token")

fails = 0


def _ck(cond, cid, desc, detail=""):
    global fails
    tag = "PASS" if cond else "FAIL"
    if not cond:
        fails += 1
    print(f"[{tag}] {cid} — {desc} :: {detail}", flush=True)


# Import modules đã đụng tới (bắt lỗi import-time: circular, NameError, ...).
try:
    import session_store  # noqa: F401
    import session_provider  # noqa: F401
    import session_phase  # noqa: F401
    _ck(True, "IMP-01", "session_store/provider/phase import OK")
except Exception as exc:
    _ck(False, "IMP-01", "import core", f"{type(exc).__name__}: {exc}")

try:
    from session_phase import NON_RETRYABLE_LOGIN_PATTERNS, is_fatal_login_error
    _ck(is_fatal_login_error("password verify failed: HTTP 400") is True
        and is_fatal_login_error("WARNING_BANNER transient") is False
        and len(NON_RETRYABLE_LOGIN_PATTERNS) >= 5,
        "IMP-02", "is_fatal_login_error gom DRY hoạt động")
except Exception as exc:
    _ck(False, "IMP-02", "is_fatal_login_error", f"{type(exc).__name__}: {exc}")

try:
    import web.manager  # noqa: F401
    import web.upi_runner  # noqa: F401
    _ck(True, "IMP-03", "web.manager + web.upi_runner import OK (alias slug, import patterns)")
except Exception as exc:
    _ck(False, "IMP-03", "import web.manager/upi_runner", f"{type(exc).__name__}: {exc}")

try:
    import web.server  # noqa: F401
    _ck(True, "IMP-04", "web.server import OK")
except Exception as exc:
    _ck(False, "IMP-04", "import web.server", f"{type(exc).__name__}: {exc}")

# Validator 3 key mới
try:
    from db.repositories import _validate_type_constraint, _EXACT_KEYS, RepositoryError
    for k in ("session.reuse_enabled", "session.revalidate_http", "session.cookie_max_age_hours"):
        assert k in _EXACT_KEYS, f"{k} thiếu trong _EXACT_KEYS"
    _validate_type_constraint("session.reuse_enabled", True)
    _validate_type_constraint("session.revalidate_http", False)
    _validate_type_constraint("session.cookie_max_age_hours", 24)
    bad = 0
    for k, v in [("session.reuse_enabled", "x"), ("session.cookie_max_age_hours", 0),
                 ("session.cookie_max_age_hours", "z")]:
        try:
            _validate_type_constraint(k, v)
        except RepositoryError:
            bad += 1
    _ck(bad == 3, "IMP-05", "validator session.* accept hợp lệ + reject sai", f"rejected={bad}/3")
except Exception as exc:
    _ck(False, "IMP-05", "validator session.*", f"{type(exc).__name__}: {exc}")

print(f"\n=== {'ALL PASS' if fails == 0 else str(fails) + ' FAIL'} ===", flush=True)
sys.exit(1 if fails else 0)
