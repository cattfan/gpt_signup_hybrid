"""Smoke: seed từ runtime/data.db thật → đếm record seed được.

Đọc-only trên session_results (list_all), ghi cache vào thư mục TẠM (không đụng
runtime thật). Xác nhận số account có cookie tái dùng được khớp kỳ vọng (~809).

Chạy: python3 test/smoke_session_seed_realdb.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB = ROOT / "runtime" / "data.db"
os.environ["GSH_DB_PATH"] = str(DB)

from db import get_engine, get_repos  # noqa: E402
from session_store import SessionStore  # noqa: E402
from session_provider import seed_from_session_results  # noqa: E402


def main() -> None:
    if not DB.exists():
        print(f"[SKIP] {DB} không tồn tại", flush=True)
        return
    engine = get_engine(str(DB))
    _, _, session_repo = get_repos(engine)
    total = len(session_repo.list_all())
    print(f"[info] session_results rows = {total}", flush=True)

    with tempfile.TemporaryDirectory(prefix="seed_real_") as d:
        store = SessionStore(instance_id="smoke", runtime_dir=Path(d))
        n = seed_from_session_results(store, session_repo, log=lambda m: print(f"  {m}", flush=True))
        # đếm file thực tế tạo ra
        files = list((Path(d) / "session_cache" / "smoke").glob("*.json"))
        ok = n == len(files) and n > 0
        tag = "PASS" if ok else "FAIL"
        print(f"[{tag}] seed {n} record (file thực={len(files)}) từ {total} rows", flush=True)
        # idempotent trên DB thật
        n2 = seed_from_session_results(store, session_repo, log=lambda *_: None)
        tag2 = "PASS" if n2 == 0 else "FAIL"
        print(f"[{tag2}] re-seed idempotent (n2={n2})", flush=True)
        sys.exit(0 if (ok and n2 == 0) else 1)


if __name__ == "__main__":
    main()
