# gpt_signup_hybrid

Tool tự động đăng ký ChatGPT + bật 2FA + lấy session/payment link.

## Kiến trúc tổng quan

```
gpt_signup_hybrid/
├── Python Backend (FastAPI + Camoufox + curl_cffi)
│   ├── Web UI: http://127.0.0.1:8083
│   ├── 3 tab: Reg | Get Session | Get Link
│   └── iCloud HME pool management
├── gopay-link-service/ (Rust — Axum)
│   └── Standalone service lấy GoPay/Midtrans payment link
└── gopay-checker-extension/ (Chrome Extension MV3)
    └── Check GoPay phone registration via Midtrans API
```

## Stack

| Component | Tech |
|-----------|------|
| Backend | Python 3.13, FastAPI, Camoufox (Firefox stealth), curl_cffi, Playwright |
| Database | SQLite (single file `runtime/data.db`) |
| Payment Link Service | Rust, Axum, reqwest, tokio |
| Browser Extension | Chrome Manifest V3, Service Worker |

## Docs

- [UPI QR Local API](docs/upi_qr_api.md) — standalone local API nhan `account_line` va tra QR image.

---

## 1. Python Backend

### Chức năng chính

- **Reg**: Signup ChatGPT batch (Camoufox headless → fill form → OTP → 2FA)
- **Get Session**: Login lại account → extract session JSON (`accessToken`)
- **Get Link**: Lấy payment URL `pay.openai.com` cho GoPay/Stripe
- **iCloud HME**: Quản lý pool Apple Hide My Email cho auto-reg
- **AutoReg**: Poll iCloud emails mới → tự động signup

### Mail providers

| Mode | Input format |
|------|-------------|
| Outlook | `email\|password\|refresh_token\|client_id` |
| iCloud Worker | `email` (worker tự nhận OTP) |
| Gmail Advanced | `email\|api_key` (checkgmail.live API) |

### Setup (1 lệnh)

```bash
# macOS / Linux
cd gpt_signup_hybrid
bash setup.sh
# → http://127.0.0.1:8083/

# Windows
setup.bat
```

**Yêu cầu**: Python 3.13, internet.

Script tự động:
1. Tạo `.venv/` + install deps
2. Cài Playwright Firefox + Camoufox binary
3. Tạo `.env` + runtime dirs
4. Start web UI

### Chạy lại

```bash
# macOS
.venv/bin/python -m gpt_signup_hybrid web --host 127.0.0.1 --port 8083

# Windows
.venv\Scripts\python -m gpt_signup_hybrid web
```

### CLI

```bash
.venv/bin/python -m gpt_signup_hybrid signup --email foo@icloud.com
.venv/bin/python -m gpt_signup_hybrid totp --secret BASE32SECRET
.venv/bin/python -m gpt_signup_hybrid enable-2fa --email x --password y
```

### Cấu trúc module

```
├── __main__.py          # Entry: python -m gpt_signup_hybrid
├── cli.py               # Typer CLI (signup, web, totp, enable-2fa, pool-status, migrate)
├── config.py            # Settings dataclass, .env parsing
├── models.py            # Pydantic models (SignupRequest, SignupResult)
├── signup.py            # Orchestrator: Phase1 → Phase2 → MFA
├── browser_phase.py     # Phase 1: Camoufox state machine (fill form, OTP)
├── http_phase.py        # Phase 2: curl_cffi extract session token
├── mfa_phase.py         # Enable TOTP 2FA
├── session_phase.py     # Login + extract session
├── payment_link.py      # Stripe checkout → payment URL
├── mail_providers.py    # Outlook/iCloud/Gmail providers
├── outlook_pool.py      # Outlook combo pool management
├── random_profile.py    # Random name/age generator
├── totp_helper.py       # TOTP utilities
├── web/                 # FastAPI server + static UI
│   ├── server.py        # App factory, routes, startup
│   ├── manager.py       # JobManager (queue + workers + SSE)
│   ├── mail_modes.py    # Mail mode registry
│   ├── auth.py          # Token auth
│   ├── sse_mux.py       # Multiplexed SSE
│   └── static/          # HTML/JS/CSS frontend
├── db/                  # SQLite persistence
│   ├── engine.py        # Connection management
│   ├── schema.py        # DDL + migrations (v1→v11)
│   └── repositories.py  # CRUD repos (Settings, Jobs, Combos, Sessions)
├── icloud_hme/          # iCloud Hide My Email pool
│   ├── runner.py        # HmeRunner (generate/check/manage cycles)
│   ├── generator.py     # Create HME addresses
│   ├── client.py        # Apple API client (httpx)
│   ├── pool.py          # Multi-profile pool manager
│   └── web/             # Separate FastAPI router
└── autoreg/             # Auto-registration runner
    └── runner.py        # Poll + signup pipeline
```

