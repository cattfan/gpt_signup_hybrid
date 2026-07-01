#!/usr/bin/env bash
# Deploy upi-qr-bot len VPS Ubuntu/Debian x86_64 (systemd).
# Yeu cau: da co binary o target/x86_64-unknown-linux-musl/release/upi-qr-bot
# Cu phap: bash scripts/deploy-vps.sh <host> [user]
# Hardcode password QUA bien moi truong VPS_SSH_PASS (sshpass) hoac dung SSH key.

set -euo pipefail

HOST="${1:?usage: deploy-vps.sh <host> [user]}"
USER="${2:-root}"
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

BIN="target/x86_64-unknown-linux-musl/release/upi-qr-bot"
SERVICE="scripts/upi-qr-bot.service"

[ -f "$BIN" ] || { echo "[deploy] missing $BIN — chay build-x86_64.sh truoc"; exit 1; }
[ -f "$SERVICE" ] || { echo "[deploy] missing $SERVICE"; exit 1; }

# Wrapper de ssh/scp non-interactive bang sshpass neu co VPS_SSH_PASS
if [ -n "${VPS_SSH_PASS:-}" ] && command -v sshpass >/dev/null 2>&1; then
  SSH_CMD=(sshpass -p "$VPS_SSH_PASS" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=20 -o PreferredAuthentications=password -o PubkeyAuthentication=no -o NumberOfPasswordPrompts=1)
  SCP_CMD=(sshpass -p "$VPS_SSH_PASS" scp -o StrictHostKeyChecking=no -o ConnectTimeout=20 -o PreferredAuthentications=password -o PubkeyAuthentication=no -o NumberOfPasswordPrompts=1)
else
  SSH_CMD=(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=20)
  SCP_CMD=(scp -o StrictHostKeyChecking=no -o ConnectTimeout=20)
fi

echo "[deploy] target host=$HOST user=$USER"

echo "[deploy] scp binary -> /usr/local/bin/upi-qr-bot.new"
"${SCP_CMD[@]}" "$BIN" "$USER@$HOST:/usr/local/bin/upi-qr-bot.new"

echo "[deploy] scp systemd unit"
"${SCP_CMD[@]}" "$SERVICE" "$USER@$HOST:/etc/systemd/system/upi-qr-bot.service"

echo "[deploy] scp env example"
"${SCP_CMD[@]}" "scripts/upi-qr-bot.env.example" "$USER@$HOST:/etc/upi-qr-bot.env.example"

echo "[deploy] remote setup (no start)"
"${SSH_CMD[@]}" "$USER@$HOST" 'bash -s' <<'REMOTE'
set -e
install -m 0755 /usr/local/bin/upi-qr-bot.new /usr/local/bin/upi-qr-bot
rm -f /usr/local/bin/upi-qr-bot.new
# data dir cho DB SQLite
install -d -m 0750 /var/lib/upi-qr-bot
# env file - khong overwrite neu da co
if [ ! -f /etc/upi-qr-bot.env ]; then
  cp /etc/upi-qr-bot.env.example /etc/upi-qr-bot.env
  chmod 600 /etc/upi-qr-bot.env
  echo "[remote] da copy env.example -> /etc/upi-qr-bot.env (CAN SUA TRUOC KHI START)"
else
  echo "[remote] /etc/upi-qr-bot.env da ton tai, giu nguyen"
fi
chmod 600 /etc/upi-qr-bot.env
systemctl daemon-reload
systemctl enable upi-qr-bot.service >/dev/null 2>&1 || true
echo "[remote] binary: $(/usr/local/bin/upi-qr-bot --version 2>&1 | head -1 || echo 'no --version flag')"
echo "[remote] systemctl status:"
systemctl status upi-qr-bot.service --no-pager 2>&1 | head -5 || true
REMOTE

echo "[deploy] DONE. Cac buoc tiep:"
echo "  1. Sua /etc/upi-qr-bot.env tren $HOST (token, proxy, DB_PATH=/var/lib/upi-qr-bot/state.db)"
echo "  2. Stop bot tren server cu (procd: /etc/init.d/upi-qr-bot stop)"
echo "  3. Start: ssh $USER@$HOST 'systemctl start upi-qr-bot && journalctl -u upi-qr-bot -f'"
