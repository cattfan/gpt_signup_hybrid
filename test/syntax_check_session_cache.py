"""AST parse các file đụng tới trong feature session-cookie-cache."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = [
    "session_store.py",
    "session_provider.py",
    "session_phase.py",
    "db/repositories.py",
    "web/manager.py",
    "web/upi_runner.py",
]

fails = 0
for rel in FILES:
    p = ROOT / rel
    try:
        ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        print(f"[PASS] {rel}", flush=True)
    except SyntaxError as e:
        fails += 1
        print(f"[FAIL] {rel} :: {e}", flush=True)

print(f"\n=== {'ALL PASS' if fails == 0 else str(fails) + ' FAIL'} ===", flush=True)
sys.exit(1 if fails else 0)
