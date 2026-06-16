# gpt_signup_hybrid

Tự động hoá vòng đời tài khoản ChatGPT: **đăng ký hàng loạt → bật 2FA → lấy session → lấy QR/link thanh toán**, điều khiển qua Web UI cục bộ hoặc CLI.

> **Version:** 2.0.0 · **Runtime:** Python 3.13 · **UI:** FastAPI + static JS · **DB:** SQLite

---

## Mục lục

- [Tính năng](#tính-năng)
- [Kiến trúc](#kiến-trúc)
- [Stack](#stack)
- [Cài đặt nhanh](#cài-đặt-nhanh)
- [Web UI](#web-ui)
- [CLI](#cli)
- [Cấu hình runtime (Settings Store)](#cấu-hình-runtime-settings-store)
- [Cấu trúc mã nguồn](#cấu-trúc-mã-nguồn)
- [Mô hình dữ liệu](#mô-hình-dữ-liệu)
- [Tích hợp ngoài](#tích-hợp-ngoài)
- [Kiểm thử](#kiểm-thử)
- [Bảo mật](#bảo-mật)

---

## Tính năng

| Tab | Mô tả |
|-----|-------|
| **Reg** | Đăng ký ChatGPT hàng loạt: Camoufox (anti-detect) hoặc pure HTTP → điền form → nhận OTP từ mail provider → bật 2FA (TOTP). |
| **Get Session** | Đăng nhập lại account → trích xuất session JSON + `accessToken`, hiển thị `planType` (free/plus/team). |
| **UPI QR** | Lấy QR thanh toán UPI cho ChatGPT Plus India: login → tạo checkout Stripe → confirm UPI → approve loop → render QR. Tải ảnh về local (Blob), xem qua modal. |
| **Settings** | Quản lý proxy pool (round-robin/random), test proxy, các cấu hình runtime. |
| **iCloud HME** *(ẩn mặc định)* | Quản lý pool Apple Hide My Email, sinh email tự động cho auto-reg. |
| **Get Link** *(ẩn mặc định)* | Lấy payment URL `pay.openai.com` cho GoPay/Stripe theo region. |

**Cơ chế chung:**
- **Multi-job song song** — chế độ Single / Multi 2 · 3 · 5 · 10 · 20 · 30 · 50 workers.
- **Realtime** — toàn bộ trạng thái job + log đẩy qua SSE (`/api/sse`) tới frontend.
- **Proxy pool** — xoay vòng, tự loại proxy chết khi gặp lỗi network.
- **Recovery** — job đang chạy lúc restart được khôi phục từ SQLite.

### Mail providers

| Mode | Input format |
|------|-------------|
| Outlook (Microsoft Graph) | `email\|password\|refresh_token\|client_id` |
| DongVanFB Outlook | `email\|password\|refresh_token\|client_id` |
| Cloudflare Worker | `email` (worker tự nhận OTP) |
| Gmail Advanced | `email\|api_key` (checkgmail.live) |

---

## Kiến trúc

Monolith Python theo package, gồm 1 web runtime (FastAPI) + nhiều job manager async + repository SQLite + frontend static.

```
┌────────────────────────────────────────────────────────────────┐
│  Frontend tĩnh (web/static/*.js) — không bundler                 │
│  index.html · app.js · session.js · upi.js · settings_panel.js   │
│        │ fetch /api/*          │ EventSource /api/sse            │
└────────┼───────────────────────┼────────────────────────────────┘
         ▼                       ▼
┌────────────────────────────────────────────────────────────────┐
│  web/server.py — FastAPI app                                     │
│  • Auth middleware (token)   • REST /api/*    • SSE mux           │
│  • startup: hydrate Settings → apply_settings() cho managers     │
└───┬──────────────┬───────────────┬───────────────┬──────────────┘
    ▼              ▼               ▼               ▼
┌────────┐  ┌────────────┐  ┌────────────┐  ┌──────────────┐
│JobMgr  │  │SessionMgr  │  │UpiJobMgr   │  │LinkMgr/HME    │
│(Reg)   │  │(Get Sess)  │  │(UPI QR)    │  │AutoReg        │
└───┬────┘  └─────┬──────┘  └─────┬──────┘  └──────┬───────┘
    ▼            ▼               ▼                ▼
┌────────────────────────────────────────────────────────────────┐
│  Domain layer                                                    │
│  signup.py · session_phase.py · pay_upi_http.py · upi_runner.py  │
│  payment_link.py · stripe_token.py · mfa_phase.py                │
└───┬──────────────────────────────────────────────────┬─────────┘
    ▼                                                    ▼
┌──────────────────────┐                  ┌──────────────────────┐
│ db/ (SQLite)         │                  │ External APIs        │
│ engine · schema      │                  │ ChatGPT/OpenAI       │
│ repositories         │                  │ Stripe · iCloud      │
│ SettingsRepository   │                  │ Outlook · Worker mail│
└──────────────────────┘                  └──────────────────────┘
```

**Luồng dữ liệu chính (ví dụ UPI QR):**
1. `web/static/upi.js` → `POST /api/upi/jobs` (danh sách `email|password|secret`).
2. `UpiJobManager` (`web/manager.py`) tạo job in-memory, enqueue, broadcast SSE.
3. Worker gọi `web/upi_runner.run_upi_qr_probe()`:
   - `session_phase.get_session_pure_request()` → login lấy `accessToken` (direct, không proxy).
   - Tạo ChatGPT checkout → `pay_upi_http._stripe_init` → elements session.
   - `stripe_token.extract_config_live()` sinh `js_checksum`/`rv_timestamp`.
   - Confirm UPI theo nhiều variant → approve retry loop (rotate proxy).
   - Trích QR image URL / `upi://` URI → render PNG bằng `qrcode`.
4. Job lưu `qr_path` → frontend tải ảnh về Blob, hiển thị qua modal.

---

## Stack

| Thành phần | Công nghệ |
|-----------|-----------|
| Ngôn ngữ | Python 3.13 (backend), JS/HTML/CSS (frontend, không framework) |
| Web | FastAPI 0.136 · Starlette · Uvicorn |
| CLI | Typer · Rich |
| HTTP client | curl_cffi (TLS impersonate) · httpx · requests |
| Browser automation | Camoufox 0.4.11 (Firefox stealth) · Playwright 1.49 |
| Validation | Pydantic 2.13 |
| Database | SQLite (stdlib `sqlite3`, WAL) — `runtime/data.db` |
| 2FA / QR | pyotp · qrcode[pil] |

---

## Cài đặt nhanh

**Yêu cầu:** Python 3.13, internet (tải deps + browser binary).

```bash
# macOS / Linux
bash setup.sh

# Windows
setup.bat
```

Script tự động: tạo `.venv/` → cài `requirements.txt` → cài Playwright Firefox + Camoufox → tạo runtime dirs → khởi động Web UI tại `http://127.0.0.1:8083/`.

**Chạy lại sau khi đã cài:**

```bash
# macOS / Linux
.venv/bin/python -m gpt_signup_hybrid web --host 127.0.0.1 --port 8083

# Windows
.venv\Scripts\python -m gpt_signup_hybrid web
```

---

## Web UI

Mặc định bind **loopback** `127.0.0.1:8083`. Token auth tự sinh và được inject vào trang khi truy cập qua loopback.

Bind ra LAN/public phải opt-in rõ ràng (UI lộ credential + điều khiển job):

```bash
.venv/bin/python -m gpt_signup_hybrid web --host 0.0.0.0 --unsafe-expose-network
# → in ra AUTH TOKEN, truyền qua ?token=... trên URL
```

---

## CLI

```bash
python -m gpt_signup_hybrid <command>
```

| Lệnh | Mô tả |
|------|-------|
| `web` | Khởi động Web UI (`--host`, `--port`, `--reload`, `--unsafe-expose-network`). |
| `signup` | Đăng ký 1 account từ CLI. |
| `enable-2fa` | Bật TOTP 2FA cho account (`--session-file`). |
| `totp` | Sinh mã TOTP hiện tại từ base32 secret. |
| `record` | Chạy hybrid browser recorder (debug payment flow). |
| `pool-status` | Xem trạng thái Outlook combo pool. |
| `import-pool` | Import combo pool vào SQLite. |
| `migrate` | Chạy migration schema DB. |

Các entry point khác:

```bash
python -m gpt_signup_hybrid.pay_upi_http   --combo 'EMAIL|PASS|SECRET'   # test UPI thuần HTTP
python -m gpt_signup_hybrid.icloud_hme      ...                          # CLI quản lý iCloud HME
```

---

## Cấu hình runtime (Settings Store)

> **Quy tắc cốt lõi:** Mọi cấu hình runtime là **single source of truth** trong bảng SQLite `settings`, truy cập qua `SettingsRepository` (`db/repositories.py`). **Không** dùng file JSON/YAML riêng, **không** dùng `localStorage` cho config.

- **Backend** đọc settings tại startup (`apply_settings()`), ghi write-through khi user đổi.
- **Frontend** dùng `Settings.get(key)` / `Settings.save(key, value, token)` (`web/static/settings.js`).
- **Key mới** phải thêm vào `_EXACT_KEYS` + `_validate_type_constraint()` trước khi dùng.
- Namespace `namespace.field` (dot-separated, lowercase): `reg.headless`, `proxy.pool`, `upi.approve_retries`...

`.env` chỉ dùng cho **bootstrap/default** (không phải runtime config):

```env
BROWSER_ENGINE=camoufox
RUNTIME_DIR=runtime
HYBRID_MAX_CONCURRENT=2        # [1-10]
HYBRID_JOB_TIMEOUT=240         # giây [30-600], phải > OTP timeout 180s
HYBRID_OUTLOOK_PROXY=          # seed proxy pool (khuyến nghị cấu hình trong UI)
ICLOUD_API_AUTH_TOKEN=         # token cho iCloud API
GPT_SIGNUP_WEB_TOKEN=          # override web auth token (mặc định tự sinh)
```

Xem đầy đủ trong [`.env.example`](.env.example).

---

## Cấu trúc mã nguồn

```
gpt_signup_hybrid/
├── __main__.py / gpt_signup_hybrid.py   # Entry: python -m gpt_signup_hybrid
├── cli.py                  # Typer CLI (web, signup, totp, enable-2fa, migrate, ...)
├── config.py               # Settings dataclass + .env parsing + runtime paths
├── models.py               # Pydantic: SignupRequest, SignupResult, BrowserHandoff
│
│  ── Domain: signup ──
├── signup.py               # Orchestrator: browser/request phase → http → MFA
├── browser_phase.py        # Phase browser (Camoufox state machine)
├── request_phase.py        # Phase pure HTTP (curl_cffi)
├── http_phase.py           # Trích session/access token sau browser phase
├── session_phase.py        # Login lại + lấy session JSON
├── mfa_phase.py            # Bật TOTP 2FA
├── mail_providers.py       # Outlook/DongVanFB/Worker/Gmail providers
├── outlook_pool.py         # Quản lý Outlook combo pool
├── sentinel_*.py           # Xử lý OpenAI Sentinel challenge (PoW + QuickJS VM)
│
│  ── Domain: payment ──
├── payment_link.py         # ChatGPT checkout → Stripe → payment/GoPay URL
├── pay_upi_http.py         # Flow UPI thuần HTTP (constants + Stripe helpers)
├── stripe_token.py         # Trích js_checksum / rv_timestamp từ bundle Stripe
├── record_pay_upi.py       # Hybrid recorder (debug)
│
├── web/
│   ├── server.py           # FastAPI app: routes, auth middleware, startup/shutdown
│   ├── manager.py          # JobManager · SessionJobManager · LinkJobManager · UpiJobManager
│   ├── upi_runner.py       # Async probe lấy QR UPI cho từng account
│   ├── sse_mux.py          # SSE fan-out đa kênh + snapshot
│   ├── auth.py             # Token auth (header/query/cookie)
│   ├── proxy_pool.py       # Proxy pool (round-robin/random, mark-dead)
│   ├── mail_modes.py       # Registry mail mode cho UI/backend
│   ├── icloud_routes.py    # Router /api/icloud/* + AutoReg
│   └── static/             # Frontend: index.html, app.js, session.js, upi.js, ...
│
├── db/
│   ├── engine.py           # DatabaseEngine: WAL, transaction, migration
│   ├── schema.py           # DDL + migrations (CURRENT_VERSION)
│   ├── repositories.py     # Combo/Job/Session/Settings/iCloud/ChatGptAccount repos
│   └── __init__.py         # get_engine() · get_repos() · get_settings_repo()
│
├── icloud_hme/             # Apple Hide My Email: pool, generator, runner, client
├── autoreg/                # AutoReg runner (poll HME emails → signup)
├── codex_auth/             # Codex OAuth helper (PKCE)
├── test/                   # Verification scripts (check_*, smoke_*, test_*)
└── docs/                   # Tài liệu (pay_upi_runbook, upi_qr_api)
```

---

## Mô hình dữ liệu

SQLite single-file `runtime/data.db`, schema versioned trong `db/schema.py`:

| Bảng | Vai trò |
|------|---------|
| `settings` | Single source of truth cho runtime config (KV, JSON value). |
| `jobs` + `job_logs` | Job lifecycle (signup/session/link) + log dòng. |
| `outlook_combos` | Outlook combo pool + trạng thái sử dụng. |
| `session_results` | Session JSON + MFA pending state. |
| `chatgpt_accounts` | Account tạo bởi AutoReg. |
| `icloud_accounts` · `icloud_emails` · `icloud_audit_log` · `pool_state` | iCloud HME pool. |

> **Lưu ý:** UPI QR job là **in-memory** (`UpiJobManager`), không persist DB — vòng đời ngắn, chạy lại được. QR PNG lưu tạm tại `runtime/upi_qr/`.

---

## Tích hợp ngoài

| Dịch vụ | Module | Mục đích |
|---------|--------|----------|
| ChatGPT / OpenAI | `browser_phase`, `request_phase`, `session_phase` | Signup, login, session, sentinel. |
| Stripe | `pay_upi_http`, `payment_link`, `stripe_token` | Checkout, elements, confirm UPI, token. |
| Apple iCloud HME | `icloud_hme/client`, `icloud_hme/session` | Sinh/quản lý Hide My Email. |
| Microsoft Graph / DongVanFB | `mail_providers` | Poll OTP Outlook. |
| Cloudflare Worker / Gmail | `mail_providers` | Poll OTP qua worker/API. |

---

## Kiểm thử

Toàn bộ verification script nằm trong `test/`, chạy trực tiếp bằng file (không inline `python -c`):

```bash
.venv/bin/python test/check_upi_module_imports.py    # 12 unit check module UPI
.venv/bin/python test/smoke_upi_server_boot.py       # 12 integration check FastAPI TestClient
```

Quy ước đặt tên: `check_<scope>.py` (check chức năng), `smoke_<scope>.py` (smoke integration), `test_<scope>.py` (unit/pytest), `probe_<scope>.py` (research).

---

## Bảo mật

- **Auth token** gate toàn bộ `/api/*` (header `X-API-Token`, query `?token=`, hoặc cookie `gsh_token`).
- **Loopback-first** — bind ra ngoài phải `--unsafe-expose-network`.
- **Redact** — proxy pool, API key, worker config, auth token được che trong audit log.
- **Proxy** — credential nhúng trong URL được mask khi log.
- Đây là tool **nội bộ / cục bộ**; không có hardening cho deploy public.

---

## License

Private / Internal use only.