### Environment (.env)

```env
BROWSER_ENGINE=camoufox
RUNTIME_DIR=runtime
HYBRID_MAX_CONCURRENT=2          # Concurrent jobs [1-10]
HYBRID_OUTLOOK_PROXY=            # http://user:pass@host:port
HYBRID_JOB_TIMEOUT=240           # Seconds [30-600]
ICLOUD_API_AUTH_TOKEN=           # Auth token for iCloud API
```

---

## 2. gopay-link-service (Rust)

Standalone HTTP service lấy GoPay/Midtrans payment link từ ChatGPT access token.

### Flow

```
access_token → ChatGPT checkout API → Stripe init
→ create GoPay payment method → confirm → redirect → Midtrans URL
```

### Build & Run

```bash
cd gopay-link-service
cargo build --release
# Binary: target/release/gopay-link-service

# Run (default port 8899)
PORT=8899 MAX_CONCURRENT=5 ./target/release/gopay-link-service
```

### API

```
POST /api/gopay
Content-Type: application/json

# Option 1: access_token trực tiếp
{"access_token": "eyJhbGci..."}

# Option 2: session JSON (tự extract accessToken)
{"session_json": "{\"accessToken\":\"eyJ...\"}"}

Response:
{"success": true, "gopay_link": "https://app.midtrans.com/...", "payment_link": "https://pay.openai.com/..."}
```

### Deploy

- Init scripts trong `deploy/` cho systemd (gopay-link-service + cloudflared tunnel)
- Web UI tại `/` (paste session → get link)

### Cấu trúc

```
gopay-link-service/
├── Cargo.toml
├── Cargo.lock
├── src/
│   ├── main.rs          # Axum router, rate limiter, handlers
│   └── gopay.rs         # Stripe/Midtrans flow logic
├── static/
│   └── index.html       # Web UI
└── deploy/
    ├── gopay-link-service.init
    └── cloudflared-tunnel.init
```

---

## 3. gopay-checker-extension (Chrome Extension)

Chrome Extension (Manifest V3) check số điện thoại đã đăng ký GoPay chưa, qua Midtrans API.

### Chức năng

- **Popup**: Paste session JSON → tự tạo snap token → check phone
- **Content Script (hero-sms.com)**: Tự detect số điện thoại trên trang → check GoPay → hiện badge ✓/✗
- **Content Script (chatgpt.com)**: Extract session/accessToken từ cookie

### Cài đặt

1. Chrome → `chrome://extensions/` → Developer mode ON
2. "Load unpacked" → chọn folder `gopay-checker-extension/`
3. Pin extension, mở popup, paste session JSON

### Cấu trúc

```
gopay-checker-extension/
├── manifest.json          # MV3 config
├── background.js          # Service Worker: HMAC signature, phone check queue
├── content-herosms.js     # Auto-detect + badge trên hero-sms.com
├── content-chatgpt.js     # Extract session từ chatgpt.com
├── content-midtrans.js    # Midtrans page integration
├── inject-midtrans.js     # Inject helper
├── popup.html             # Extension popup UI
├── popup.js               # Popup logic
├── badge.css              # Badge styles
└── icons/                 # Extension icons (16/48/128)
```

---

## Runtime data

Tất cả data runtime nằm trong `runtime/` (gitignored):

```
runtime/
├── data.db              # SQLite database (single source of truth)
├── profiles/            # Browser profile templates
├── sessions/            # Signup result JSON
├── outlook_state/       # Token rotation state
└── har_hybrid/          # HAR debug captures
```

---

## License

Private / Internal use only.
