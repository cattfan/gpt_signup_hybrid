#!/usr/bin/env bash
# Cross-build upi-qr-bot cho x86_64-unknown-linux-musl tu Mac.
# Reuse zig-cc/zig-cxx wrapper de build BoringSSL (wreq) + zstd-sys an toan voi musl.
set -e

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

export CC_x86_64_unknown_linux_musl="$ROOT_DIR/scripts/zig-cc.sh"
export CXX_x86_64_unknown_linux_musl="$ROOT_DIR/scripts/zig-cxx.sh"
export CC="$ROOT_DIR/scripts/zig-cc.sh"
export CXX="$ROOT_DIR/scripts/zig-cxx.sh"
chmod +x scripts/zig-cc.sh scripts/zig-cxx.sh

echo "[build] target=x86_64-unknown-linux-musl profile=release"
echo "[build] CC=$CC"
echo "[build] zig version: $(zig version)"
echo "[build] start $(date +%H:%M:%S)"

cargo zigbuild --release --target x86_64-unknown-linux-musl

BIN=target/x86_64-unknown-linux-musl/release/upi-qr-bot
[ -f "$BIN" ] || { echo "[build] FAIL: binary not produced at $BIN"; exit 1; }

SIZE=$(stat -f%z "$BIN" 2>/dev/null || stat -c%s "$BIN")
echo "[build] OK $(date +%H:%M:%S) — $BIN ($SIZE bytes)"
file "$BIN" 2>/dev/null || true
