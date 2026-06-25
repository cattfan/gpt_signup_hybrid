# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), [SemVer](https://semver.org/).

## [3.2.0] — 2026-06-25

### Added — REG anti-ban master suite (Phase 1-9)

Reference: `docs/journals/260625-1224-reg-anti-ban-master-plan.md`

#### Foundation (Phase 1)
- `_geo_locale.py` — proxy IP → locale/timezone/geolocation auto-detect (top 15 country mapping).
- `random_profile_for_locale()` — name pool theo locale (en-IN → tên Ấn, en-US → tên Anglo).
- Settings Store: 6 keys mới (`reg.persona`, `reg.fresh_profile`, `reg.har_validate`, `reg.human_typing_delay_ms_min/max`, `reg.locale_auto_geo`).
- Helpers `read_oai_asli_from_ctx` + `read_oai_asli_from_session` đọc cookie cho `auth_session_logging_id`.

#### Browser anti-detection (Phase 2)
- `_human_input.py` — `human_type` (Gaussian 120-260ms + 8% pause), `human_click` (mousemove → jitter → click), `random_mouse_wander`, `dwell` jitter.
- State machine `password_create` chuyển sang form UI thật + `expect_response` capture.
- Mouse wander + dwell jitter ở 4 state transition critical.

#### Persona + cookie chain (Phase 3)
- `BrowserPersona` dataclass + 2 instance: `CHROME_145_WIN`, `FIREFOX_135_MAC`.
- Sentinel persona forwarding (`sentinel_quickjs` + `sentinel_pow` accept persona arg).
- `_datadog_session.py` — `_dd_s` Datadog RUM cookie generator + injector.
- Schema v12: `outlook_combos.persona_cookies` JSON column + `ComboRepository.{get,set}_persona_cookies`.

#### Pure_request optimize (Phase 4)
- `_navigate_headers` helper (page navigate Sec-Fetch-Mode).
- `_step_send_otp` đổi sang Sec-Fetch-Mode=navigate + follow 302.
- `_common_headers(persona=...)` persona-aware (Chrome có sec-ch-ua, Firefox không).
- `_step_auth_url` đọc cookie `oai-asli` cho query `auth_session_logging_id`.
- Visit `/email-verification` HTML thay `/create-account/password` XHR.

#### HAR alignment validation (Phase 5)
- `test/check_har_alignment.py` — 5 invariants × 19 sub-checks, jq-based pre-extract.
- CLI flag `--har-validate` + `_run_har_alignment_validate` post-reg auto-run.
- GitHub Actions workflow `.github/workflows/anti-ban-suite.yml` — trigger PR.

#### Closure + cleanup (Phase 6-7)
- `signup.py:run_signup` save persona_cookies sau signup successful (whitelist 7 cookies).
- `session_phase.py` locale auto-detect (Camoufox + Chrome runner).
- `session_phase.py` anti409 flow inject `_dd_s` Datadog cookie.
- Migration v11→v12 zero-data-loss verified.
- Removed dead code: `_step_signup`, `_step_register_password`, `passwordless/send-otp` fallback.
- CLI flag `--persona` (default `firefox_mac`) + `SignupRequest.persona` field.
- Runtime warning khi `reg_mode=pure_request` về so-token missing.

#### HAR audit gap fix (Phase 8)
- `_step_providers` — GET `/api/auth/providers` TRƯỚC csrf (browser thật làm vậy, ~337ms gap). Fix gap detect được khi audit HAR golden.

#### Camoufox anti-detect hardening (Phase 9)
- `block_webrtc=True` — chặn WebRTC mDNS IP leak khi dùng proxy.
- `humanize=True` — Camoufox native mouse jitter.
- `locale=list[str]` — pass `["en-IN", "en"]` để navigator.languages khớp record tay.

### Fixed
- 4 chỗ hardcode `Accept-Language: en-US,en;q=0.9` + `sec-ch-ua*` trong `request_phase.py` thay bằng `_navigate_headers()` persona-aware (`_prime_chatgpt_session`, `_step_oauth_init`, `_step_follow_redirects`, `_consume_callback`).
- `auth_session_logging_id` được đọc từ cookie `oai-asli` thay vì gen UUID mới (fix 3 chỗ: `browser_phase`, `session_phase` async + sync anti409).
- `profile_template` default = False (fresh profile mỗi reg để tránh CF cookie cluster ban).
- `_register_with_password` + `_PAGE_CREATE_ACCOUNT_JS` evaluate bypass form removed (so-token cần DOM events thật).
- Runtime bug: `NameError: 'settings' is not defined` trong state machine password_create (smoke test).
- Runtime bug: `NameError: 'logging_id' is not defined` outer scope `run_browser_phase` — dùng `logging_id_holder` nonlocal closure.

### Schema
- v11→v12: ALTER TABLE `outlook_combos` ADD COLUMN `persona_cookies TEXT`.

### Test
- 16 test/check_*.py mới (Phase 1-8 coverage).
- HAR alignment self-test 19/19 invariants PASS.
- Migration v11→v12 zero-loss test.
- Suite: `bash test/run_phase1_suite.sh` → PASS=16/16.

### Documentation
- `docs/journals/260625-1224-reg-anti-ban-master-plan.md` — full master plan.
- `test/golden_records/README.md` — golden HAR documentation.

### Operational notes
- Anti-detect hoàn chỉnh: Camoufox-Firefox 135 Mac persona, sentinel SDK in-page sinh so-token đầy đủ.
- Production REG cần proxy residential India (datacenter sẽ ban dù code perfect).
- CAPTCHA/Turnstile auto-solve defer (cần 3rd party API).
- Headless trên server không display khuyến nghị `xvfb-run`.

## [3.0.1] — earlier release

(see git log)

## [3.0.0] — earlier release

(see git log)
