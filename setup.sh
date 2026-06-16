#!/usr/bin/env bash
# gpt_signup_hybrid — 1 lệnh setup + start web UI.
#
# Coi thư mục này là project root: tất cả file (.venv, runtime, .env)
# đều nằm trong gpt_signup_hybrid/, không leak ra parent.
#
# Pinned stack (xem requirements.txt):
#   - Python 3.13 (Camoufox 0.4.11 + Firefox 135 chưa hỗ trợ Python 3.14)
#   - playwright==1.49.1 (Firefox 132 driver — match Camoufox FF135)
#   - camoufox==0.4.11 (binary FF 135.0.1-beta.24)
#
# Usage:
#   cd gpt_signup_hybrid
#   bash setup.sh
set -euo pipefail

# ROOT_DIR = chính thư mục chứa setup.sh.
# Folder này có thể tên bất kỳ (gpt_signup_hybrid, gpt_signup_hybrid_clean, foo, ...)
# nên KHÔNG add parent vào sys.path — sẽ tạo symlink trong venv shim dir
# với đúng tên `gpt_signup_hybrid` để Python tìm được package.
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

PKG_NAME="gpt_signup_hybrid"

# Python 3.13 bắt buộc — fail-fast nếu không có.
PY_BIN="${PYTHON:-}"
if [ -z "${PY_BIN}" ]; then
  if command -v python3.13 >/dev/null 2>&1; then
    PY_BIN="$(command -v python3.13)"
  else
    echo "ERROR: cần Python 3.13 (Camoufox 0.4.11 chưa hỗ trợ 3.14)." >&2
    echo "  Cài qua Homebrew:  brew install python@3.13" >&2
    echo "  Hoặc set PYTHON=/path/to/python3.13 rồi chạy lại." >&2
    exit 1
  fi
fi
PY_VERSION="$("$PY_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [ "$PY_VERSION" != "3.13" ]; then
  echo "ERROR: $PY_BIN báo Python $PY_VERSION, cần 3.13." >&2
  exit 1
fi

REQ_FILE="$ROOT_DIR/requirements.txt"
if [ ! -f "$REQ_FILE" ]; then
  echo "ERROR: $REQ_FILE không tồn tại." >&2
  exit 1
fi

echo "═══════════════════════════════════════════════════════════"
echo "  gpt_signup_hybrid — auto setup + start"
echo "  python: $PY_BIN ($PY_VERSION)"
echo "  root:   $ROOT_DIR"
echo "═══════════════════════════════════════════════════════════"

# 1. Python venv (trong chính package)
if [ ! -d ".venv" ]; then
  echo "[1/6] Creating .venv (python $PY_VERSION)..."
  "$PY_BIN" -m venv .venv
else
  EXISTING="$(.venv/bin/python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "?")"
  if [ "$EXISTING" != "3.13" ]; then
    echo "[1/6] .venv đang dùng Python $EXISTING — recreate cho 3.13..."
    rm -rf .venv
    "$PY_BIN" -m venv .venv
  else
    echo "[1/6] .venv exists (python 3.13) ✓"
  fi
fi

# 2. Install pinned deps (dùng python -m pip để tránh pip symlink trỏ sai venv)
echo "[2/6] Installing dependencies (pinned)..."
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r "$REQ_FILE"

# 3. Tạo shim dir + symlink + .pth để Python import được package
#    bất kể folder gốc tên gì (gpt_signup_hybrid_clean, foo, …).
SITE_PKG="$(.venv/bin/python -c 'import site; print(site.getsitepackages()[0])')"
if [ -z "$SITE_PKG" ] || [ ! -d "$SITE_PKG" ]; then
  echo "ERROR: không xác định được site-packages." >&2
  exit 1
fi
echo "[3/6] Wiring package import via shim symlink..."
SHIM_DIR="$SITE_PKG/_gpt_signup_hybrid_shim"
mkdir -p "$SHIM_DIR"
SHIM_LINK="$SHIM_DIR/$PKG_NAME"
# Refresh symlink mỗi lần (idempotent, fix khi user move folder).
rm -rf "$SHIM_LINK"
ln -s "$ROOT_DIR" "$SHIM_LINK"
echo "$SHIM_DIR" > "$SITE_PKG/_gpt_signup_hybrid_root.pth"
echo "  ✓ symlink $SHIM_LINK → $ROOT_DIR"
echo "  ✓ pth     $SITE_PKG/_gpt_signup_hybrid_root.pth → $SHIM_DIR"

# 4. Playwright Firefox (driver browser) — chỉ install nếu chưa có.
echo "[4/6] Installing Playwright Firefox (driver)..."
.venv/bin/playwright install firefox

# 5. Camoufox binary + GeoIP database.
#    Binary: idempotent (skips if up-to-date).
#    GeoIP (~66MB): cached locally, only re-download if >24h old.
echo "[5/6] Fetching Camoufox binary + GeoIP..."
.venv/bin/python -c "
from camoufox.pkgman import camoufox_path
camoufox_path(download_if_missing=True)
print('  Camoufox binary OK')

import time, shutil
from pathlib import Path
from camoufox.locale import MMDB_FILE, ALLOW_GEOIP
if not ALLOW_GEOIP:
    print('  GeoIP extra not installed, skipping')
else:
    cache = Path('runtime/geoip/GeoLite2-City.mmdb')
    need_download = True
    if cache.exists() and (time.time() - cache.stat().st_mtime) < 86400:
        MMDB_FILE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cache, MMDB_FILE)
        age_h = (time.time() - cache.stat().st_mtime) / 3600
        print(f'  GeoIP restored from cache (age: {age_h:.1f}h)')
        need_download = False
    if need_download:
        from camoufox.locale import download_mmdb
        download_mmdb()
        cache.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(MMDB_FILE, cache)
        print(f'  GeoIP cached to {cache}')
"

# 6. .env trong chính package
if [ ! -f ".env" ]; then
  echo "[6/6] Creating .env..."
  cat > .env << 'EOF'
# Browser / runtime config (đọc bởi config.load_settings)
BROWSER_ENGINE=camoufox
RUNTIME_DIR=runtime
BROWSER_VIEWPORT_WIDTH=1440
BROWSER_VIEWPORT_HEIGHT=800
BROWSER_USE_PROFILE_TEMPLATE=true
BROWSER_PROFILE_TEMPLATE_DIR=runtime/profiles/template
BROWSER_CAMOUFOX_PROFILE_DIR=runtime/profiles/camoufox_template

# Web UI config (đọc bởi web.manager)
HYBRID_MAX_CONCURRENT=2
HYBRID_OUTLOOK_PROXY=
HYBRID_JOB_TIMEOUT=240

EOF
  echo "  ✓ .env created"
else
  echo "[6/6] .env exists ✓"
fi

# Tạo runtime dirs trong package
mkdir -p \
  runtime/profiles/template \
  runtime/profiles/camoufox_template \
  runtime/sessions \
  runtime/outlook_state \
  runtime/outlook_pool \
  runtime/har_hybrid

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  ✓ Setup done. Starting web UI..."
echo "  → http://127.0.0.1:8083/"
echo ""
echo "═══════════════════════════════════════════════════════════"
echo ""

# CWD vẫn là $ROOT_DIR. Python load .pth → thấy shim dir →
# import được `gpt_signup_hybrid` qua symlink.
.venv/bin/python -m gpt_signup_hybrid web --host 127.0.0.1 --port 8083
