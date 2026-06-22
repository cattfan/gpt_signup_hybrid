"""Check seed_from_session_results: lọc cookie, dedupe, idempotent, no-clobber.

Chạy: python3 test/check_session_seed.py
Dùng FakeRepo trả rows giống session_results (cookies/two_factor là JSON string).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GSH_DB_PATH", str(ROOT / "runtime" / "data.db"))

from session_store import SessionStore  # noqa: E402
from session_provider import seed_from_session_results  # noqa: E402

_FAILS = 0


def _check(cond: bool, cid: str, desc: str, detail: str = "") -> None:
    global _FAILS
    tag = "PASS" if cond else "FAIL"
    if not cond:
        _FAILS += 1
    print(f"[{tag}] {cid} — {desc} :: {detail}", flush=True)


_SESS_COOKIES = json.dumps([
    {"name": "__Secure-next-auth.session-token", "value": "tok", "domain": "chatgpt.com"},
])


class FakeRepo:
    """Mimic SessionResultRepository.list_all() — rows raw (JSON string cols),
    ORDER BY created_at DESC."""
    def __init__(self, rows): self._rows = rows
    def list_all(self): return list(self._rows)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="seed_") as d:
        store = SessionStore(instance_id="seed", runtime_dir=Path(d))
        rows = [
            # mới nhất của good@x.com — có cookie session-token → seed
            {"email": "good@x.com", "cookies": _SESS_COOKIES, "access_token": "A1",
             "session_token": "S1", "two_factor": None, "created_at": "2026-06-21 22:00:00"},
            # row cũ hơn cùng email → dedupe, bỏ
            {"email": "good@x.com", "cookies": _SESS_COOKIES, "access_token": "A0",
             "session_token": "S0", "two_factor": None, "created_at": "2026-06-20 10:00:00"},
            # cookie rỗng [] → bỏ
            {"email": "empty@x.com", "cookies": "[]", "access_token": "B",
             "session_token": None, "two_factor": None, "created_at": "2026-06-21 21:00:00"},
            # cookie null → bỏ
            {"email": "null@x.com", "cookies": None, "access_token": "C",
             "session_token": None, "two_factor": None, "created_at": "2026-06-21 20:00:00"},
        ]
        repo = FakeRepo(rows)

        # TC-01: seed đúng 1 (chỉ good@x.com)
        n = seed_from_session_results(store, repo, log=lambda *_: None)
        _check(n == 1, "TC-01", "seed đúng 1 record (lọc cookie rỗng)", f"n={n}")

        rec = store.load("good@x.com")
        _check(rec is not None and rec["access_token"] == "A1" and rec["last_validated_at"] is None,
               "TC-02", "record mới nhất, last_validated_at=None", f"at={rec and rec['access_token']}")
        _check(store.load("empty@x.com") is None and store.load("null@x.com") is None,
               "TC-03", "cookie rỗng/null không seed")

        # TC-04: idempotent — chạy lại không nhân đôi, không đổi
        n2 = seed_from_session_results(store, repo, log=lambda *_: None)
        _check(n2 == 0, "TC-04", "chạy lại seed 0 (idempotent)", f"n2={n2}")

        # TC-05: no-clobber — record runtime mới hơn không bị seed đè
        store.save("good@x.com", {"cookies": json.loads(_SESS_COOKIES), "access_token": "RUNTIME_FRESH"})
        n3 = seed_from_session_results(store, repo, log=lambda *_: None)
        _check(n3 == 0 and store.load("good@x.com")["access_token"] == "RUNTIME_FRESH",
               "TC-05", "không clobber record runtime", f"n3={n3}")

    print(f"\n=== {'ALL PASS' if _FAILS == 0 else str(_FAILS) + ' FAIL'} ===", flush=True)
    sys.exit(1 if _FAILS else 0)


if __name__ == "__main__":
    main()
