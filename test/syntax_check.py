"""Parse AST + compile các file Python đã sửa để bắt lỗi cú pháp.

Chạy: .venv/bin/python test/syntax_check.py
In tiến trình realtime theo từng file.
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TARGETS = [
    "request_phase.py",
    "web/manager.py",
    "db/repositories.py",
]


def main() -> int:
    failed = 0
    for i, rel in enumerate(TARGETS, 1):
        path = ROOT / rel
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src, filename=str(path))
            compile(tree, filename=str(path), mode="exec")
            print(f"[PASS] [{i}/{len(TARGETS)}] {rel} :: AST + compile OK", flush=True)
        except SyntaxError as exc:
            failed += 1
            print(f"[FAIL] [{i}/{len(TARGETS)}] {rel} :: {exc}", flush=True)
    print(f"--- {len(TARGETS) - failed}/{len(TARGETS)} PASS ---", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
