"""Job manager: queue + concurrency control + broadcast events."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ..config import load_settings, runtime_session_dir
from ..mail_providers import OutlookCombo, OutlookComboError
from ..mfa_phase import MfaError, enable_2fa
from ..models import SignupRequest, SignupResult
from ..signup import run_signup
from .._browser_retry import NETWORK_ERROR_MARKERS
from .mail_modes import MailModeParseError, get_spec
from .proxy_pool import get_proxy_pool

if TYPE_CHECKING:
    from ..db.repositories import ComboRepository, JobRepository, SessionResultRepository
    from .sse_mux import SseMux

_log = logging.getLogger(__name__)

# ── Unified SSE Mux hook (set by server.py at startup to avoid circular import) ──
_sse_mux: "SseMux | None" = None


def set_sse_mux(mux: "SseMux") -> None:
    """Inject the SseMux singleton from server.py (avoids circular import)."""
    global _sse_mux
    _sse_mux = mux


_NO_RETRY_ERROR_KEYS = (
    "OutlookComboError",
    "invalid_grant",
    "service abuse",
    "AADSTS70000",
    "AADSTS50173",
    "AADSTS70008",
    "AADSTS50034",
    "AADSTS50057",
    "combo dead",
)


def _is_fatal_error(error: str | None) -> bool:
    if not error:
        return False
    error_lower = error.lower()
    return any(k.lower() in error_lower for k in _NO_RETRY_ERROR_KEYS)


# ── Aggregated log helpers ────────────────────────────────────────────


def _append_account_log(
    *, session_dir: Path, email: str, password: str, totp_secret: str,
) -> None:
    """Append 1 dòng email|password|totp_secret vào accounts.txt tổng hợp."""
    log_path = session_dir / "accounts.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{email}|{password}|{totp_secret}\n"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line)


def _append_link_log(*, session_dir: Path, payment_link: str) -> None:
    """Append payment_link vào links.txt."""
    log_path = session_dir / "links.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"{payment_link}\n")


# ── Load .env riêng của gpt_signup_hybrid ─────────────────────────────
def _load_hybrid_env() -> dict[str, str]:
    """Đọc gpt_signup_hybrid/.env (cùng thư mục package root)."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip("'\"")
    return values


_HYBRID_ENV = _load_hybrid_env()


def _env(key: str, default: str) -> str:
    """Ưu tiên: os.environ > .env file > default."""
    return os.environ.get(key) or _HYBRID_ENV.get(key) or default


# Parsed constants
_MAX_CONCURRENT = min(max(int(_env("HYBRID_MAX_CONCURRENT", "2")), 1), 10)
_DEFAULT_JOB_TIMEOUT = min(max(float(_env("HYBRID_JOB_TIMEOUT", "240")), 30), 600)
_DEFAULT_PROXY = _env("HYBRID_OUTLOOK_PROXY", "") or None

# Mail modes dùng Outlook combo 4 phần (email|password|refresh_token|client_id) →
# tracked qua outlook_combos table. Cả "outlook" (cascade Microsoft+DongVanFB) và
# "dongvanfb" (direct DongVanFB) đều cần mark_success/mark_failure.
_OUTLOOK_COMBO_MODES = frozenset({"outlook", "dongvanfb"})


def _mask_proxy(proxy: str | None) -> str:
    """Mask user:pass trong proxy URL cho log. None/empty → 'direct'."""
    if not proxy:
        return "direct"
    if "@" in proxy:
        scheme_split = proxy.split("://", 1)
        if len(scheme_split) == 2:
            scheme, rest = scheme_split
            _, _, host = rest.partition("@")
            return f"{scheme}://***@{host}"
    return proxy


def _is_proxy_network_error(exc_or_msg) -> bool:
    """True nếu lỗi là network/proxy → đáng để mark proxy chết.

    Nhận exception hoặc string message. Dùng cho proxy pool rotation: khi 1 job
    fail vì proxy không kết nối được, loại proxy đó khỏi vòng xoay để job sau
    không dính lại.
    """
    if exc_or_msg is None:
        return False
    msg = str(exc_or_msg)
    if not msg:
        return False
    return any(marker in msg for marker in NETWORK_ERROR_MARKERS)


def _resolve_job_proxy() -> str | None:
    """Resolve proxy cho 1 job từ proxy pool (round_robin/random).

    - Pool có proxy live → pick 1 để tránh trùng IP giữa các job.
    - Pool rỗng / hết proxy live → None (direct).

    Proxy pool là nguồn cấu hình proxy DUY NHẤT (single source of truth) —
    không còn proxy đơn riêng lẻ.
    """
    pool = get_proxy_pool()
    if pool.is_active():
        return pool.pick()
    return None


async def run_with_proxy_rotation(
    func,
    *,
    log=None,
    max_attempts: int | None = None,
):
    """Chạy ``func(proxy)`` với proxy xoay từ pool, tự loại proxy chết.

    Dùng cho các luồng HTTP API one-shot (không phải job worker) cần proxy —
    vd endpoint extension GoPay snap-token. Khác với JobManager worker đã có
    ``_begin_job_proxy`` / ``_note_proxy_failure``, helper này gom logic đó lại
    cho caller ngoài worker.

    Cơ chế:
      - Pool rỗng/không active → gọi ``func(None)`` đúng 1 lần (direct).
      - Pool active → pick proxy live, gọi ``func(proxy)``. Nếu raise lỗi
        network/proxy (``_is_proxy_network_error``) → mark-dead proxy đó rồi thử
        proxy live kế tiếp. Lỗi KHÔNG phải network (vd Stripe decline) → raise
        ngay, không retry (fail-fast, không che lỗi nghiệp vụ).
      - Hết proxy live mà vẫn lỗi network → raise lỗi cuối cùng.

    Args:
        func: async callable nhận 1 tham số ``proxy: str | None``.
        log: optional callable(str) để log proxy đang dùng (đã mask).
        max_attempts: giới hạn số proxy thử (default = số proxy live trong pool).

    Returns:
        Giá trị trả về của ``func`` ở lần thành công đầu tiên.
    """
    pool = get_proxy_pool()
    if not pool.is_active():
        return await func(None)

    attempts = max_attempts if max_attempts is not None else max(1, len(pool.live_entries()))
    last_exc: Exception | None = None

    for _ in range(attempts):
        proxy = pool.pick()
        if proxy is None:
            break  # hết proxy live
        if log:
            log(f"[proxy] dùng {_mask_proxy(proxy)}")
        try:
            return await func(proxy)
        except Exception as exc:  # noqa: BLE001
            if not _is_proxy_network_error(exc):
                # Lỗi nghiệp vụ (Stripe decline, session expired...) → không retry.
                raise
            last_exc = exc
            if pool.mark_dead(proxy) and log:
                log(f"[proxy] {_mask_proxy(proxy)} lỗi network — loại khỏi pool")

    if last_exc is not None:
        raise last_exc
    # Hết proxy live ngay từ đầu (pick trả None) → chạy direct fail-fast.
    return await func(None)


def _hydrate_proxy_pool_from_settings(settings: dict) -> None:
    """Cấu hình ProxyPool singleton từ settings dict (gọi 1 lần lúc startup).

    Idempotent — 3 manager đều gọi nhưng configure() ghi đè cùng giá trị.
    """
    proxies = settings.get("proxy.pool") if "proxy.pool" in settings else None
    mode = settings.get("proxy.rotation_mode") if "proxy.rotation_mode" in settings else None
    if proxies is not None or mode is not None:
        get_proxy_pool().configure(proxies, mode=mode)


def _seed_proxy_pool_from_env() -> None:
    """Seed pool với env ``HYBRID_OUTLOOK_PROXY`` (backward-compat).

    Chỉ seed khi pool đang rỗng. Settings Store (``proxy.pool``) sẽ ghi đè khi
    ``apply_settings`` chạy lúc startup nếu DB đã có cấu hình.
    """
    if _DEFAULT_PROXY and get_proxy_pool().size == 0:
        get_proxy_pool().configure([_DEFAULT_PROXY])


_seed_proxy_pool_from_env()




def _make_on_enroll_callback(
    session_repo: "SessionResultRepository | None",
    *,
    email: str,
    log,
):
    """Tạo callback persist mfa_pending vào DB sau khi enroll OK.

    Best-effort: callback raise → log warning, NHƯNG vẫn tiếp tục activate.
    `enable_2fa` đã wrap callback trong try/except.

    Trả về None nếu session_repo không khả dụng — caller pass None vào
    `enable_2fa` thay vì pass callback no-op.
    """
    if session_repo is None:
        return None

    async def _on_enroll(state: dict) -> None:
        # set_mfa_pending là sync DB write — chạy trong thread pool để không
        # block event loop trên SQLite I/O.
        await asyncio.to_thread(session_repo.set_mfa_pending, email, state)
        log(
            f"[mfa] pending enrollment persisted "
            f"factor_id={(state.get('factor_id') or '')[:20]} secret_len={len(state.get('secret') or '')}"
        )

    return _on_enroll


def _persist_partial_state_sync(
    session_repo: "SessionResultRepository | None",
    *,
    email: str,
    partial_state: dict | None,
    log,
) -> None:
    """Persist partial_state từ MfaError vào mfa_pending (sync, dùng trong except).

    Best-effort: lỗi DB → log warning. Mục đích chỉ để tránh mất secret khi
    activate fail (on_enroll có thể đã chạy rồi nhưng chạy lại cũng idempotent).
    """
    if session_repo is None or not partial_state or not partial_state.get("secret"):
        return
    try:
        session_repo.set_mfa_pending(email, {
            "secret": partial_state["secret"],
            "factor_id": partial_state.get("factor_id"),
            "session_id": partial_state.get("session_id"),
            "status": "enrolled",
        })
        log(
            f"[mfa] partial_state persisted (activate fail recovery) "
            f"secret_len={len(partial_state['secret'])}"
        )
    except Exception as exc:
        _log.warning("persist partial mfa state for %s failed: %s", email, exc)


JobStatus = str  # queued | running | success | error | cancelled


@dataclass
class Job:
    id: str
    email: str
    combo: str  # raw combo line
    mail_mode: str = "outlook"
    reg_mode: str = "browser"  # "pure_request" or "browser"
    status: JobStatus = "queued"
    log_lines: list[str] = field(default_factory=list)
    error: str | None = None
    # Output sau khi success
    password: str | None = None
    secret: str | None = None
    first_code: str | None = None
    user_id: str | None = None
    session_path: str | None = None
    # Post-reg optional results
    session_data: dict[str, Any] | None = None  # post-reg session JSON
    payment_link: str | None = None  # post-reg payment URL
    region: str | None = None  # region để fetch payment link (snapshot tại lúc add_jobs)
    retry_count: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "mail_mode": self.mail_mode,
            "reg_mode": self.reg_mode,
            "status": self.status,
            "error": self.error,
            "user_id": self.user_id,
            "has_password": bool(self.password),
            "has_secret": bool(self.secret),
            "has_first_code": bool(self.first_code),
            "has_session_path": bool(self.session_path),
            "has_session": self.session_data is not None,
            "payment_link": self.payment_link,
            "region": self.region,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "retry_count": self.retry_count,
            "duration": (
                (self.finished_at or time.time()) - self.started_at if self.started_at else None
            ),
            "log_count": len(self.log_lines),
        }

    def to_dict_secrets(self) -> dict[str, Any]:
        """Chỉ password + secret + first_code + session_path. Dùng cho /api/jobs/secrets."""
        return {
            "password": self.password,
            "secret": self.secret,
            "first_code": self.first_code,
            "session_path": self.session_path,
        }

    def to_dict_full(self) -> dict[str, Any]:
        """Detail endpoint — bao gồm cả secrets + session_data + log_lines."""
        d = self.to_dict()
        d.update(self.to_dict_secrets())
        d["log_lines"] = list(self.log_lines)
        d["session_data"] = self.session_data
        return d


class JobManager:
    """Quản lý jobs + concurrency thông qua worker pool pattern.

    Thay vì spawn task riêng cho mỗi job (race condition khi thay đổi
    max_concurrent giữa chừng), dùng N worker coroutine lấy job từ queue.
    Khi thay đổi max_concurrent → scale worker pool lên/xuống.
    """

    def __init__(self, *, max_concurrent: int = 1, job_repo: "JobRepository | None" = None, combo_repo: "ComboRepository | None" = None, session_repo: "SessionResultRepository | None" = None):
        self.jobs: dict[str, Job] = {}
        self.order: list[str] = []  # giữ thứ tự tạo
        self._max = max_concurrent
        self._headless = True
        self._debug = False
        self._job_timeout = _DEFAULT_JOB_TIMEOUT
        self._tasks: dict[str, asyncio.Task] = {}  # job_id → running task (for cancel)
        # Worker pool
        self._job_queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._worker_started = False
        # Shutdown guard: khi True, CancelledError handler không persist 'cancelled'
        # vì shutdown() đã ghi running→queued rồi.
        self._shutting_down: bool = False
        # Bounded retry khi persist "running" fail (transient SQLite error)
        self._persist_running_retries: dict[str, int] = {}
        self._persist_running_max_retries = 3
        # Detached delayed-requeue tasks (sống ngoài wait_for() để không bị
        # job_timeout cancel khi đang sleep) — track theo job_id để:
        #   1. shutdown cancel sạch
        #   2. retry_job/remove_job cancel ownership tránh duplicate enqueue
        self._delayed_requeue_tasks: dict[str, asyncio.Task] = {}
        # SQLite persistence (optional)
        self._job_repo: "JobRepository | None" = job_repo
        self._combo_repo: "ComboRepository | None" = combo_repo
        self._session_repo: "SessionResultRepository | None" = session_repo
        # Post-reg optional toggles
        self._post_reg_get_session: bool = False
        self._post_reg_get_link: bool = False
        # Region cho post-reg get-link (snapshot vào job.region khi add_jobs)
        self._post_reg_link_region: str = "VN"
        # Auto-retry: tự requeue jobs bị failed
        self._auto_retry: bool = False
        self._auto_retry_max: int = 3
        self._auto_retry_delay: float = 15.0
        # Stagger: tránh nhiều browser khởi tạo cùng 1 lúc → random 5-10s giữa các start
        self._stagger_lock = asyncio.Lock()
        self._last_start_ts: float = 0.0
        self._stagger_min_seconds = 5.0
        self._stagger_max_seconds = 10.0

    def _ensure_workers(self) -> None:
        """Đảm bảo đủ worker theo max_concurrent. Gọi mỗi khi thêm job hoặc đổi config."""
        if not self._worker_started:
            self._worker_started = True
        # Prune worker đã chết (bị cancel khi stop_all hoặc exit do exception).
        # Nếu không prune, len(self._workers) vẫn = _max → không spawn worker mới
        # → job enqueue sau stop_all sẽ nằm yên trong queue mãi.
        self._workers = [t for t in self._workers if not t.done()]
        # Scale lên nếu cần thêm worker
        while len(self._workers) < self._max:
            task = asyncio.create_task(self._worker_loop())
            self._workers.append(task)
        # Scale xuống: cancel worker thừa (chúng sẽ tự exit khi bị cancel ở chỗ queue.get)
        while len(self._workers) > self._max:
            task = self._workers.pop()
            task.cancel()

    async def _worker_loop(self) -> None:
        """Worker lấy job từ queue, chạy tuần tự từng cái một.

        Stagger: trước mỗi start, đợi tới ít nhất `_stagger_min_seconds` sau lần
        start gần nhất + random jitter — tránh spawn nhiều browser cùng tick.

        Job execution wrap trong inner task để `stop_all` cancel job mà không
        kill luôn worker. Nếu worker bị kill, các job add lại sau đó sẽ kẹt
        trong queue vì không ai pick lên.
        """
        try:
            while True:
                job_id = await self._job_queue.get()
                job = self.jobs.get(job_id)
                if job is None or job.status != "queued":
                    continue  # job đã bị remove/cancel trước khi tới lượt
                # Stagger start nếu max_concurrent > 1 (single mode không cần).
                # Reserve slot trong lock (fast), sleep ngoài lock + poll job
                # status mỗi 0.25s — bail nhanh nếu job bị cancel/remove giữa
                # chừng (đảm bảo stop_all + add lại không kẹt vì stagger debt).
                if self._max > 1:
                    async with self._stagger_lock:
                        now = time.monotonic()
                        wait_min = self._last_start_ts + self._stagger_min_seconds - now
                        if wait_min > 0:
                            jitter = random.uniform(
                                self._stagger_min_seconds, self._stagger_max_seconds,
                            )
                            wait = max(wait_min, jitter)
                            self._last_start_ts = now + wait
                        else:
                            wait = 0.0
                            self._last_start_ts = now
                    if wait > 0:
                        self._job_log(job, f"[stagger] đợi {wait:.1f}s trước khi start")
                        deadline = time.monotonic() + wait
                        while True:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                break
                            await asyncio.sleep(min(0.25, remaining))
                            cur = self.jobs.get(job_id)
                            if cur is None or cur.status != "queued":
                                break
                    cur = self.jobs.get(job_id)
                    if cur is None or cur.status != "queued":
                        continue
                inner = asyncio.create_task(self._run_job_with_timeout(job))
                self._tasks[job_id] = inner
                try:
                    await inner
                except asyncio.CancelledError:
                    # Nếu chính worker bị cancel → re-raise (dù inner cũng cancelled).
                    current = asyncio.current_task()
                    if current is not None and current.cancelled():
                        raise
                    # Inner job bị cancel (stop_all/remove_job) — worker đi tiếp.
                    if inner.cancelled():
                        continue
                    raise
                finally:
                    self._tasks.pop(job_id, None)
        except asyncio.CancelledError:
            pass

    @property
    def max_concurrent(self) -> int:
        return self._max

    def set_max_concurrent(self, n: int) -> None:
        if n < 1 or n > 10:
            raise ValueError("max_concurrent phải trong [1, 10]")
        self._max = n
        self._ensure_workers()

    @property
    def headless(self) -> bool:
        return self._headless

    def set_headless(self, value: bool) -> None:
        self._headless = bool(value)

    @property
    def debug(self) -> bool:
        return self._debug

    def set_debug(self, value: bool) -> None:
        self._debug = bool(value)

    @property
    def job_timeout(self) -> float:
        return self._job_timeout

    def set_job_timeout(self, seconds: float) -> None:
        if seconds < 30 or seconds > 600:
            raise ValueError("job_timeout phải trong [30, 600]")
        self._job_timeout = float(seconds)

    def _begin_job_proxy(self, job: "Job", log) -> str | None:
        """Resolve proxy cho 1 job từ proxy pool (round_robin/random).

        Lưu vào ``job._active_proxy`` (transient) để error handler mark-dead
        đúng proxy. Log proxy đang dùng (đã mask credential).
        """
        proxy = _resolve_job_proxy()
        job._active_proxy = proxy  # type: ignore[attr-defined]
        if proxy:
            log(f"[proxy] using {_mask_proxy(proxy)}")
        return proxy

    def _note_proxy_failure(self, job: "Job", exc_or_msg) -> None:
        """Mark proxy chết nếu lỗi là network/proxy → loại khỏi vòng xoay."""
        proxy = getattr(job, "_active_proxy", None)
        if proxy and _is_proxy_network_error(exc_or_msg):
            if get_proxy_pool().mark_dead(proxy):
                self._job_log(
                    job, f"[proxy] {_mask_proxy(proxy)} lỗi network — loại khỏi pool"
                )

    @property
    def post_reg_get_session(self) -> bool:
        return self._post_reg_get_session

    def set_post_reg_get_session(self, value: bool) -> None:
        self._post_reg_get_session = bool(value)

    @property
    def post_reg_get_link(self) -> bool:
        return self._post_reg_get_link

    def set_post_reg_get_link(self, value: bool) -> None:
        self._post_reg_get_link = bool(value)

    @property
    def post_reg_link_region(self) -> str:
        return self._post_reg_link_region

    def set_post_reg_link_region(self, value: str) -> None:
        from ..payment_link import REGION_BILLING
        v = (value or "").strip().upper()
        if v not in REGION_BILLING:
            raise ValueError(f"invalid region: {value}. Must be one of: {list(REGION_BILLING.keys())}")
        self._post_reg_link_region = v

    @property
    def auto_retry(self) -> bool:
        return self._auto_retry

    @property
    def auto_retry_max(self) -> int:
        return self._auto_retry_max

    @property
    def auto_retry_delay(self) -> float:
        return self._auto_retry_delay

    def set_auto_retry(self, enabled: bool, *, max_retries: int | None = None, delay: float | None = None) -> None:
        self._auto_retry = bool(enabled)
        if max_retries is not None:
            self._auto_retry_max = max(1, min(max_retries, 10))
        if delay is not None:
            self._auto_retry_delay = max(5.0, min(delay, 120.0))

    def apply_settings(self, settings: dict) -> None:
        """Hydrate fields from settings dict (startup boot). Only set if key present."""
        if "reg.headless" in settings:
            self._headless = bool(settings["reg.headless"])
        if "reg.job_timeout" in settings:
            val = float(settings["reg.job_timeout"])
            if 30 <= val <= 600:
                self._job_timeout = val
        if "reg.debug" in settings:
            self._debug = bool(settings["reg.debug"])
        if "reg.max_concurrent" in settings:
            val = int(settings["reg.max_concurrent"])
            if 1 <= val <= 10:
                self._max = val
        _hydrate_proxy_pool_from_settings(settings)
        if "reg.post_reg_get_session" in settings:
            self._post_reg_get_session = bool(settings["reg.post_reg_get_session"])
        if "reg.post_reg_get_link" in settings:
            self._post_reg_get_link = bool(settings["reg.post_reg_get_link"])
        if "reg.post_reg_link_region" in settings:
            v = str(settings["reg.post_reg_link_region"]).strip().upper()
            if v:
                self._post_reg_link_region = v
        if "reg.auto_retry" in settings:
            self._auto_retry = bool(settings["reg.auto_retry"])
        if "reg.auto_retry_max" in settings:
            val = int(settings["reg.auto_retry_max"])
            if 0 <= val <= 10:
                self._auto_retry_max = val
        if "reg.auto_retry_delay" in settings:
            val = float(settings["reg.auto_retry_delay"])
            if val >= 5.0:
                self._auto_retry_delay = val

    async def _maybe_auto_retry(self, job: Job) -> bool:
        """If auto-retry enabled and retries < max, requeue job after delay.

        Returns True if job was requeued.
        """
        if not self._auto_retry:
            return False
        if _is_fatal_error(job.error):
            self._job_log(job, f"[auto-retry] combo lỗi fatal — không retry")
            return False
        if job.retry_count >= self._auto_retry_max:
            self._job_log(job, f"[auto-retry] đã retry {job.retry_count}/{self._auto_retry_max} lần — dừng")
            return False
        job.retry_count += 1
        delay = self._auto_retry_delay * job.retry_count
        self._job_log(job, f"[auto-retry] sẽ retry {job.retry_count}/{self._auto_retry_max} sau {delay:.0f}s")
        if not await self._persist_status(job, "queued"):
            return False
        job.status = "queued"
        job.error = None
        job.started_at = None
        job.finished_at = None
        self._broadcast_job(job)
        self._schedule_delayed_requeue(job.id, delay)
        return True

    def _broadcast(self, event: dict[str, Any]) -> None:
        if _sse_mux is not None:
            _sse_mux.publish("reg", event)

    def _job_log(self, job: Job, msg: str, *, persisted: bool = False) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        job.log_lines.append(line)
        if len(job.log_lines) > 500:
            job.log_lines = job.log_lines[-500:]
        # Persist log line to SQLite BEFORE broadcasting.
        # Nếu persist fail: vẫn broadcast (UX > strict consistency cho log lines).
        # State transitions (success/error/cancelled) luôn persist atomic qua
        # _persist_status — nếu ĐÓNG persist fail → job không chuyển state.
        if not persisted and self._job_repo is not None and job.id in self.jobs:
            try:
                self._job_repo.append_log(job.id, line)
            except Exception as exc:
                _log.warning("SQLite append_log failed for job %s: %s", job.id, exc)
        self._broadcast({"type": "log", "job_id": job.id, "line": line})

    def _broadcast_job(self, job: Job) -> None:
        self._broadcast({"type": "job", "job": job.to_dict()})

    async def _persist_status(self, job: Job, status: str, **kwargs: object) -> bool:
        """Persist status transition to SQLite — non-blocking (runs in thread pool).

        Nếu `log_line` trong kwargs → insert log trong cùng transaction với status update.
        Returns True if persist succeeded (or no repo). Returns False if persist failed.
        On failure: caller must NOT update in-memory state or broadcast SSE.
        """
        if self._job_repo is None:
            return True
        try:
            repo = self._job_repo
            job_id = job.id
            await repo.engine.run_sync(lambda: repo.update_status(job_id, status, **kwargs))
            return True
        except Exception as exc:
            _log.warning("SQLite update_status failed for job %s → %s: %s", job.id, status, exc)
            return False

    def _schedule_delayed_requeue(
        self,
        job_id: str,
        delay: float,
        *,
        retry_2fa_only: bool = False,
    ) -> None:
        """Spawn detached task để requeue job sau `delay` giây.

        Tách ra ngoài cây gọi của `wait_for(job_timeout)` — nếu để
        `await asyncio.sleep + put_nowait` ngay trong _run_job(), timeout cancel
        có thể nuốt cả requeue khi `delay >= job_timeout` (worst case 30s).
        """
        async def _runner() -> None:
            try:
                await asyncio.sleep(delay)
                if self._shutting_down:
                    return
                job = self.jobs.get(job_id)
                if job is None:
                    return  # job đã bị remove
                # Chống duplicate enqueue: helper chạy song song với worker, có thể
                # 1 worker khác đã pickup job này (qua retry hoặc cycle khác). Chỉ
                # re-enqueue khi job vẫn ở trạng thái queued.
                if job.status != "queued":
                    return
                if retry_2fa_only:
                    job._retry_2fa_only = True  # type: ignore[attr-defined]
                self._job_queue.put_nowait(job_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover — defensive
                _log.error("delayed requeue for %s failed: %s", job_id, exc)

        if self._shutting_down:
            return
        # Cancel pending helper cùng job_id (nếu có) để tránh duplicate enqueue:
        # khi cycle exhausted lặp lại hoặc retry_job chạy trong khi sleep.
        existing = self._delayed_requeue_tasks.get(job_id)
        if existing is not None and not existing.done():
            existing.cancel()
        task = asyncio.create_task(_runner())
        self._delayed_requeue_tasks[job_id] = task
        task.add_done_callback(lambda _t, jid=job_id: self._delayed_requeue_tasks.pop(jid, None) if self._delayed_requeue_tasks.get(jid) is _t else None)

    def add_jobs(self, combos: list[str], *, default_password: str | None = None, mail_mode: str = "outlook", worker_config: dict[str, str] | None = None, reg_mode: str = "browser") -> list[Job]:
        """Thêm jobs từ list combo/email strings. Skip đã có trong list (dedup theo email)."""
        spec = get_spec(mail_mode)  # KeyError nếu mode lạ — server chặn trước
        existing_emails = {j.email.lower() for j in self.jobs.values() if j.status != "cancelled"}
        out: list[Job] = []
        for raw in combos:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                parsed = spec.parse_line(line)
            except (OutlookComboError, MailModeParseError) as exc:
                jid = uuid.uuid4().hex[:12]
                job = Job(
                    id=jid,
                    email="<invalid>",
                    combo=line[:80],
                    mail_mode=spec.id,
                    status="error",
                    error=f"parse fail: {exc}",
                    finished_at=time.time(),
                )
                # Persist to SQLite before adding to memory
                if self._job_repo is not None:
                    try:
                        self._job_repo.create({
                            "id": job.id,
                            "email": job.email,
                            "combo": job.combo,
                            "mail_mode": job.mail_mode,
                            "status": job.status,
                            "error": job.error,
                            "created_at": job.created_at,
                            "finished_at": job.finished_at,
                            "job_type": "signup",
                        })
                    except Exception as exc_db:
                        _log.warning("SQLite persist failed for job %s: %s", jid, exc_db)
                        continue  # skip — don't add to memory, don't broadcast
                self.jobs[jid] = job
                self.order.append(jid)
                self._broadcast_job(job)
                out.append(job)
                continue

            if parsed.email.lower() in existing_emails:
                continue  # dedup
            existing_emails.add(parsed.email.lower())

            jid = uuid.uuid4().hex[:12]
            job = Job(id=jid, email=parsed.email, combo=line, mail_mode=spec.id, reg_mode=reg_mode)
            # Snapshot region tại lúc add_jobs — đổi region sau đó không ảnh hưởng job cũ.
            # Chỉ lưu khi user bật fetch-link (region không liên quan nếu không fetch).
            if self._post_reg_get_link:
                job.region = self._post_reg_link_region
            job._default_password = default_password  # type: ignore[attr-defined]
            job._worker_config = worker_config  # type: ignore[attr-defined]
            # Pre-ensure Outlook combo row tồn tại trong DB — nhưng PRESERVE
            # refresh_token đã rotate. Chỉ explicit import-pool mới overwrite token.
            if self._combo_repo is not None and spec.id in _OUTLOOK_COMBO_MODES:
                try:
                    combo_obj = OutlookCombo.parse(parsed.raw)
                    self._combo_repo.ensure_exists({
                        "email": combo_obj.email,
                        "password": combo_obj.password,
                        "refresh_token": combo_obj.refresh_token,
                        "client_id": combo_obj.client_id,
                    })
                except Exception as exc_db:
                    _log.warning("combo ensure_exists failed for %s: %s — skipping job", parsed.email, exc_db)
                    continue  # Không enqueue khi row chưa tồn tại — token rotation sẽ fail
            # Persist to SQLite before adding to memory
            if self._job_repo is not None:
                try:
                    self._job_repo.create({
                        "id": job.id,
                        "email": job.email,
                        "combo": job.combo,
                        "mail_mode": job.mail_mode,
                        "status": job.status,
                        "region": job.region,
                        "created_at": job.created_at,
                        "job_type": "signup",
                    })
                except Exception as exc_db:
                    _log.warning("SQLite persist failed for job %s: %s", jid, exc_db)
                    continue  # skip — don't add to memory, don't broadcast
            self.jobs[jid] = job
            self.order.append(jid)
            self._broadcast_job(job)
            out.append(job)
        # Enqueue jobs → workers sẽ pick lên theo thứ tự
        self._ensure_workers()
        for j in out:
            if j.status == "queued":
                self._job_queue.put_nowait(j.id)
        return out

    def remove_job(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None:
            return False
        # Persist deletion to SQLite FIRST (prevent resurrection on restart)
        if self._job_repo is not None:
            try:
                self._job_repo.delete(job_id)
            except Exception as exc:
                _log.warning("SQLite delete failed for job %s: %s — abort remove", job_id, exc)
                return False
        # SQLite succeeded (or no repo) → safe to mutate memory state
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        self.jobs.pop(job_id, None)
        if job_id in self.order:
            self.order.remove(job_id)
        self._tasks.pop(job_id, None)
        self._broadcast({"type": "remove", "job_id": job_id})
        return True

    async def stop_all(self) -> int:
        """Cancel tất cả jobs đang running/queued. Return số job đã cancel."""
        # Drain queue trước — tránh worker pick job mới
        while not self._job_queue.empty():
            try:
                self._job_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        count = 0
        for job_id, job in list(self.jobs.items()):
            if job.status in ("running", "queued"):
                # Persist to SQLite first — skip job if persist fails
                if not await self._persist_status(job, "cancelled"):
                    continue
                task = self._tasks.get(job_id)
                if task and not task.done():
                    task.cancel()
                job.status = "cancelled"
                job.finished_at = time.time()
                self._broadcast_job(job)
                count += 1
        # Reset stagger debt — batch jobs mới sau stop_all không phải đợi
        # khoảng cách stagger tính từ batch cũ.
        self._last_start_ts = 0.0
        return count

    def clear_finished(self) -> int:
        """Xoá tất cả jobs đã xong (success/error) khỏi memory và SQLite.

        Cascade xoá job_logs trong SQLite tự động qua FK ON DELETE CASCADE.
        Nếu SQLite delete fail: abort — KHÔNG clear memory (giữ nhất quán).
        """
        # Persist deletion to SQLite first (fail → abort)
        if self._job_repo is not None:
            try:
                self._job_repo.delete_finished(job_type="signup")
            except Exception as exc:
                _log.warning("SQLite delete_finished failed: %s — memory not cleared", exc)
                return -1

        # Clear from memory (only if SQLite succeeded or no repo)
        removed = 0
        for jid in list(self.order):
            job = self.jobs.get(jid)
            if job and job.status in ("success", "error"):
                self.jobs.pop(jid, None)
                self.order.remove(jid)
                self._tasks.pop(jid, None)
                removed += 1
        if removed:
            self._broadcast({"type": "clear_finished", "removed": removed})
        return removed

    async def clear_all(self) -> int:
        """Xoá TẤT CẢ jobs (mọi status) khỏi memory và SQLite.

        Cancel running tasks trước khi xoá. Drain queue.
        """
        # Drain queue — tránh worker pick job mới
        while not self._job_queue.empty():
            try:
                self._job_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        # Cancel tất cả running tasks
        for job_id, task in list(self._tasks.items()):
            if task and not task.done():
                task.cancel()
        # Cancel delayed-requeue tasks
        for t in list(self._delayed_requeue_tasks.values()):
            if not t.done():
                t.cancel()
        self._delayed_requeue_tasks.clear()

        # Persist deletion to SQLite first
        if self._job_repo is not None:
            try:
                self._job_repo.delete_all(job_type="signup")
            except Exception as exc:
                _log.warning("SQLite delete_all failed: %s — memory not cleared", exc)
                return -1

        # Clear from memory
        removed = len(self.jobs)
        self.jobs.clear()
        self.order.clear()
        self._tasks.clear()
        self._last_start_ts = 0.0

        if removed:
            self._broadcast({"type": "clear_all", "removed": removed})
        return removed

    async def retry_failed(self) -> int:
        """Retry tất cả jobs có status error hoặc cancelled.

        Return số job đã retry thành công.
        """
        retried = 0
        targets = [
            jid for jid, job in self.jobs.items()
            if job.status in ("error", "cancelled")
        ]
        for jid in targets:
            ok = await self.retry_job(jid)
            if ok:
                retried += 1
        return retried

    def shutdown(self) -> None:
        """Mark all running jobs as queued in SQLite for recovery on next startup.

        Gọi khi nhận SIGINT/SIGTERM. Đảm bảo jobs đang chạy sẽ được recover
        ở lần khởi động tiếp theo thông qua recover_interrupted().
        Cancel worker tasks để event loop có thể thoát sạch.

        Sequence quan trọng:
        1. Set _shutting_down = True → CancelledError handler skip persist 'cancelled'
        2. recover_interrupted() → DB: running → queued
        3. Cancel workers → CancelledError fires nhưng không ghi đè DB
        """
        self._shutting_down = True
        if self._job_repo is not None:
            try:
                self._job_repo.recover_interrupted()
                _log.info("shutdown: marked running jobs as queued for recovery")
            except Exception as exc:
                _log.error("shutdown: failed to mark running jobs as queued: %s", exc)
        # Cancel pending delayed-requeue tasks (sleep 30s rồi put_nowait) — chúng
        # detached khỏi worker tasks nên cần cancel riêng để event loop thoát sạch.
        for t in list(self._delayed_requeue_tasks.values()):
            if not t.done():
                t.cancel()
        self._delayed_requeue_tasks.clear()
        # Cancel all worker tasks — cho phép event loop thoát sạch
        for w in self._workers:
            if not w.done():
                w.cancel()
        self._workers.clear()

    async def retry_job(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None:
            return False
        # Cancel task hiện tại nếu running
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        # Cancel pending delayed-requeue helper (nếu đang sleep) để retry_job
        # không bị duplicate enqueue khi helper wake.
        pending = self._delayed_requeue_tasks.pop(job_id, None)
        if pending is not None and not pending.done():
            pending.cancel()

        # Nếu signup đã thành công (có session_path) nhưng 2FA fail,
        # retry chỉ Phase 2 — không signup lại (tránh trigger login flow + duplicate).
        retry_2fa_only = bool(job.session_path and not job.secret)

        # Persist status reset to SQLite first
        # retry-2fa-only giữ session_path trong DB; full retry clear hết
        persist_kwargs: dict[str, object] = {}
        if retry_2fa_only:
            persist_kwargs["session_path"] = job.session_path
            persist_kwargs["user_id"] = job.user_id
        if not await self._persist_status(job, "queued", **persist_kwargs):
            return False  # SQLite fail → don't mutate memory

        # Reset state — giữ password đã gen (không gen lại khi retry)
        job.status = "queued"
        job.error = None
        job.secret = None
        job.first_code = None
        # KHÔNG reset job.password — dùng lại password đã gen ban đầu
        if not retry_2fa_only:
            job.user_id = None
            job.session_path = None
        job.started_at = None
        job.finished_at = None
        retry_label = "retry-2fa" if retry_2fa_only else "retry"
        retry_line = f"[{datetime.now():%H:%M:%S}] -- {retry_label} --"
        # Persist retry marker log to SQLite (recovery cần thấy retry đã xảy ra)
        if self._job_repo is not None:
            try:
                self._job_repo.append_log(job.id, retry_line)
            except Exception as exc:
                _log.warning("SQLite append_log (retry marker) failed for job %s: %s", job.id, exc)
        job.log_lines.append(retry_line)
        self._broadcast_job(job)
        self._broadcast({"type": "log", "job_id": job_id, "line": retry_line})
        # Mark để worker biết cần chạy 2fa-only
        job._retry_2fa_only = retry_2fa_only  # type: ignore[attr-defined]
        self._ensure_workers()
        self._job_queue.put_nowait(job_id)
        return True

    async def rerun_link_for_job(self, job_id: str, *, region: str | None = None) -> bool:
        """Re-fetch payment link cho job đã có session (success hoặc 2fa-failed-after-signup).

        Read access_token từ session.json (file đã save). Không re-login, không
        cancel job khác. Region = arg ưu tiên > job.region (snapshot lúc add_jobs)
        > config hiện tại.

        Trả True nếu đã chạy (kết quả thành công hay không thì check job.payment_link
        + broadcast SSE). False nếu không thể bắt đầu (job không có session_path,
        đang chạy, không có access_token).
        """
        job = self.jobs.get(job_id)
        if job is None:
            return False
        if job.status == "running":
            return False  # đang chạy signup/2fa, không chen ngang
        if not job.session_path or not Path(job.session_path).exists():
            self._job_log(job, "[rerun-link] không có session_path, bỏ qua")
            self._broadcast_job(job)
            return False

        # Resolve region
        from ..payment_link import REGION_BILLING
        if region:
            region_resolved = region.strip().upper()
            if region_resolved not in REGION_BILLING:
                self._job_log(job, f"[rerun-link] region không hợp lệ: {region}")
                self._broadcast_job(job)
                return False
        else:
            region_resolved = job.region or self._post_reg_link_region

        # Đọc access_token từ session file
        try:
            sdata = json.loads(Path(job.session_path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            self._job_log(job, f"[rerun-link] session file đọc fail: {exc}")
            self._broadcast_job(job)
            return False

        access_token = sdata.get("access_token")
        if not access_token:
            self._job_log(job, "[rerun-link] session thiếu access_token")
            self._broadcast_job(job)
            return False

        self._job_log(job, f"[rerun-link] region={region_resolved}, fetching...")
        from ..payment_link import get_checkout_url, SessionExpiredError, PaymentLinkError, CloudflareBlockedError

        url: str | None = None
        last_err: Exception | None = None
        # 2 attempts × 30s timeout × 1.5s sleep
        for attempt in range(1, 3):
            try:
                url = await asyncio.wait_for(
                    get_checkout_url(
                        access_token, proxy=_resolve_job_proxy(), region=region_resolved,
                    ),
                    timeout=30.0,
                )
                break
            except asyncio.TimeoutError:
                last_err = TimeoutError("timeout 30s")
                self._job_log(job, f"[rerun-link] attempt {attempt}/2 timeout 30s")
            except SessionExpiredError as exc:
                last_err = exc
                self._job_log(job, f"[rerun-link] session expired — no retry: {exc}")
                break
            except CloudflareBlockedError as exc:
                last_err = exc
                self._job_log(job, f"[rerun-link] cloudflare block — no retry: {exc}")
                break
            except PaymentLinkError as exc:
                last_err = exc
                self._job_log(job, f"[rerun-link] attempt {attempt}/2 failed: {exc}")
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                self._job_log(job, f"[rerun-link] attempt {attempt}/2 unexpected: {exc}")
            if attempt < 2:
                await asyncio.sleep(1.5)

        if url:
            job.payment_link = url
            job.region = region_resolved
            self._job_log(job, f"[rerun-link] success: {url}")
            # Persist payment_link + region (status không đổi)
            if self._job_repo is not None:
                try:
                    repo = self._job_repo
                    jid = job.id
                    new_link = url
                    new_region = region_resolved

                    def _persist():
                        with repo.engine.transaction() as conn:
                            conn.execute(
                                "UPDATE jobs SET payment_link = ?, region = ? WHERE id = ?",
                                (new_link, new_region, jid),
                            )

                    await repo.engine.run_sync(_persist)
                except Exception as exc:
                    _log.warning("rerun-link: persist payment_link failed for %s: %s", job_id, exc)
            self._broadcast_job(job)
            try:
                settings = load_settings()
                _append_link_log(
                    session_dir=runtime_session_dir(settings),
                    payment_link=url,
                )
            except Exception:
                pass
            return True

        msg = f"[rerun-link] failed: {last_err}" if last_err else "[rerun-link] failed: unknown"
        self._job_log(job, msg)
        self._broadcast_job(job)
        return True

    def list_jobs(self) -> list[dict[str, Any]]:
        return [self.jobs[jid].to_dict() for jid in self.order if jid in self.jobs]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        return job.to_dict_full() if job else None

    def get_log(self, job_id: str) -> list[str]:
        job = self.jobs.get(job_id)
        return list(job.log_lines) if job else []

    def get_secrets_map(self) -> dict[str, dict[str, Any]]:
        """Trả map job_id → {password, secret, first_code, session_path} cho mọi job.

        Dùng cho UI render Success pane một lần thay vì fetch detail từng job.
        Endpoint gọi method này phải có auth gate.
        """
        return {
            jid: self.jobs[jid].to_dict_secrets()
            for jid in self.order
            if jid in self.jobs
        }

    def _spawn(self, job: Job) -> None:
        """Legacy: chỉ dùng cho internal retry trực tiếp (không qua queue)."""
        task = asyncio.create_task(self._run_job_with_timeout(job))
        self._tasks[job.id] = task

    async def _run_job_with_timeout(self, job: Job) -> None:
        """Wrap _run_job với timeout. Kill nếu vượt job_timeout."""
        # Track task để có thể cancel từ bên ngoài
        self._tasks[job.id] = asyncio.current_task()  # type: ignore[arg-type]
        try:
            # Kiểm tra nếu là retry-2fa-only
            retry_2fa_only = getattr(job, '_retry_2fa_only', False)
            if hasattr(job, '_retry_2fa_only'):
                del job._retry_2fa_only  # type: ignore[attr-defined]

            # Debug mode + headed → không timeout (chờ user cancel)
            timeout = None if (self._debug and not self._headless) else self._job_timeout

            if retry_2fa_only:
                await asyncio.wait_for(self._run_2fa_only_inner(job), timeout=timeout)
            else:
                await asyncio.wait_for(self._run_job(job), timeout=timeout)
        except asyncio.TimeoutError:
            error_msg = f"timeout {self._job_timeout:.0f}s exceeded — killed"
            fatal_line = f"[fatal] job timeout {self._job_timeout:.0f}s — killed"
            if await self._persist_status(job, "error", error=error_msg, log_line=fatal_line):
                job.status = "error"
                job.error = error_msg
                job.finished_at = time.time()
                self._job_log(job, fatal_line, persisted=True)
                self._broadcast_job(job)
        except asyncio.CancelledError:
            # Shutdown path: recover_interrupted() đã ghi running→queued,
            # KHÔNG ghi đè cancelled vào DB.
            if not self._shutting_down and job.id in self.jobs:
                if await self._persist_status(job, "cancelled"):
                    job.status = "cancelled"
                    job.finished_at = time.time()
                    self._broadcast_job(job)
            raise
        finally:
            self._tasks.pop(job.id, None)
            if job.status == "error" and job.id in self.jobs:
                await self._maybe_auto_retry(job)

    def _spawn_2fa_only(self, job: Job) -> None:
        """Legacy — không dùng trực tiếp nữa, retry qua queue."""
        job._retry_2fa_only = True  # type: ignore[attr-defined]
        self._ensure_workers()
        self._job_queue.put_nowait(job.id)

    async def _run_2fa_only_inner(self, job: Job) -> None:
        """Chạy Phase 2 (enable 2FA) khi signup đã có session_path."""
        try:
            if job.id not in self.jobs:
                return
            if not await self._persist_status(job, "running"):
                # Transient SQLite fail — requeue với bounded retry
                retries = self._persist_running_retries.get(job.id, 0) + 1
                if retries <= self._persist_running_max_retries:
                    self._persist_running_retries[job.id] = retries
                    _log.warning(
                        "persist running failed for 2fa-only job %s — requeue (attempt %d/%d)",
                        job.id, retries, self._persist_running_max_retries,
                    )
                    await asyncio.sleep(min(retries * 2.0, 6.0))
                    job._retry_2fa_only = True  # type: ignore[attr-defined]
                    self._job_queue.put_nowait(job.id)
                else:
                    # Exhausted retries — re-enqueue so job isn't permanently stuck.
                    # Job stays queued in both memory and DB (Req 10.3/10.4 respected).
                    # Reset counter so next pickup starts fresh — gives SQLite time to recover.
                    _log.critical(
                        "persist running failed for 2fa-only job %s after %d retries — "
                        "re-enqueueing with extended delay (SQLite may recover)",
                        job.id, retries,
                    )
                    self._persist_running_retries.pop(job.id, None)
                    # Detached task — không bị wait_for(job_timeout) cancel khi sleep.
                    self._schedule_delayed_requeue(job.id, 30.0, retry_2fa_only=True)
                return
            self._persist_running_retries.pop(job.id, None)
            job.status = "running"
            job.started_at = time.time()
            self._broadcast_job(job)

            def log(msg: str) -> None:
                self._job_log(job, msg)

            # Đọc access_token từ session.json đã save
            if not job.session_path or not Path(job.session_path).exists():
                error_msg = "session file mất, không retry 2FA được"
                if await self._persist_status(job, "error", error=error_msg):
                    job.status = "error"
                    job.error = error_msg
                    job.finished_at = time.time()
                    self._broadcast_job(job)
                return

            try:
                sdata = json.loads(Path(job.session_path).read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                error_msg = f"session file corrupt: {exc}"
                if await self._persist_status(job, "error", error=error_msg):
                    job.status = "error"
                    job.error = error_msg
                    job.finished_at = time.time()
                    self._broadcast_job(job)
                return

            access_token = sdata.get("access_token")
            if not access_token:
                error_msg = "session file thiếu access_token"
                if await self._persist_status(job, "error", error=error_msg):
                    job.status = "error"
                    job.error = error_msg
                    job.finished_at = time.time()
                    self._broadcast_job(job)
                return

            log("[2fa] retry-only: dùng session đã có")
            # Đọc pending enrollment từ DB — nếu enroll trước đó đã OK nhưng
            # activate fail → tái dùng secret/factor_id thay vì enroll lại
            # (server đã có active factor → conflict loop).
            pending: dict | None = None
            if self._session_repo is not None:
                try:
                    pending = await asyncio.to_thread(
                        self._session_repo.get_mfa_pending, job.email,
                    )
                    if pending:
                        log(
                            f"[2fa] dùng pending enrollment factor_id="
                            f"{(pending.get('factor_id') or '')[:20]}"
                        )
                except Exception as exc_pending:
                    _log.warning(
                        "load mfa_pending for %s failed: %s — sẽ enroll mới",
                        job.email, exc_pending,
                    )

            on_enroll_cb = _make_on_enroll_callback(
                self._session_repo, email=job.email, log=log,
            )
            try:
                mfa_result = await enable_2fa(
                    access_token=access_token,
                    cookies=sdata.get("cookies"),
                    proxy=_resolve_job_proxy(),
                    pending_enrollment=pending,
                    on_enroll=on_enroll_cb,
                    log=log,
                )
            except MfaError as exc:
                # Persist partial_state nếu enroll OK nhưng activate fail.
                # Lần retry sau sẽ tái dùng pending để bypass enroll.
                _persist_partial_state_sync(
                    self._session_repo,
                    email=job.email,
                    partial_state=exc.partial_state,
                    log=log,
                )
                error_msg = f"2fa: {exc}"
                if await self._persist_status(job, "error", error=error_msg):
                    job.status = "error"
                    job.error = error_msg
                    job.finished_at = time.time()
                    self._broadcast_job(job)
                return

            session_path = Path(job.session_path)
            two_fa_path = session_path.with_suffix(".2fa.json")
            two_fa_path.write_text(
                json.dumps({
                    "email": job.email,
                    "user_id": job.user_id,
                    "two_factor": mfa_result,
                }, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            # Merge two_factor vào file session chính (1 file/account, dễ downstream parse)
            try:
                existing = json.loads(session_path.read_text(encoding="utf-8"))
                existing["two_factor"] = mfa_result
                session_path.write_text(
                    json.dumps(existing, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception as exc_merge:
                _log.warning("merge two_factor into session JSON failed for %s: %s", job.email, exc_merge)

            # Persist 2FA vào session_results SQLite (fail-fast)
            if self._session_repo is not None:
                try:
                    self._session_repo.update_2fa(job.email, mfa_result)
                except Exception as exc_db:
                    error_msg = f"2fa persist failed: {exc_db}"
                    if await self._persist_status(job, "error", error=error_msg):
                        job.status = "error"
                        job.error = error_msg
                        job.finished_at = time.time()
                        self._broadcast_job(job)
                    return
                # 2FA đã persist OK → clear mfa_pending (idempotent, best-effort).
                try:
                    await asyncio.to_thread(
                        self._session_repo.clear_mfa_pending, job.email,
                    )
                except Exception as exc_clear:
                    _log.warning(
                        "clear_mfa_pending for %s failed (non-fatal): %s",
                        job.email, exc_clear,
                    )

            job.secret = mfa_result.get("secret")
            job.first_code = mfa_result.get("first_code")

            # Append to aggregated accounts.txt (best-effort, không fail job nếu IO lỗi)
            if job.secret and job.password:
                try:
                    settings = load_settings()
                    _append_account_log(
                        session_dir=runtime_session_dir(settings),
                        email=job.email,
                        password=job.password,
                        totp_secret=job.secret,
                    )
                except Exception as exc_log:
                    _log.warning("append accounts.txt failed for %s: %s", job.email, exc_log)

            if await self._persist_status(job, "success", secret=job.secret, first_code=job.first_code):
                job.status = "success"
                job.finished_at = time.time()
                self._broadcast_job(job)

        except asyncio.CancelledError:
            if not self._shutting_down and job.id in self.jobs:
                if await self._persist_status(job, "cancelled"):
                    job.status = "cancelled"
                    job.finished_at = time.time()
                    self._broadcast_job(job)
            raise
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            fatal_line = f"[fatal] {error_msg}"
            if await self._persist_status(job, "error", error=error_msg, log_line=fatal_line):
                job.status = "error"
                job.error = error_msg
                job.finished_at = time.time()
                self._job_log(job, fatal_line, persisted=True)
                self._broadcast_job(job)

    async def _persist_signup_failure(self, job: Job, error_msg: str) -> None:
        """Mark job error sau signup fail. Fail-safe 3 tầng:

        Tầng 1 (preferred): atomic combo.mark_failure + job.update_status trong 1 tx.
        Tầng 2 (fallback):  _persist_status(error) đơn lẻ, kèm log warning combo.
        Tầng 3 (last resort): UPDATE memory + broadcast SSE để UI không kẹt "running".
                              DB sẽ tự recover ở lần restart kế (running → queued).

        Tránh trường hợp memory state = "running" mãi khi DB transient down.
        """
        # Tầng 1: atomic
        atomic_ok = False
        if self._job_repo is not None:
            try:
                engine = self._job_repo.engine
                combo_repo = self._combo_repo
                mail_mode = job.mail_mode
                email = job.email
                jid = job.id

                def _persist_failure_atomic() -> None:
                    with engine.transaction():
                        if combo_repo is not None and mail_mode in _OUTLOOK_COMBO_MODES:
                            combo_repo.mark_failure(email, error_msg)
                        self._job_repo.update_status(jid, "error", error=error_msg)

                await engine.run_sync(_persist_failure_atomic)
                atomic_ok = True
            except Exception as exc_db:
                _log.warning(
                    "atomic signup failure persist failed for job %s (%s): %s — fallback split persist",
                    job.id, job.email, exc_db,
                )

        if atomic_ok:
            job.status = "error"
            job.error = error_msg
            job.finished_at = time.time()
            self._broadcast_job(job)
            return

        # Tầng 2: split persist (combo best-effort, job status fail-safe)
        if self._combo_repo is not None and job.mail_mode in _OUTLOOK_COMBO_MODES:
            try:
                self._combo_repo.mark_failure(job.email, error_msg)
            except Exception as exc_db:
                _log.warning(
                    "combo mark_failure (fallback) failed for %s: %s",
                    job.email, exc_db,
                )

        if await self._persist_status(job, "error", error=error_msg):
            job.status = "error"
            job.error = error_msg
            job.finished_at = time.time()
            self._broadcast_job(job)
            return

        # Tầng 3: DB hoàn toàn fail → mutate memory + broadcast để UI không kẹt.
        # Recovery (running→queued) ở lần restart sẽ đưa job về queued, user retry.
        _log.critical(
            "DB persist totally failed for job %s (%s) — marking memory error to unstick UI. "
            "Restart sẽ recover qua running→queued: %s",
            job.id, job.email, error_msg,
        )
        job.status = "error"
        job.error = f"{error_msg} [DB persist failed — sẽ recover khi restart]"
        job.finished_at = time.time()
        self._broadcast_job(job)

    async def _run_job(self, job: Job) -> None:
        try:
            if job.id not in self.jobs:
                return  # đã bị remove trước khi tới lượt
            if not await self._persist_status(job, "running"):
                # Transient SQLite fail — requeue với bounded retry
                retries = self._persist_running_retries.get(job.id, 0) + 1
                if retries <= self._persist_running_max_retries:
                    self._persist_running_retries[job.id] = retries
                    _log.warning(
                        "persist running failed for job %s — requeue (attempt %d/%d)",
                        job.id, retries, self._persist_running_max_retries,
                    )
                    await asyncio.sleep(min(retries * 2.0, 6.0))  # backoff
                    self._job_queue.put_nowait(job.id)
                else:
                    # Exhausted retries — do NOT mutate memory or broadcast (Req 10.3/10.4).
                    # Re-enqueue so job isn't permanently stuck in dead state.
                    # Job stays queued in both memory and DB (Req 10.3/10.4 respected).
                    # Reset counter — gives SQLite time to recover between cycles.
                    _log.critical(
                        "persist running failed for job %s after %d retries — "
                        "re-enqueueing with extended delay (SQLite may recover)",
                        job.id, retries,
                    )
                    self._persist_running_retries.pop(job.id, None)
                    # Detached task — không bị wait_for(job_timeout) cancel khi sleep.
                    self._schedule_delayed_requeue(job.id, 30.0)
                return
            # Persist succeeded — clear retry counter
            self._persist_running_retries.pop(job.id, None)
            job.status = "running"
            job.started_at = time.time()
            self._broadcast_job(job)

            def log(msg: str) -> None:
                self._job_log(job, msg)

            log(f"[mode] {job.mail_mode}")
            job_proxy = self._begin_job_proxy(job, log)

            # Build SignupRequest qua registry spec
            spec = get_spec(job.mail_mode)
            parsed = spec.parse_line(job.combo)
            request = spec.build_request(
                parsed,
                worker_config=getattr(job, '_worker_config', None),
                password=job.password or getattr(job, '_default_password', None),
                headless=self._headless,
                keep_browser_open=self._debug and not self._headless,
                proxy=job_proxy,
                reg_mode=job.reg_mode,
            )
            result: SignupResult = await run_signup(
                request, log=log, combo_repo=self._combo_repo,
            )

            # Update job email nếu đã resolve từ API (URL-only gmail_advanced)
            if result.email and result.email != job.email:
                old_email = job.email
                # Persist resolved email vào SQLite TRƯỚC khi mutate memory/broadcast.
                # Nếu persist fail → giữ email cũ (nhất quán DB↔memory).
                if self._job_repo is not None:
                    try:
                        self._job_repo.update_email(job.id, result.email)
                    except Exception as exc:
                        _log.error(
                            "Failed to persist resolved email %s→%s for job %s: %s — keeping old email",
                            old_email, result.email, job.id, exc,
                        )
                        # Don't mutate memory, don't broadcast — email stays as old
                        result.email = old_email  # ensure downstream uses consistent email
                    else:
                        job.email = result.email
                        self._broadcast_job(job)
                else:
                    job.email = result.email
                    self._broadcast_job(job)

            if not result.success:
                error_msg = result.error or "signup failed"
                self._note_proxy_failure(job, error_msg)
                await self._persist_signup_failure(job, error_msg)
                return

            # Lưu session JSON
            settings = load_settings()
            session_path = (
                runtime_session_dir(settings)
                / f"signup-{datetime.now():%Y%m%d-%H%M%S}-{job.email.replace('@', '_at_')}.json"
            )
            session_path.write_text(
                json.dumps(result.model_dump(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            job.session_path = str(session_path)
            job.user_id = result.user_id
            job.password = result.password

            # Persist session result + mark combo success vào SQLite (atomic)
            # Fail-safe 3 tầng để job không kẹt "running" khi DB transient down.
            # Session file đã save (line trên) — DB fail không mất session data.
            if self._session_repo is not None:
                engine = self._session_repo.engine
                session_repo = self._session_repo
                combo_repo = self._combo_repo
                mail_mode = job.mail_mode
                email = job.email
                session_data = {
                    "email": result.email,
                    "password": result.password,
                    "name": result.name,
                    "age": result.age,
                    "user_id": result.user_id,
                    "account_id": result.account_id,
                    "session_token": result.session_token,
                    "access_token": result.access_token,
                    "cookies": result.cookies,
                    "phase1_seconds": result.phase1_seconds,
                    "phase2_seconds": result.phase2_seconds,
                    "otp_seconds": result.otp_seconds,
                }

                def _persist_success():
                    with engine.transaction():
                        session_repo.create(session_data)
                        if combo_repo is not None and mail_mode in _OUTLOOK_COMBO_MODES:
                            combo_repo.mark_success(email)

                try:
                    await engine.run_sync(_persist_success)
                except Exception as exc_db:
                    # Session file đã save xuống disk → vẫn có thể recover.
                    # Mark job error trong DB nếu được; nếu không, mark memory error
                    # để UI không kẹt running. Restart sẽ đưa lại queued cho retry.
                    error_msg = f"session persist failed: {exc_db}"
                    _log.error(
                        "session_results persist failed for job %s (%s): %s",
                        job.id, job.email, exc_db,
                    )
                    if await self._persist_status(job, "error", error=error_msg):
                        job.status = "error"
                        job.error = error_msg
                        job.finished_at = time.time()
                        self._broadcast_job(job)
                    else:
                        _log.critical(
                            "DB totally failed for job %s — memory error fallback to unstick UI",
                            job.id,
                        )
                        job.status = "error"
                        job.error = f"{error_msg} [DB persist failed — sẽ recover khi restart]"
                        job.finished_at = time.time()
                        self._broadcast_job(job)
                    return
            elif self._combo_repo is not None and job.mail_mode in _OUTLOOK_COMBO_MODES:
                try:
                    self._combo_repo.mark_success(job.email)
                except Exception as exc_db:
                    _log.warning("combo mark_success failed for %s: %s", job.email, exc_db)

            # Phase 2: enable 2FA
            log("[2fa] enabling…")
            if not result.access_token:
                error_msg = "missing access_token, không thể enable 2FA"
                if await self._persist_status(job, "error", error=error_msg):
                    job.status = "error"
                    job.error = error_msg
                    job.finished_at = time.time()
                    self._broadcast_job(job)
                return

            try:
                mfa_result = await enable_2fa(
                    access_token=result.access_token,
                    cookies=result.cookies,
                    proxy=getattr(job, "_active_proxy", None),
                    on_enroll=_make_on_enroll_callback(
                        self._session_repo, email=job.email, log=log,
                    ),
                    log=log,
                )
            except MfaError as exc:
                _persist_partial_state_sync(
                    self._session_repo,
                    email=job.email,
                    partial_state=exc.partial_state,
                    log=log,
                )
                error_msg = f"2fa: {exc}"
                if await self._persist_status(job, "error", error=error_msg):
                    job.status = "error"
                    job.error = error_msg
                    job.finished_at = time.time()
                    self._broadcast_job(job)
                return

            # Lưu .2fa.json kèm
            two_fa_path = session_path.with_suffix(".2fa.json")
            two_fa_path.write_text(
                json.dumps({
                    "email": job.email,
                    "user_id": job.user_id,
                    "two_factor": mfa_result,
                }, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            # Merge two_factor vào file session chính (1 file/account, dễ downstream parse)
            try:
                existing = json.loads(session_path.read_text(encoding="utf-8"))
                existing["two_factor"] = mfa_result
                session_path.write_text(
                    json.dumps(existing, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception as exc_merge:
                _log.warning("merge two_factor into session JSON failed for %s: %s", job.email, exc_merge)

            # Persist 2FA vào session_results SQLite (fail-fast: 2FA persist failure → job error)
            if self._session_repo is not None:
                try:
                    self._session_repo.update_2fa(job.email, mfa_result)
                except Exception as exc_db:
                    error_msg = f"2fa persist failed: {exc_db}"
                    if await self._persist_status(job, "error", error=error_msg):
                        job.status = "error"
                        job.error = error_msg
                        job.finished_at = time.time()
                        self._broadcast_job(job)
                    return
                # 2FA đã persist OK → clear mfa_pending (idempotent, best-effort).
                try:
                    await asyncio.to_thread(
                        self._session_repo.clear_mfa_pending, job.email,
                    )
                except Exception as exc_clear:
                    _log.warning(
                        "clear_mfa_pending for %s failed (non-fatal): %s",
                        job.email, exc_clear,
                    )

            job.secret = mfa_result.get("secret")
            job.first_code = mfa_result.get("first_code")

            # Append to aggregated accounts.txt
            if job.secret and job.password:
                try:
                    settings = load_settings()
                    _append_account_log(
                        session_dir=runtime_session_dir(settings),
                        email=job.email,
                        password=job.password,
                        totp_secret=job.secret,
                    )
                except Exception as exc_log:
                    _log.warning("append accounts.txt failed for %s: %s", job.email, exc_log)

            # Post-reg optional steps
            if self._post_reg_get_session or self._post_reg_get_link:
                await self._post_reg_steps(job, result)

            if await self._persist_status(
                job, "success",
                password=job.password, secret=job.secret,
                first_code=job.first_code, user_id=job.user_id,
                session_path=job.session_path, payment_link=job.payment_link,
                region=job.region,
                session_data=json.dumps(job.session_data, ensure_ascii=False) if job.session_data else None,
            ):
                job.status = "success"
                job.finished_at = time.time()
                self._broadcast_job(job)

        except asyncio.CancelledError:
            if not self._shutting_down and job.id in self.jobs:
                if await self._persist_status(job, "cancelled"):
                    job.status = "cancelled"
                    job.finished_at = time.time()
                    self._broadcast_job(job)
            raise
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            fatal_line = f"[fatal] {error_msg}"
            if await self._persist_status(job, "error", error=error_msg, log_line=fatal_line):
                job.status = "error"
                job.error = error_msg
                job.finished_at = time.time()
                self._job_log(job, fatal_line, persisted=True)
                self._broadcast_job(job)

    async def _post_reg_steps(self, job: Job, result: SignupResult) -> None:
        """Execute enabled post-reg toggles — song song khi cả 2 bật.

        Mỗi step độc lập: lỗi 1 step không ảnh hưởng step khác và không làm fail
        overall job (reg+2FA đã thành công).
        """
        coros = []
        if self._post_reg_get_session:
            coros.append(self._post_reg_fetch_session(job, result.cookies))
        if self._post_reg_get_link:
            coros.append(self._post_reg_fetch_link(job, result.access_token))
        if coros:
            await asyncio.gather(*coros)

    async def _post_reg_fetch_session(self, job: Job, cookies: list | None) -> None:
        """Post-reg: GET /api/auth/session bằng cookies đã có (HTTP only, không re-login)."""
        try:
            self._job_log(job, "[post-reg] fetching session...")
            from ..session_phase import fetch_session_via_http
            data = None
            for ses_attempt in range(1, 4):
                try:
                    data = await fetch_session_via_http(
                        cookies=cookies,
                        proxy=getattr(job, "_active_proxy", None),
                        timeout=30.0,
                    )
                    break
                except Exception as exc:
                    self._job_log(job, f"[post-reg] get-session attempt {ses_attempt} failed: {exc}")
                    if ses_attempt < 3:
                        await asyncio.sleep(2.0)
            if data:
                job.session_data = data
                user_email = (data.get("user") or {}).get("email", "?")
                self._job_log(job, f"[post-reg] session OK — user: {user_email}")
            else:
                self._job_log(job, "[post-reg] get-session failed after 3 attempts")
        except Exception as exc:
            self._job_log(job, f"[post-reg] get-session failed: {exc}")

    async def _post_reg_fetch_link(self, job: Job, access_token: str | None) -> None:
        """Post-reg: lấy payment link bằng access_token đã có (không re-login)."""
        try:
            self._job_log(job, "[post-reg] fetching payment link...")
            if not access_token:
                raise RuntimeError("access_token rỗng từ SignupResult")
            from ..payment_link import get_checkout_url, SessionExpiredError, CloudflareBlockedError
            region = job.region or self._post_reg_link_region
            self._job_log(job, f"[post-reg] region={region}")
            url = None
            for attempt in range(1, 3):
                try:
                    url = await asyncio.wait_for(
                        get_checkout_url(access_token, proxy=getattr(job, "_active_proxy", None), region=region),
                        timeout=30.0,
                    )
                    break
                except asyncio.TimeoutError:
                    self._job_log(job, f"[post-reg] get-link attempt {attempt}/2 timeout 30s")
                except SessionExpiredError as exc:
                    self._job_log(job, f"[post-reg] get-link: session expired — no retry: {exc}")
                    break
                except CloudflareBlockedError as exc:
                    self._job_log(job, f"[post-reg] get-link: cloudflare block — no retry: {exc}")
                    break
                except Exception as exc:
                    self._job_log(job, f"[post-reg] get-link attempt {attempt}/2 failed: {exc}")
                if attempt < 2:
                    await asyncio.sleep(1.5)
            if url:
                job.payment_link = url
                self._job_log(job, f"[post-reg] payment link: {url}")
                try:
                    settings = load_settings()
                    _append_link_log(
                        session_dir=runtime_session_dir(settings),
                        payment_link=url,
                    )
                except Exception:
                    pass
        except Exception as exc:
            self._job_log(job, f"[post-reg] get-link failed: {exc}")


# Singleton
_manager: JobManager | None = None


def get_manager(
    job_repo: "JobRepository | None" = None,
    combo_repo: "ComboRepository | None" = None,
    session_repo: "SessionResultRepository | None" = None,
) -> JobManager:
    global _manager
    if _manager is None:
        _manager = JobManager(max_concurrent=1, job_repo=job_repo, combo_repo=combo_repo, session_repo=session_repo)
        # Recovery: load persisted SIGNUP jobs from SQLite on first creation
        if job_repo is not None:
            try:
                recovered = job_repo.recover_interrupted()
            except Exception as exc:
                _log.warning("Job recovery from SQLite failed: %s", exc)
                recovered = []
            # Load completed jobs (success/error/cancelled) for UI history
            try:
                completed = job_repo.list_completed()
            except Exception as exc:
                _log.warning("Loading completed jobs from SQLite failed: %s", exc)
                completed = []
            # Add completed jobs first (historical), then recovered (active)
            # Filter by job_type='signup' — session/link jobs handled by their own managers
            # Sort ALL toàn cục theo created_at để preserve original creation order
            all_rows: list[tuple[dict, bool]] = [
                (row, False) for row in completed if row.get("job_type", "signup") == "signup"
            ] + [
                (row, True) for row in recovered if row.get("job_type", "signup") == "signup"
            ]
            all_rows.sort(key=lambda x: x[0].get("created_at", 0))

            for row, is_recovered in all_rows:
                # Deserialize session_data JSON nếu có
                raw_sd = row.get("session_data")
                _session_data: dict[str, Any] | None = None
                if raw_sd and isinstance(raw_sd, str):
                    try:
                        _session_data = json.loads(raw_sd)
                    except (json.JSONDecodeError, ValueError):
                        pass

                if is_recovered:
                    job = Job(
                        id=row["id"],
                        email=row["email"],
                        combo=row["combo"],
                        mail_mode=row.get("mail_mode", "outlook"),
                        status="queued",
                        error=row.get("error"),
                        password=row.get("password"),
                        secret=row.get("secret"),
                        first_code=row.get("first_code"),
                        user_id=row.get("user_id"),
                        session_path=row.get("session_path"),
                        payment_link=row.get("payment_link"),
                        region=row.get("region"),
                        session_data=_session_data,
                        created_at=row["created_at"],
                        started_at=None,
                        finished_at=row.get("finished_at"),
                    )
                    # B6 fix: nếu Phase1+2 đã xong (session_path đã lưu) nhưng
                    # 2FA chưa enroll (secret rỗng) → recovery phải resume vào
                    # nhánh 2FA-only thay vì chạy lại full signup. Không gán
                    # flag này → worker sẽ register email đã tồn tại → 409 →
                    # loop screen detection cho tới khi timeout 240s.
                    if row.get("session_path") and not row.get("secret"):
                        job._retry_2fa_only = True  # type: ignore[attr-defined]
                else:
                    job = Job(
                        id=row["id"],
                        email=row["email"],
                        combo=row["combo"],
                        mail_mode=row.get("mail_mode", "outlook"),
                        status=row["status"],
                        error=row.get("error"),
                        password=row.get("password"),
                        secret=row.get("secret"),
                        first_code=row.get("first_code"),
                        user_id=row.get("user_id"),
                        session_path=row.get("session_path"),
                        payment_link=row.get("payment_link"),
                        region=row.get("region"),
                        session_data=_session_data,
                        created_at=row["created_at"],
                        started_at=row.get("started_at"),
                        finished_at=row.get("finished_at"),
                    )
                # Load log lines for this job
                try:
                    log_rows = job_repo.get_logs(row["id"])
                    job.log_lines = [lr["line"] for lr in log_rows]
                except Exception as exc:
                    _log.warning("Failed to load logs for job %s: %s", row["id"], exc)
                _manager.jobs[job.id] = job
                _manager.order.append(job.id)
                if is_recovered:
                    _manager._job_queue.put_nowait(job.id)
            loaded_total = len(completed) + len(recovered)
            if loaded_total:
                _log.info(
                    "Loaded %d job(s) from SQLite (%d completed, %d to recover)",
                    loaded_total, len(completed), len(recovered),
                )
                _manager._ensure_workers()
    return _manager


# ─────────────────────────────────────────────────────────────────────
# SessionJobManager — Get Session feature
# ─────────────────────────────────────────────────────────────────────

from ..session_phase import SessionError, get_session  # noqa: E402


@dataclass
class SessionJob:
    id: str
    email: str
    password: str
    secret: str | None = None
    reg_mode: str = "browser"  # "pure_request" or "browser"
    status: JobStatus = "queued"
    log_lines: list[str] = field(default_factory=list)
    error: str | None = None
    session_data: dict[str, Any] | None = None  # full /api/auth/session JSON
    retry_count: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    def _extract_plan_type(self) -> str | None:
        """Lấy account.planType từ session_data (nếu có).

        ChatGPT /api/auth/session trả về object có thể chứa `account.planType`
        ('free' / 'plus' / 'team' / ...). Chỉ trả về str non-empty, ngược lại None.
        """
        if not isinstance(self.session_data, dict):
            return None
        account = self.session_data.get("account")
        if not isinstance(account, dict):
            return None
        pt = account.get("planType")
        if not isinstance(pt, str):
            return None
        pt = pt.strip()
        return pt or None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "status": self.status,
            "error": self.error,
            "has_session": self.session_data is not None,
            "plan_type": self._extract_plan_type(),
            "retry_count": self.retry_count,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": (
                (self.finished_at or time.time()) - self.started_at if self.started_at else None
            ),
            "log_count": len(self.log_lines),
        }

    def to_dict_full(self) -> dict[str, Any]:
        d = self.to_dict()
        d["log_lines"] = list(self.log_lines)
        d["session_data"] = self.session_data
        return d


class SessionJobManager:
    """Quản lý Get Session jobs — worker pool pattern tương tự JobManager.

    Persist vào SQLite qua JobRepository (job_type='session').
    Recovery sau restart qua recover_interrupted().
    """

    def __init__(self, *, max_concurrent: int = 1, job_repo: "JobRepository | None" = None):
        self.jobs: dict[str, SessionJob] = {}
        self.order: list[str] = []
        self._max = max_concurrent
        self._headless = True
        self._debug = False
        self._job_timeout = _DEFAULT_JOB_TIMEOUT
        self._tasks: dict[str, asyncio.Task] = {}
        self._job_queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._worker_started = False
        self._shutting_down: bool = False
        self._persist_running_retries: dict[str, int] = {}
        self._persist_running_max_retries = 3
        self._delayed_requeue_tasks: dict[str, asyncio.Task] = {}
        self._job_repo: "JobRepository | None" = job_repo
        # Auto-retry
        self._auto_retry: bool = False
        self._auto_retry_max: int = 3
        self._auto_retry_delay: float = 15.0
        # Stagger: random 5-10s giữa các start
        self._stagger_lock = asyncio.Lock()
        self._last_start_ts: float = 0.0
        self._stagger_min_seconds = 5.0
        self._stagger_max_seconds = 10.0

    @property
    def headless(self) -> bool:
        return self._headless

    def set_headless(self, value: bool) -> None:
        self._headless = bool(value)

    @property
    def debug(self) -> bool:
        return self._debug

    def set_debug(self, value: bool) -> None:
        self._debug = bool(value)

    @property
    def auto_retry(self) -> bool:
        return self._auto_retry

    @property
    def auto_retry_max(self) -> int:
        return self._auto_retry_max

    @property
    def auto_retry_delay(self) -> float:
        return self._auto_retry_delay

    def set_auto_retry(self, enabled: bool, *, max_retries: int | None = None, delay: float | None = None) -> None:
        self._auto_retry = bool(enabled)
        if max_retries is not None:
            self._auto_retry_max = max(1, min(max_retries, 10))
        if delay is not None:
            self._auto_retry_delay = max(5.0, min(delay, 120.0))

    def apply_settings(self, settings: dict) -> None:
        """Hydrate fields from settings dict (startup boot). Only set if key present."""
        if "reg.headless" in settings:
            self._headless = bool(settings["reg.headless"])
        if "reg.debug" in settings:
            self._debug = bool(settings["reg.debug"])
        if "reg.job_timeout" in settings:
            val = float(settings["reg.job_timeout"])
            if 30 <= val <= 600:
                self._job_timeout = val
        _hydrate_proxy_pool_from_settings(settings)
        if "reg.max_concurrent" in settings:
            val = int(settings["reg.max_concurrent"])
            if 1 <= val <= 10:
                self._max = val
        if "reg.auto_retry" in settings:
            self._auto_retry = bool(settings["reg.auto_retry"])
        if "reg.auto_retry_max" in settings:
            val = int(settings["reg.auto_retry_max"])
            if 0 <= val <= 10:
                self._auto_retry_max = val
        if "reg.auto_retry_delay" in settings:
            val = float(settings["reg.auto_retry_delay"])
            if val >= 5.0:
                self._auto_retry_delay = val

    async def _maybe_auto_retry(self, job: SessionJob) -> bool:
        if not self._auto_retry:
            return False
        if _is_fatal_error(job.error):
            self._job_log(job, f"[auto-retry] combo lỗi fatal — không retry")
            return False
        if job.retry_count >= self._auto_retry_max:
            self._job_log(job, f"[auto-retry] đã retry {job.retry_count}/{self._auto_retry_max} lần — dừng")
            return False
        job.retry_count += 1
        delay = self._auto_retry_delay * job.retry_count
        self._job_log(job, f"[auto-retry] sẽ retry {job.retry_count}/{self._auto_retry_max} sau {delay:.0f}s")
        if not await self._persist_status(job, "queued"):
            return False
        job.status = "queued"
        job.error = None
        job.started_at = None
        job.finished_at = None
        self._broadcast_job(job)
        self._schedule_delayed_requeue(job.id, delay)
        return True

    def _ensure_workers(self) -> None:
        if not self._worker_started:
            self._worker_started = True
        self._workers = [t for t in self._workers if not t.done()]
        while len(self._workers) < self._max:
            task = asyncio.create_task(self._worker_loop())
            self._workers.append(task)
        while len(self._workers) > self._max:
            task = self._workers.pop()
            task.cancel()

    async def _worker_loop(self) -> None:
        try:
            while True:
                job_id = await self._job_queue.get()
                job = self.jobs.get(job_id)
                if job is None or job.status != "queued":
                    continue
                if self._max > 1:
                    async with self._stagger_lock:
                        now = time.monotonic()
                        wait_min = self._last_start_ts + self._stagger_min_seconds - now
                        if wait_min > 0:
                            jitter = random.uniform(
                                self._stagger_min_seconds, self._stagger_max_seconds,
                            )
                            wait = max(wait_min, jitter)
                            self._last_start_ts = now + wait
                        else:
                            wait = 0.0
                            self._last_start_ts = now
                    if wait > 0:
                        self._job_log(job, f"[stagger] đợi {wait:.1f}s trước khi start")
                        deadline = time.monotonic() + wait
                        while True:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                break
                            await asyncio.sleep(min(0.25, remaining))
                            cur = self.jobs.get(job_id)
                            if cur is None or cur.status != "queued":
                                break
                    cur = self.jobs.get(job_id)
                    if cur is None or cur.status != "queued":
                        continue
                inner = asyncio.create_task(self._run_job(job))
                self._tasks[job_id] = inner
                try:
                    await inner
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelled():
                        raise
                    if inner.cancelled():
                        # job bị cancel — worker tiếp tục vòng kế
                        continue
                    raise
                finally:
                    self._tasks.pop(job_id, None)
        except asyncio.CancelledError:
            pass

    @property
    def max_concurrent(self) -> int:
        return self._max

    def set_max_concurrent(self, n: int) -> None:
        if n < 1 or n > 10:
            raise ValueError("max_concurrent phải trong [1, 10]")
        self._max = n
        self._ensure_workers()

    @property
    def job_timeout(self) -> float:
        return self._job_timeout

    def set_job_timeout(self, seconds: float) -> None:
        if seconds < 30 or seconds > 600:
            raise ValueError("job_timeout phải trong [30, 600]")
        self._job_timeout = float(seconds)

    def _broadcast(self, event: dict[str, Any]) -> None:
        if _sse_mux is not None:
            _sse_mux.publish("session", event)

    def _schedule_delayed_requeue(self, job_id: str, delay: float) -> None:
        """SessionMgr: spawn detached task để requeue job sau `delay` giây.

        Tách ra ngoài cây gọi của `wait_for(job_timeout)` để timeout cancel
        không nuốt requeue khi delay >= job_timeout.
        """
        async def _runner() -> None:
            try:
                await asyncio.sleep(delay)
                if self._shutting_down:
                    return
                job = self.jobs.get(job_id)
                if job is None:
                    return
                # Chống duplicate enqueue (xem JobManager._schedule_delayed_requeue).
                if job.status != "queued":
                    return
                self._job_queue.put_nowait(job_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover
                _log.error("SessionMgr: delayed requeue for %s failed: %s", job_id, exc)

        if self._shutting_down:
            return
        existing = self._delayed_requeue_tasks.get(job_id)
        if existing is not None and not existing.done():
            existing.cancel()
        task = asyncio.create_task(_runner())
        self._delayed_requeue_tasks[job_id] = task
        task.add_done_callback(lambda _t, jid=job_id: self._delayed_requeue_tasks.pop(jid, None) if self._delayed_requeue_tasks.get(jid) is _t else None)

    def _job_log(self, job: SessionJob, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        job.log_lines.append(line)
        if len(job.log_lines) > 500:
            job.log_lines = job.log_lines[-500:]
        if self._job_repo is not None and job.id in self.jobs:
            try:
                self._job_repo.append_log(job.id, line)
            except Exception as exc:
                _log.warning("SessionMgr: append_log failed for %s: %s", job.id, exc)
        self._broadcast({"type": "log", "job_id": job.id, "line": line})

    def _broadcast_job(self, job: SessionJob) -> None:
        self._broadcast({"type": "job", "job": job.to_dict()})

    async def _persist_status(self, job: SessionJob, status: str, **kwargs: object) -> bool:
        if self._job_repo is None:
            return True
        try:
            repo = self._job_repo
            job_id = job.id
            await repo.engine.run_sync(lambda: repo.update_status(job_id, status, **kwargs))
            return True
        except Exception as exc:
            _log.warning("SessionMgr: update_status failed for %s → %s: %s", job.id, status, exc)
            return False

    def add_jobs(self, combos: list[str], reg_mode: str = "browser") -> list[SessionJob]:
        """Parse input lines: email|password|secret. Dedup theo email."""
        existing_emails = {j.email.lower() for j in self.jobs.values() if j.status != "cancelled"}
        out: list[SessionJob] = []
        for raw in combos:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) < 2:
                jid = uuid.uuid4().hex[:12]
                job = SessionJob(
                    id=jid, email="<invalid>", password="",
                    status="error", error=f"format sai, cần email|password|secret: {line[:60]}",
                    finished_at=time.time(),
                )
                if self._job_repo is not None:
                    try:
                        self._job_repo.create({
                            "id": job.id, "email": job.email, "combo": line[:80],
                            "mail_mode": "outlook", "status": job.status,
                            "password": job.password, "secret": job.secret,
                            "error": job.error, "created_at": job.created_at,
                            "finished_at": job.finished_at, "job_type": "session",
                        })
                    except Exception as exc_db:
                        _log.warning("SessionMgr: persist failed for %s: %s", jid, exc_db)
                        continue
                self.jobs[jid] = job
                self.order.append(jid)
                self._broadcast_job(job)
                out.append(job)
                continue

            email = parts[0].strip()
            password = parts[1].strip()
            secret = parts[2].strip() if len(parts) >= 3 else None

            if email.lower() in existing_emails:
                continue
            existing_emails.add(email.lower())

            jid = uuid.uuid4().hex[:12]
            job = SessionJob(id=jid, email=email, password=password, secret=secret, reg_mode=reg_mode)
            if self._job_repo is not None:
                try:
                    self._job_repo.create({
                        "id": job.id, "email": job.email,
                        "combo": f"{email}|{password}|{secret or ''}",
                        "mail_mode": "outlook", "status": job.status,
                        "password": password, "secret": secret,
                        "created_at": job.created_at, "job_type": "session",
                    })
                except Exception as exc_db:
                    _log.warning("SessionMgr: persist failed for %s: %s", jid, exc_db)
                    continue
            self.jobs[jid] = job
            self.order.append(jid)
            self._broadcast_job(job)
            out.append(job)

        self._ensure_workers()
        for j in out:
            if j.status == "queued":
                self._job_queue.put_nowait(j.id)
        return out

    async def stop_all(self) -> int:
        while not self._job_queue.empty():
            try:
                self._job_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        count = 0
        for job_id, job in list(self.jobs.items()):
            if job.status in ("running", "queued"):
                if not await self._persist_status(job, "cancelled"):
                    continue
                task = self._tasks.get(job_id)
                if task and not task.done():
                    task.cancel()
                job.status = "cancelled"
                job.finished_at = time.time()
                self._broadcast_job(job)
                count += 1
        self._last_start_ts = 0.0
        return count

    def shutdown(self) -> None:
        """Cancel worker tasks để event loop thoát sạch khi app shutdown."""
        self._shutting_down = True
        for t in list(self._delayed_requeue_tasks.values()):
            if not t.done():
                t.cancel()
        self._delayed_requeue_tasks.clear()
        for w in self._workers:
            if not w.done():
                w.cancel()
        self._workers.clear()

    def clear_finished(self) -> int:
        """Xoá jobs đã xong (success/error). Giữ cancelled để user retry."""
        if self._job_repo is not None:
            try:
                self._job_repo.delete_finished(job_type="session")
            except Exception as exc:
                _log.warning("SessionMgr: delete_finished failed: %s", exc)
                return -1
        removed = 0
        for jid in list(self.order):
            job = self.jobs.get(jid)
            if job and job.status in ("success", "error"):
                self.jobs.pop(jid, None)
                self.order.remove(jid)
                self._tasks.pop(jid, None)
                removed += 1
        if removed:
            self._broadcast({"type": "clear_finished", "removed": removed})
        return removed

    def remove_job(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None:
            return False
        # Persist deletion to SQLite FIRST
        if self._job_repo is not None:
            try:
                self._job_repo.delete(job_id)
            except Exception as exc:
                _log.warning("SessionMgr: delete failed for %s: %s", job_id, exc)
                return False
        # SQLite succeeded (or no repo) → safe to mutate memory state
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        self.jobs.pop(job_id, None)
        if job_id in self.order:
            self.order.remove(job_id)
        self._tasks.pop(job_id, None)
        self._broadcast({"type": "remove", "job_id": job_id})
        return True

    async def retry_job(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None:
            return False
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        # Cancel pending delayed-requeue helper để tránh duplicate enqueue.
        pending = self._delayed_requeue_tasks.pop(job_id, None)
        if pending is not None and not pending.done():
            pending.cancel()
        if not await self._persist_status(job, "queued", password=job.password, secret=job.secret):
            return False
        job.status = "queued"
        job.error = None
        job.session_data = None
        job.started_at = None
        job.finished_at = None
        retry_line = f"[{datetime.now():%H:%M:%S}] -- retry --"
        if self._job_repo is not None:
            try:
                self._job_repo.append_log(job.id, retry_line)
            except Exception as exc:
                _log.warning("SessionMgr: SQLite append_log (retry marker) failed for %s: %s", job.id, exc)
        job.log_lines.append(retry_line)
        self._broadcast_job(job)
        self._broadcast({"type": "log", "job_id": job_id, "line": retry_line})
        self._ensure_workers()
        self._job_queue.put_nowait(job_id)
        return True

    def list_jobs(self) -> list[dict[str, Any]]:
        return [self.jobs[jid].to_dict() for jid in self.order if jid in self.jobs]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        return job.to_dict_full() if job else None

    def get_log(self, job_id: str) -> list[str]:
        job = self.jobs.get(job_id)
        return list(job.log_lines) if job else []

    def _begin_job_proxy(self, job: SessionJob, log) -> str | None:
        """Resolve proxy cho 1 job từ proxy pool (round_robin/random)."""
        proxy = _resolve_job_proxy()
        job._active_proxy = proxy  # type: ignore[attr-defined]
        if proxy:
            log(f"[proxy] using {_mask_proxy(proxy)}")
        return proxy

    def _note_proxy_failure(self, job: SessionJob, exc_or_msg) -> None:
        """Mark proxy chết nếu lỗi là network/proxy → loại khỏi vòng xoay."""
        proxy = getattr(job, "_active_proxy", None)
        if proxy and _is_proxy_network_error(exc_or_msg):
            if get_proxy_pool().mark_dead(proxy):
                self._job_log(
                    job, f"[proxy] {_mask_proxy(proxy)} lỗi network — loại khỏi pool"
                )

    async def _run_job(self, job: SessionJob) -> None:
        self._tasks[job.id] = asyncio.current_task()  # type: ignore[arg-type]
        try:
            if job.id not in self.jobs:
                return
            if not await self._persist_status(job, "running"):
                # Transient SQLite fail — requeue với bounded retry
                retries = self._persist_running_retries.get(job.id, 0) + 1
                if retries <= self._persist_running_max_retries:
                    self._persist_running_retries[job.id] = retries
                    _log.warning(
                        "SessionMgr: persist running failed for job %s — requeue (attempt %d/%d)",
                        job.id, retries, self._persist_running_max_retries,
                    )
                    await asyncio.sleep(min(retries * 2.0, 6.0))
                    self._job_queue.put_nowait(job.id)
                else:
                    # Re-enqueue so job isn't permanently stuck in dead state.
                    # Job stays queued in both memory and DB (Req 10.3/10.4 respected).
                    _log.critical(
                        "SessionMgr: persist running failed for job %s after %d retries — "
                        "re-enqueueing with extended delay (SQLite may recover)",
                        job.id, retries,
                    )
                    self._persist_running_retries.pop(job.id, None)
                    # Detached task — không bị wait_for(job_timeout) cancel khi sleep.
                    self._schedule_delayed_requeue(job.id, 30.0)
                return
            self._persist_running_retries.pop(job.id, None)
            job.status = "running"
            job.started_at = time.time()
            self._broadcast_job(job)

            def log(msg: str) -> None:
                self._job_log(job, msg)

            job_proxy = self._begin_job_proxy(job, log)

            # Get Session fix cứng dùng pure_request (HTTP-only, không browser).
            from ..session_phase import get_session_pure_request
            session_data = await asyncio.wait_for(
                get_session_pure_request(
                    email=job.email,
                    password=job.password,
                    secret=job.secret,
                    proxy=job_proxy,
                    log=log,
                ),
                timeout=self._job_timeout,
            )

            job.session_data = session_data
            if await self._persist_status(
                job, "success",
                session_data=json.dumps(session_data, ensure_ascii=False) if session_data else None,
            ):
                job.status = "success"
                job.finished_at = time.time()
                self._broadcast_job(job)

        except asyncio.TimeoutError:
            error_msg = f"timeout {self._job_timeout:.0f}s exceeded"
            if await self._persist_status(job, "error", error=error_msg):
                job.status = "error"
                job.error = error_msg
                job.finished_at = time.time()
                self._job_log(job, f"[fatal] timeout {self._job_timeout:.0f}s")
                self._broadcast_job(job)
        except asyncio.CancelledError:
            if not self._shutting_down and job.id in self.jobs:
                if await self._persist_status(job, "cancelled"):
                    job.status = "cancelled"
                    job.finished_at = time.time()
                    self._broadcast_job(job)
            raise
        except SessionError as exc:
            error_msg = str(exc)
            self._note_proxy_failure(job, exc)
            if await self._persist_status(job, "error", error=error_msg):
                job.status = "error"
                job.error = error_msg
                job.finished_at = time.time()
                self._job_log(job, f"[error] {exc}")
                self._broadcast_job(job)
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            self._note_proxy_failure(job, exc)
            if await self._persist_status(job, "error", error=error_msg):
                job.status = "error"
                job.error = error_msg
                job.finished_at = time.time()
                self._job_log(job, f"[fatal] {error_msg}")
                self._broadcast_job(job)
        finally:
            self._tasks.pop(job.id, None)
            if job.status == "error" and job.id in self.jobs:
                await self._maybe_auto_retry(job)


# Singleton
_session_manager: SessionJobManager | None = None


def get_session_manager(job_repo: "JobRepository | None" = None) -> SessionJobManager:
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionJobManager(max_concurrent=1, job_repo=job_repo)
        # Recovery: load persisted session jobs
        if job_repo is not None:
            try:
                recovered = job_repo.recover_interrupted()
                completed = job_repo.list_completed()
                # Filter by job_type='session'
                all_rows = [
                    (row, False) for row in completed if row.get("job_type") == "session"
                ] + [
                    (row, True) for row in recovered if row.get("job_type") == "session"
                ]
                all_rows.sort(key=lambda t: t[0]["created_at"])
                for row, is_recovered in all_rows:
                    password = row.get("password") or ""
                    secret = row.get("secret")
                    combo = row.get("combo") or ""
                    parts = combo.split("|") if isinstance(combo, str) else []
                    if not password and len(parts) >= 2:
                        password = parts[1].strip()
                    if secret is None and len(parts) >= 3:
                        secret = parts[2].strip() or None
                    # Deserialize session_data JSON
                    raw_sd = row.get("session_data")
                    _sd: dict[str, Any] | None = None
                    if raw_sd and isinstance(raw_sd, str):
                        try:
                            _sd = json.loads(raw_sd)
                        except (json.JSONDecodeError, ValueError):
                            pass
                    job = SessionJob(
                        id=row["id"],
                        email=row["email"],
                        password=password,
                        secret=secret,
                        status="queued" if is_recovered else row["status"],
                        error=row.get("error"),
                        session_data=_sd,
                        created_at=row["created_at"],
                        started_at=row.get("started_at"),
                        finished_at=row.get("finished_at"),
                    )
                    try:
                        log_rows = job_repo.get_logs(row["id"])
                        job.log_lines = [lr["line"] for lr in log_rows]
                    except Exception:
                        pass
                    _session_manager.jobs[job.id] = job
                    _session_manager.order.append(job.id)
                    if is_recovered:
                        _session_manager._job_queue.put_nowait(job.id)
                if all_rows:
                    _session_manager._ensure_workers()
            except Exception as exc:
                _log.warning("SessionMgr recovery failed: %s", exc)
    return _session_manager

# ─────────────────────────────────────────────────────────────────────
# LinkJobManager — Get Link feature
# ─────────────────────────────────────────────────────────────────────

from ..payment_link import get_checkout_url, PaymentLinkError  # noqa: E402


LinkMode = Literal["combo", "session_json", "access_token"]


@dataclass
class LinkJob:
    id: str
    email: str
    password: str
    secret: str | None = None
    mode: LinkMode = "combo"
    reg_mode: str = "browser"  # "pure_request" or "browser"
    # Pre-provided token (dùng cho mode session_json / access_token)
    _access_token: str | None = field(default=None, repr=False)
    status: JobStatus = "queued"
    log_lines: list[str] = field(default_factory=list)
    error: str | None = None
    payment_link: str | None = None
    region: str = "VN"  # snapshot region tại lúc add_jobs (per-job, không đổi theo dropdown)
    retry_count: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "mode": self.mode,
            "status": self.status,
            "error": self.error,
            "payment_link": self.payment_link,
            "region": self.region,
            "retry_count": self.retry_count,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": (
                (self.finished_at or time.time()) - self.started_at if self.started_at else None
            ),
            "log_count": len(self.log_lines),
        }

    def to_dict_full(self) -> dict[str, Any]:
        d = self.to_dict()
        d["log_lines"] = list(self.log_lines)
        d["access_token"] = self._access_token
        return d


def _serialize_link_combo(job: LinkJob) -> str:
    """Store enough link-job input state in the existing jobs.combo column."""
    return json.dumps(
        {
            "mode": job.mode,
            "email": job.email,
            "password": job.password,
            "secret": job.secret,
            "access_token": job._access_token,
            "region": job.region,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _restore_link_fields(row: dict) -> tuple[LinkMode, str, str | None, str | None, str]:
    """Restore link mode/input state from persisted row, with legacy fallback.

    Returns: (mode, password, secret, access_token, region).
    region đọc từ jobs.region column (v4) nếu có, fallback từ combo JSON, fallback "VN".
    """
    combo = row.get("combo") or ""
    password = row.get("password") or ""
    secret = row.get("secret")
    mode: LinkMode = "combo"
    access_token: str | None = None
    region: str = row.get("region") or "VN"

    if isinstance(combo, str) and combo.startswith("{"):
        try:
            data = json.loads(combo)
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict):
            raw_mode = data.get("mode")
            if raw_mode in ("combo", "session_json", "access_token"):
                mode = raw_mode
            password = password or data.get("password") or ""
            if secret is None:
                secret = data.get("secret")
            raw_token = data.get("access_token")
            if isinstance(raw_token, str) and raw_token:
                access_token = raw_token
            # Region từ column v4 ưu tiên; fallback combo JSON cho rows trước v4
            if not row.get("region"):
                region_from_combo = data.get("region")
                if isinstance(region_from_combo, str) and region_from_combo:
                    region = region_from_combo
            return mode, password, secret, access_token, region

    # Legacy rows stored combo as email|password|secret.
    parts = combo.split("|") if isinstance(combo, str) else []
    if len(parts) >= 2:
        password = password or parts[1].strip()
    if secret is None and len(parts) >= 3:
        secret = parts[2].strip() or None
    return mode, password, secret, access_token, region


class LinkJobManager:
    """Quản lý Get Link jobs — login via browser → get payment link.

    Persist vào SQLite qua JobRepository (job_type='link').
    Recovery sau restart qua recover_interrupted().
    """

    def __init__(self, *, max_concurrent: int = 1, job_repo: "JobRepository | None" = None):
        self.jobs: dict[str, LinkJob] = {}
        self.order: list[str] = []
        self._max = max_concurrent
        self._headless = True
        self._debug = False
        self._job_timeout = 180.0
        self._region: str = "VN"
        self._tasks: dict[str, asyncio.Task] = {}
        self._job_queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._worker_started = False
        self._shutting_down: bool = False
        self._persist_running_retries: dict[str, int] = {}
        self._persist_running_max_retries = 3
        self._delayed_requeue_tasks: dict[str, asyncio.Task] = {}
        self._job_repo: "JobRepository | None" = job_repo
        # Auto-retry
        self._auto_retry: bool = False
        self._auto_retry_max: int = 3
        self._auto_retry_delay: float = 15.0
        # Stagger: random 5-10s giữa các start
        self._stagger_lock = asyncio.Lock()
        self._last_start_ts: float = 0.0
        self._stagger_min_seconds = 5.0
        self._stagger_max_seconds = 10.0

    @property
    def region(self) -> str:
        return self._region

    def set_region(self, value: str) -> None:
        from ..payment_link import REGION_BILLING
        if value not in REGION_BILLING:
            raise ValueError(f"invalid region: {value}. Must be one of: {list(REGION_BILLING.keys())}")
        self._region = value

    @property
    def headless(self) -> bool:
        return self._headless

    def set_headless(self, value: bool) -> None:
        self._headless = bool(value)

    @property
    def debug(self) -> bool:
        return self._debug

    def set_debug(self, value: bool) -> None:
        self._debug = bool(value)

    @property
    def auto_retry(self) -> bool:
        return self._auto_retry

    @property
    def auto_retry_max(self) -> int:
        return self._auto_retry_max

    @property
    def auto_retry_delay(self) -> float:
        return self._auto_retry_delay

    def set_auto_retry(self, enabled: bool, *, max_retries: int | None = None, delay: float | None = None) -> None:
        self._auto_retry = bool(enabled)
        if max_retries is not None:
            self._auto_retry_max = max(1, min(max_retries, 10))
        if delay is not None:
            self._auto_retry_delay = max(5.0, min(delay, 120.0))

    def apply_settings(self, settings: dict) -> None:
        """Hydrate fields from settings dict (startup boot). Only set if key present."""
        if "reg.headless" in settings:
            self._headless = bool(settings["reg.headless"])
        if "reg.debug" in settings:
            self._debug = bool(settings["reg.debug"])
        if "reg.job_timeout" in settings:
            val = float(settings["reg.job_timeout"])
            if 30 <= val <= 600:
                self._job_timeout = val
        _hydrate_proxy_pool_from_settings(settings)
        if "reg.max_concurrent" in settings:
            val = int(settings["reg.max_concurrent"])
            if 1 <= val <= 10:
                self._max = val
        if "reg.post_reg_link_region" in settings:
            v = str(settings["reg.post_reg_link_region"]).strip().upper()
            if v:
                self._region = v
        if "reg.auto_retry" in settings:
            self._auto_retry = bool(settings["reg.auto_retry"])
        if "reg.auto_retry_max" in settings:
            val = int(settings["reg.auto_retry_max"])
            if 0 <= val <= 10:
                self._auto_retry_max = val
        if "reg.auto_retry_delay" in settings:
            val = float(settings["reg.auto_retry_delay"])
            if val >= 5.0:
                self._auto_retry_delay = val

    async def _maybe_auto_retry(self, job: LinkJob) -> bool:
        if not self._auto_retry:
            return False
        if _is_fatal_error(job.error):
            self._job_log(job, f"[auto-retry] combo lỗi fatal — không retry")
            return False
        if job.retry_count >= self._auto_retry_max:
            self._job_log(job, f"[auto-retry] đã retry {job.retry_count}/{self._auto_retry_max} lần — dừng")
            return False
        job.retry_count += 1
        delay = self._auto_retry_delay * job.retry_count
        self._job_log(job, f"[auto-retry] sẽ retry {job.retry_count}/{self._auto_retry_max} sau {delay:.0f}s")
        if not await self._persist_status(job, "queued"):
            return False
        job.status = "queued"
        job.error = None
        job.payment_link = None
        job.started_at = None
        job.finished_at = None
        self._broadcast_job(job)
        self._schedule_delayed_requeue(job.id, delay)
        return True

    def _ensure_workers(self) -> None:
        if not self._worker_started:
            self._worker_started = True
        self._workers = [t for t in self._workers if not t.done()]
        while len(self._workers) < self._max:
            task = asyncio.create_task(self._worker_loop())
            self._workers.append(task)
        while len(self._workers) > self._max:
            task = self._workers.pop()
            task.cancel()

    async def _persist_status(self, job: LinkJob, status: str, **kwargs: object) -> bool:
        if self._job_repo is None:
            return True
        try:
            repo = self._job_repo
            job_id = job.id
            await repo.engine.run_sync(lambda: repo.update_status(job_id, status, **kwargs))
            return True
        except Exception as exc:
            _log.warning("LinkMgr: update_status failed for %s → %s: %s", job.id, status, exc)
            return False

    def _persist_job_create(self, job: LinkJob) -> bool:
        """Persist link job mới vào SQLite. Return False nếu fail."""
        if self._job_repo is None:
            return True
        try:
            self._job_repo.create({
                "id": job.id,
                "email": job.email,
                "combo": _serialize_link_combo(job),
                "mail_mode": "outlook",  # placeholder — job_type='link' phân biệt
                "status": job.status,
                "error": job.error,
                "password": job.password,
                "secret": job.secret,
                "payment_link": job.payment_link,
                "region": job.region,
                "created_at": job.created_at,
                "finished_at": job.finished_at,
                "job_type": "link",
            })
            return True
        except Exception as exc:
            _log.warning("LinkMgr: persist create failed for %s: %s", job.id, exc)
            return False

    async def _worker_loop(self) -> None:
        try:
            while True:
                job_id = await self._job_queue.get()
                job = self.jobs.get(job_id)
                if job is None or job.status != "queued":
                    continue
                if self._max > 1:
                    async with self._stagger_lock:
                        now = time.monotonic()
                        wait_min = self._last_start_ts + self._stagger_min_seconds - now
                        if wait_min > 0:
                            jitter = random.uniform(
                                self._stagger_min_seconds, self._stagger_max_seconds,
                            )
                            wait = max(wait_min, jitter)
                            self._last_start_ts = now + wait
                        else:
                            wait = 0.0
                            self._last_start_ts = now
                    if wait > 0:
                        self._job_log(job, f"[stagger] đợi {wait:.1f}s trước khi start")
                        deadline = time.monotonic() + wait
                        while True:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                break
                            await asyncio.sleep(min(0.25, remaining))
                            cur = self.jobs.get(job_id)
                            if cur is None or cur.status != "queued":
                                break
                    cur = self.jobs.get(job_id)
                    if cur is None or cur.status != "queued":
                        continue
                inner = asyncio.create_task(self._run_job(job))
                self._tasks[job_id] = inner
                try:
                    await inner
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelled():
                        raise
                    if inner.cancelled():
                        # job bị cancel — worker tiếp tục vòng kế
                        continue
                    raise
                finally:
                    self._tasks.pop(job_id, None)
        except asyncio.CancelledError:
            pass

    @property
    def max_concurrent(self) -> int:
        return self._max

    def set_max_concurrent(self, n: int) -> None:
        if n < 1 or n > 10:
            raise ValueError("max_concurrent phải trong [1, 10]")
        self._max = n
        self._ensure_workers()

    @property
    def job_timeout(self) -> float:
        return self._job_timeout

    def set_job_timeout(self, seconds: float) -> None:
        if seconds < 30 or seconds > 600:
            raise ValueError("job_timeout phải trong [30, 600]")
        self._job_timeout = float(seconds)

    def _broadcast(self, event: dict[str, Any]) -> None:
        if _sse_mux is not None:
            _sse_mux.publish("link", event)

    def _schedule_delayed_requeue(self, job_id: str, delay: float) -> None:
        """LinkMgr: spawn detached task để requeue job sau `delay` giây.

        Tách ra ngoài cây gọi của `wait_for(job_timeout)` để timeout cancel
        không nuốt requeue khi delay >= job_timeout.
        """
        async def _runner() -> None:
            try:
                await asyncio.sleep(delay)
                if self._shutting_down:
                    return
                job = self.jobs.get(job_id)
                if job is None:
                    return
                # Chống duplicate enqueue (xem JobManager._schedule_delayed_requeue).
                if job.status != "queued":
                    return
                self._job_queue.put_nowait(job_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover
                _log.error("LinkMgr: delayed requeue for %s failed: %s", job_id, exc)

        if self._shutting_down:
            return
        existing = self._delayed_requeue_tasks.get(job_id)
        if existing is not None and not existing.done():
            existing.cancel()
        task = asyncio.create_task(_runner())
        self._delayed_requeue_tasks[job_id] = task
        task.add_done_callback(lambda _t, jid=job_id: self._delayed_requeue_tasks.pop(jid, None) if self._delayed_requeue_tasks.get(jid) is _t else None)

    def _job_log(self, job: LinkJob, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        job.log_lines.append(line)
        if len(job.log_lines) > 500:
            job.log_lines = job.log_lines[-500:]
        if self._job_repo is not None and job.id in self.jobs:
            try:
                self._job_repo.append_log(job.id, line)
            except Exception as exc:
                _log.warning("LinkMgr: append_log failed for %s: %s", job.id, exc)
        self._broadcast({"type": "log", "job_id": job.id, "line": line})

    def _broadcast_job(self, job: LinkJob) -> None:
        self._broadcast({"type": "job", "job": job.to_dict()})

    def add_jobs(self, lines: list[str], *, mode: LinkMode = "combo", region: str | None = None, reg_mode: str = "browser") -> list[LinkJob]:
        """Parse input based on mode. Dedup theo email.

        Region được snapshot vào từng job (per-job), không mutate state global.
        Dropdown region chỉ là default cho batch tiếp theo, không ảnh hưởng job
        đang chạy hoặc job đã add trước đó với region khác.
        """
        # Snapshot region cho batch này. Nếu caller không truyền, dùng default state.
        from ..payment_link import REGION_BILLING
        if region is not None:
            region_resolved = region.strip().upper()
            if region_resolved not in REGION_BILLING:
                raise ValueError(f"invalid region: {region}. Must be one of: {list(REGION_BILLING.keys())}")
            # Cập nhật default global cho UI snapshot tiếp theo
            self._region = region_resolved
        else:
            region_resolved = self._region

        existing_emails = {j.email.lower() for j in self.jobs.values() if j.status != "cancelled"}
        out: list[LinkJob] = []

        if mode == "combo":
            out = self._parse_combo(lines, existing_emails, region_resolved, reg_mode)
        elif mode == "session_json":
            out = self._parse_session_json(lines, existing_emails, region_resolved)
        elif mode == "access_token":
            out = self._parse_access_token(lines, existing_emails, region_resolved)
        else:
            return out

        self._ensure_workers()
        for j in out:
            if j.status == "queued":
                self._job_queue.put_nowait(j.id)
        return out

    def _parse_combo(self, lines: list[str], existing_emails: set[str], region: str, reg_mode: str = "browser") -> list[LinkJob]:
        """Mode combo: email|password|secret per line."""
        out: list[LinkJob] = []
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) < 2:
                jid = uuid.uuid4().hex[:12]
                job = LinkJob(
                    id=jid, email="<invalid>", password="",
                    mode="combo", region=region,
                    status="error", error=f"format sai, cần email|password|secret: {line[:60]}",
                    finished_at=time.time(),
                )
                if not self._persist_job_create(job):
                    continue
                self.jobs[jid] = job
                self.order.append(jid)
                self._broadcast_job(job)
                out.append(job)
                continue

            email = parts[0].strip()
            password = parts[1].strip()
            secret = parts[2].strip() if len(parts) >= 3 else None

            if email.lower() in existing_emails:
                continue
            existing_emails.add(email.lower())

            jid = uuid.uuid4().hex[:12]
            job = LinkJob(id=jid, email=email, password=password, secret=secret, mode="combo", region=region, reg_mode=reg_mode)
            if not self._persist_job_create(job):
                continue  # skip if SQLite persist fails
            self.jobs[jid] = job
            self.order.append(jid)
            self._broadcast_job(job)
            out.append(job)
        return out

    def _parse_session_json(self, lines: list[str], existing_emails: set[str], region: str) -> list[LinkJob]:
        """Mode session_json: toàn bộ input là 1 JSON object duy nhất chứa accessToken."""
        out: list[LinkJob] = []
        full_text = "\n".join(lines).strip()
        if not full_text:
            return out

        try:
            data = json.loads(full_text)
        except (json.JSONDecodeError, ValueError) as exc:
            jid = uuid.uuid4().hex[:12]
            job = LinkJob(
                id=jid, email="<invalid>", password="",
                mode="session_json", region=region,
                status="error", error=f"invalid JSON: {exc}",
                finished_at=time.time(),
            )
            if not self._persist_job_create(job):
                return out
            self.jobs[jid] = job
            self.order.append(jid)
            self._broadcast_job(job)
            out.append(job)
            return out

        if not isinstance(data, dict):
            jid = uuid.uuid4().hex[:12]
            job = LinkJob(
                id=jid, email="<invalid>", password="",
                mode="session_json", region=region,
                status="error", error="JSON phải là object, không phải array/primitive",
                finished_at=time.time(),
            )
            if not self._persist_job_create(job):
                return out
            self.jobs[jid] = job
            self.order.append(jid)
            self._broadcast_job(job)
            out.append(job)
            return out

        token = data.get("accessToken") or ""
        user = data.get("user") or {}
        email = user.get("email") or f"token_{uuid.uuid4().hex[:6]}"

        if not token:
            jid = uuid.uuid4().hex[:12]
            job = LinkJob(
                id=jid, email=email, password="",
                mode="session_json", region=region,
                status="error", error="session JSON thiếu accessToken",
                finished_at=time.time(),
            )
            if not self._persist_job_create(job):
                return out
            self.jobs[jid] = job
            self.order.append(jid)
            self._broadcast_job(job)
            out.append(job)
            return out

        if email.lower() in existing_emails:
            return out
        existing_emails.add(email.lower())

        jid = uuid.uuid4().hex[:12]
        job = LinkJob(
            id=jid, email=email, password="", mode="session_json", region=region,
            _access_token=token,
        )
        if not self._persist_job_create(job):
            return out  # skip if persist fails
        self.jobs[jid] = job
        self.order.append(jid)
        self._broadcast_job(job)
        out.append(job)
        return out

    def _parse_access_token(self, lines: list[str], existing_emails: set[str], region: str) -> list[LinkJob]:
        """Mode access_token: mỗi line là 1 raw JWT."""
        out: list[LinkJob] = []
        for raw in lines:
            token = raw.strip()
            if not token or token.startswith("#"):
                continue

            # Tạo label từ token (email nếu decode được, hoặc token prefix)
            email = self._extract_email_from_jwt(token) or f"token_...{token[-8:]}"

            if email.lower() in existing_emails:
                continue
            existing_emails.add(email.lower())

            jid = uuid.uuid4().hex[:12]
            job = LinkJob(
                id=jid, email=email, password="", mode="access_token", region=region,
                _access_token=token,
            )
            if not self._persist_job_create(job):
                continue  # skip if persist fails
            self.jobs[jid] = job
            self.order.append(jid)
            self._broadcast_job(job)
            out.append(job)
        return out

    @staticmethod
    def _extract_email_from_jwt(token: str) -> str | None:
        """Decode JWT payload (no verify) để lấy email."""
        import base64
        parts = token.split(".")
        if len(parts) < 2:
            return None
        try:
            payload_b64 = parts[1]
            # Fix padding
            padding = 4 - len(payload_b64) % 4
            if padding != 4:
                payload_b64 += "=" * padding
            payload_bytes = base64.urlsafe_b64decode(payload_b64)
            payload = json.loads(payload_bytes)
            return payload.get("email") or payload.get("https://api.openai.com/auth", {}).get("email")
        except Exception:
            return None

    async def stop_all(self) -> int:
        while not self._job_queue.empty():
            try:
                self._job_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        count = 0
        for job_id, job in list(self.jobs.items()):
            if job.status in ("running", "queued"):
                if not await self._persist_status(job, "cancelled"):
                    continue
                task = self._tasks.get(job_id)
                if task and not task.done():
                    task.cancel()
                job.status = "cancelled"
                job.finished_at = time.time()
                self._broadcast_job(job)
                count += 1
        # Reset stagger debt — batch jobs mới sau stop_all không phải đợi
        # khoảng cách stagger tính từ batch cũ.
        self._last_start_ts = 0.0
        return count

    def shutdown(self) -> None:
        """Cancel worker tasks để event loop thoát sạch khi app shutdown."""
        self._shutting_down = True
        for t in list(self._delayed_requeue_tasks.values()):
            if not t.done():
                t.cancel()
        self._delayed_requeue_tasks.clear()
        for w in self._workers:
            if not w.done():
                w.cancel()
        self._workers.clear()

    def clear_finished(self) -> int:
        """Xoá jobs đã xong (success/error). Giữ cancelled để user retry."""
        if self._job_repo is not None:
            try:
                self._job_repo.delete_finished(job_type="link")
            except Exception as exc:
                _log.warning("LinkMgr: delete_finished failed: %s", exc)
                return -1
        removed = 0
        for jid in list(self.order):
            job = self.jobs.get(jid)
            if job and job.status in ("success", "error"):
                self.jobs.pop(jid, None)
                self.order.remove(jid)
                self._tasks.pop(jid, None)
                removed += 1
        if removed:
            self._broadcast({"type": "clear_finished", "removed": removed})
        return removed

    def remove_job(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None:
            return False
        # Persist deletion to SQLite FIRST
        if self._job_repo is not None:
            try:
                self._job_repo.delete(job_id)
            except Exception as exc:
                _log.warning("LinkMgr: delete failed for %s: %s", job_id, exc)
                return False
        # SQLite succeeded (or no repo) → safe to mutate memory state
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        self.jobs.pop(job_id, None)
        if job_id in self.order:
            self.order.remove(job_id)
        self._tasks.pop(job_id, None)
        self._broadcast({"type": "remove", "job_id": job_id})
        return True

    async def retry_job(self, job_id: str, *, region: str | None = None) -> bool:
        """Re-queue link job để chạy lại. Cho phép từ mọi status trừ 'running'.

        Nếu `region` truyền vào — override region của job (lưu lại snapshot mới).
        """
        job = self.jobs.get(job_id)
        if job is None:
            return False
        if job.status == "running":
            return False
        # Resolve region (nếu user override)
        if region is not None:
            from ..payment_link import REGION_BILLING
            r = (region or "").strip().upper()
            if r not in REGION_BILLING:
                raise ValueError(f"invalid region: {region}")
            job.region = r
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        # Cancel pending delayed-requeue helper để tránh duplicate enqueue.
        pending = self._delayed_requeue_tasks.pop(job_id, None)
        if pending is not None and not pending.done():
            pending.cancel()
        # Persist: clear payment_link, update region, status=queued.
        # Cần update combo JSON để region snapshot mới được persist khi recovery.
        if self._job_repo is not None:
            try:
                repo = self._job_repo
                jid = job.id
                new_combo = _serialize_link_combo(job)

                def _persist():
                    with repo.engine.transaction() as conn:
                        # Clear status fields giống update_status('queued'), kèm combo JSON mới + region.
                        conn.execute(
                            """UPDATE jobs
                               SET status = 'queued',
                                   started_at = NULL,
                                   finished_at = NULL,
                                   error = NULL,
                                   payment_link = NULL,
                                   region = ?,
                                   combo = ?
                               WHERE id = ?""",
                            (job.region, new_combo, jid),
                        )
                await repo.engine.run_sync(_persist)
            except Exception as exc:
                _log.warning("LinkMgr: retry persist failed for %s: %s", job_id, exc)
                return False
        job.status = "queued"
        job.error = None
        job.payment_link = None
        job.started_at = None
        job.finished_at = None
        retry_line = f"[{datetime.now():%H:%M:%S}] -- retry (region={job.region}) --"
        if self._job_repo is not None:
            try:
                self._job_repo.append_log(job.id, retry_line)
            except Exception as exc:
                _log.warning("LinkMgr: SQLite append_log (retry marker) failed for %s: %s", job.id, exc)
        job.log_lines.append(retry_line)
        self._broadcast_job(job)
        self._broadcast({"type": "log", "job_id": job_id, "line": retry_line})
        self._ensure_workers()
        self._job_queue.put_nowait(job_id)
        return True

    def list_jobs(self) -> list[dict[str, Any]]:
        return [self.jobs[jid].to_dict() for jid in self.order if jid in self.jobs]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        return job.to_dict_full() if job else None

    def get_log(self, job_id: str) -> list[str]:
        job = self.jobs.get(job_id)
        return list(job.log_lines) if job else []

    def _begin_job_proxy(self, job: LinkJob, log) -> str | None:
        """Resolve proxy cho 1 job từ proxy pool (round_robin/random)."""
        proxy = _resolve_job_proxy()
        job._active_proxy = proxy  # type: ignore[attr-defined]
        if proxy:
            log(f"[proxy] using {_mask_proxy(proxy)}")
        return proxy

    def _note_proxy_failure(self, job: LinkJob, exc_or_msg) -> None:
        """Mark proxy chết nếu lỗi là network/proxy → loại khỏi vòng xoay."""
        proxy = getattr(job, "_active_proxy", None)
        if proxy and _is_proxy_network_error(exc_or_msg):
            if get_proxy_pool().mark_dead(proxy):
                self._job_log(
                    job, f"[proxy] {_mask_proxy(proxy)} lỗi network — loại khỏi pool"
                )

    async def _run_job(self, job: LinkJob) -> None:
        self._tasks[job.id] = asyncio.current_task()  # type: ignore[arg-type]
        try:
            if job.id not in self.jobs:
                return
            if not await self._persist_status(job, "running"):
                # Transient SQLite fail — requeue với bounded retry
                retries = self._persist_running_retries.get(job.id, 0) + 1
                if retries <= self._persist_running_max_retries:
                    self._persist_running_retries[job.id] = retries
                    _log.warning(
                        "LinkMgr: persist running failed for job %s — requeue (attempt %d/%d)",
                        job.id, retries, self._persist_running_max_retries,
                    )
                    await asyncio.sleep(min(retries * 2.0, 6.0))
                    self._job_queue.put_nowait(job.id)
                else:
                    # Re-enqueue so job isn't permanently stuck in dead state.
                    # Job stays queued in both memory and DB (Req 10.3/10.4 respected).
                    _log.critical(
                        "LinkMgr: persist running failed for job %s after %d retries — "
                        "re-enqueueing with extended delay (SQLite may recover)",
                        job.id, retries,
                    )
                    self._persist_running_retries.pop(job.id, None)
                    # Detached task — không bị wait_for(job_timeout) cancel khi sleep.
                    self._schedule_delayed_requeue(job.id, 30.0)
                return
            self._persist_running_retries.pop(job.id, None)
            job.status = "running"
            job.started_at = time.time()
            self._broadcast_job(job)

            def log(msg: str) -> None:
                self._job_log(job, msg)

            job_proxy = self._begin_job_proxy(job, log)

            # ── Resolve access_token theo mode ──
            access_token: str | None = None

            if job.mode == "combo":
                # Login via browser or pure_request → obtain token
                log("[login] starting")
                from ..config import env_insecure_tls
                keep_browser_open = self._debug and not self._headless
                # Debug + headed → bỏ timeout để user soi browser tới khi cancel
                timeout = None if keep_browser_open else self._job_timeout
                try:
                    if job.reg_mode == "pure_request":
                        from ..session_phase import get_session_pure_request
                        session_data = await asyncio.wait_for(
                            get_session_pure_request(
                                email=job.email,
                                password=job.password,
                                secret=job.secret,
                                proxy=job_proxy,
                                log=log,
                            ),
                            timeout=self._job_timeout,
                        )
                    else:
                        session_data = await asyncio.wait_for(
                            get_session(
                                email=job.email,
                                password=job.password,
                                secret=job.secret,
                                headless=self._headless,
                                proxy=job_proxy,
                                tls_insecure=env_insecure_tls(),
                                keep_browser_open=keep_browser_open,
                                log=log,
                            ),
                            timeout=timeout,
                        )
                except asyncio.TimeoutError:
                    error_msg = f"timeout {self._job_timeout:.0f}s exceeded (login phase)"
                    if await self._persist_status(job, "error", error=error_msg):
                        job.status = "error"
                        job.error = error_msg
                        job.finished_at = time.time()
                        self._job_log(job, f"[fatal] timeout {self._job_timeout:.0f}s")
                        self._broadcast_job(job)
                    return
                except SessionError as exc:
                    error_msg = f"login: {exc}"
                    self._note_proxy_failure(job, exc)
                    if await self._persist_status(job, "error", error=error_msg):
                        job.status = "error"
                        job.error = error_msg
                        job.finished_at = time.time()
                        self._job_log(job, f"[login] failed: {exc}")
                        self._broadcast_job(job)
                    return

                access_token = session_data.get("accessToken") if session_data else None
                if not access_token:
                    error_msg = "login: missing accessToken in session"
                    if await self._persist_status(job, "error", error=error_msg):
                        job.status = "error"
                        job.error = error_msg
                        job.finished_at = time.time()
                        self._job_log(job, "[login] failed: no accessToken in response")
                        self._broadcast_job(job)
                    return
                log("[login] success")

            elif job.mode in ("session_json", "access_token"):
                # Token đã được parse sẵn
                access_token = job._access_token
                if not access_token:
                    error_msg = "no access_token provided"
                    if await self._persist_status(job, "error", error=error_msg):
                        job.status = "error"
                        job.error = error_msg
                        job.finished_at = time.time()
                        self._job_log(job, "[token] missing — nothing to do")
                        self._broadcast_job(job)
                    return
                log(f"[token] using pre-provided token (mode={job.mode})")

            # ── Get payment link ──
            log("[link] fetching payment URL")

            url: str | None = None
            last_link_exc: Exception | None = None
            log(f"[link] region={job.region}")
            # Cắt timeout 60→30s, attempts 3→2, sleep 3→1.5s.
            # checkout API + Stripe init + host replace thực tế <10s khi mạng OK.
            # 30s đủ cover chậm 1 chút mà không kéo loop quá lâu.
            link_t0 = time.monotonic()
            for link_attempt in range(1, 3):
                try:
                    url = await asyncio.wait_for(
                        get_checkout_url(access_token, proxy=job_proxy, region=job.region),
                        timeout=30.0,
                    )
                    break
                except asyncio.TimeoutError:
                    last_link_exc = TimeoutError("timeout 30s")
                    log(f"[link] attempt {link_attempt}/2 timeout 30s")
                except PaymentLinkError as exc:
                    last_link_exc = exc
                    # 401 = token expired — retry vô nghĩa
                    from ..payment_link import SessionExpiredError, CloudflareBlockedError
                    if isinstance(exc, SessionExpiredError):
                        log(f"[link] session expired — no retry: {exc}")
                        break
                    if isinstance(exc, CloudflareBlockedError):
                        # CF block sẽ tiếp tục block trong vài giây — retry liền vô ích.
                        log(f"[link] cloudflare block — no retry: {exc}")
                        break
                    log(f"[link] attempt {link_attempt}/2 failed: {exc}")

                if link_attempt < 2:
                    await asyncio.sleep(1.5)
            link_elapsed = time.monotonic() - link_t0

            if url is None:
                error_msg = f"payment_link: {last_link_exc}" if last_link_exc else "payment_link: unknown error"
                if await self._persist_status(job, "error", error=error_msg):
                    job.status = "error"
                    job.error = error_msg
                    job.finished_at = time.time()
                    self._job_log(job, f"[link] failed after 2 attempts ({link_elapsed:.1f}s): {last_link_exc}")
                    self._broadcast_job(job)
                return

            if await self._persist_status(job, "success", payment_link=url):
                job.payment_link = url
                job.status = "success"
                job.finished_at = time.time()
                log(f"[link] success: {url}")
                self._broadcast_job(job)
                # Append to aggregated links.txt
                try:
                    settings = load_settings()
                    _append_link_log(
                        session_dir=runtime_session_dir(settings),
                        payment_link=url,
                    )
                except Exception:
                    pass

        except asyncio.CancelledError:
            if not self._shutting_down and job.id in self.jobs:
                if await self._persist_status(job, "cancelled"):
                    job.status = "cancelled"
                    job.finished_at = time.time()
                    self._broadcast_job(job)
            raise
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            if await self._persist_status(job, "error", error=error_msg):
                job.status = "error"
                job.error = error_msg
                job.finished_at = time.time()
                self._job_log(job, f"[fatal] {error_msg}")
                self._broadcast_job(job)
        finally:
            self._tasks.pop(job.id, None)
            if job.status == "error" and job.id in self.jobs:
                await self._maybe_auto_retry(job)


# Singleton
_link_manager: LinkJobManager | None = None


def get_link_manager(job_repo: "JobRepository | None" = None) -> LinkJobManager:
    global _link_manager
    if _link_manager is None:
        _link_manager = LinkJobManager(max_concurrent=1, job_repo=job_repo)
        # Recovery: load persisted link jobs
        if job_repo is not None:
            try:
                recovered = job_repo.recover_interrupted()
                completed = job_repo.list_completed()
                all_rows = [
                    (row, False) for row in completed if row.get("job_type") == "link"
                ] + [
                    (row, True) for row in recovered if row.get("job_type") == "link"
                ]
                all_rows.sort(key=lambda t: t[0]["created_at"])
                for row, is_recovered in all_rows:
                    mode, password, secret, access_token, region = _restore_link_fields(row)
                    job = LinkJob(
                        id=row["id"],
                        email=row["email"],
                        password=password,
                        secret=secret,
                        mode=mode,
                        region=region,
                        _access_token=access_token,
                        status="queued" if is_recovered else row["status"],
                        error=row.get("error"),
                        payment_link=row.get("payment_link"),
                        created_at=row["created_at"],
                        started_at=row.get("started_at"),
                        finished_at=row.get("finished_at"),
                    )
                    try:
                        log_rows = job_repo.get_logs(row["id"])
                        job.log_lines = [lr["line"] for lr in log_rows]
                    except Exception:
                        pass
                    _link_manager.jobs[job.id] = job
                    _link_manager.order.append(job.id)
                    if is_recovered:
                        _link_manager._job_queue.put_nowait(job.id)
                if all_rows:
                    _link_manager._ensure_workers()
            except Exception as exc:
                _log.warning("LinkMgr recovery failed: %s", exc)
    return _link_manager


# ─────────────────────────────────────────────────────────────────────
# UpiJobManager — Get UPI QR feature
# ─────────────────────────────────────────────────────────────────────
#
# Pattern clone từ SessionJobManager nhưng:
#   - Worker chạy upi_runner.run_upi_qr_probe (login + checkout + confirm + approve loop).
#   - Hardcoded: PROMO, PROXY_FROM_STEP, DO_CONFIRM, DO_APPROVE, APPROVE_DELAY,
#     APPROVE_PROXY_BATCH, APPROVE_BACKEND_EXCEPTION_CONSECUTIVE, CONFIRM_VARIANTS.
#   - Configurable: max_concurrent, job_timeout, approve_retries,
#     restart_threshold, max_restarts.
#   - Multi-mode → KHÔNG stagger giữa start (yêu cầu UI: "multi thì chạy luôn ko cần delay").
#   - In-memory only (không persist DB) — UPI jobs ngắn hạn, user chạy lại được.

from .upi_runner import UpiQrError, UpiQrResult, run_upi_qr_probe  # noqa: E402

_UPI_QR_DIR = Path(__file__).resolve().parents[1] / "runtime" / "upi_qr"


def _extract_plan_from_session(session_data: dict[str, Any] | None) -> str | None:
    """Lấy plan type từ /api/auth/session JSON.

    ChatGPT trả plan ở 1 trong 2 vị trí (varies theo NextAuth version):
        - top-level: ``accountPlan`` (e.g. "free", "plus")
        - nested:    ``account.planType`` (e.g. "free", "plus", "team")

    Trả str non-empty (lowercase-ed) hoặc None nếu cả 2 đều thiếu.
    """
    if not isinstance(session_data, dict):
        return None
    # 1. top-level accountPlan
    top = session_data.get("accountPlan")
    if isinstance(top, str) and top.strip():
        return top.strip().lower()
    # 2. account.planType
    account = session_data.get("account")
    if isinstance(account, dict):
        pt = account.get("planType")
        if isinstance(pt, str) and pt.strip():
            return pt.strip().lower()
    return None
_DEFAULT_UPI_JOB_TIMEOUT = 1800.0  # 30 phút — đủ cho 500 retries × 3s + buffer
_DEFAULT_UPI_APPROVE_RETRIES = 500
_DEFAULT_UPI_RESTART_THRESHOLD = 30  # consec exception trước khi restart checkout
_DEFAULT_UPI_MAX_RESTARTS = 3        # số lần restart tối đa / job
_DEFAULT_UPI_PROXY_FROM_STEP = 3     # giữ default cũ — step 1-2 DIRECT, 3-6 via proxy


@dataclass
class UpiJob:
    id: str
    email: str
    password: str
    secret: str | None = None
    status: JobStatus = "queued"
    log_lines: list[str] = field(default_factory=list)
    error: str | None = None
    qr_path: str | None = None
    qr_source: str | None = None
    qr_reason: str | None = None
    qr_expires_at: int | None = None
    amount: int = 0
    return_url: str | None = None
    checkout_session: str | None = None
    has_upi_uri: bool = False
    has_qr_image_url: bool = False
    backend_exception_count: int = 0
    confirm_attempts: list[dict[str, Any]] = field(default_factory=list)
    approve_attempts: list[dict[str, Any]] = field(default_factory=list)
    page_refresh_attempts: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    # Auth artifacts giữ in-memory để endpoint check-session gọi /api/auth/session
    # khi user request (sau khi QR hết hạn). KHÔNG serialize ra to_dict() —
    # tránh leak credentials qua SSE/snapshot. Chỉ tồn tại trong RAM, mất khi
    # restart server (UpiJob vốn không persist DB).
    _access_token: str | None = field(default=None, repr=False)
    _session_cookies: list[dict[str, Any]] | None = field(default=None, repr=False)
    # Cache kết quả check-session gần nhất để frontend không phải re-fetch khi
    # user mở lại UI / re-render. None = chưa check bao giờ.
    plan_check: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "status": self.status,
            "error": self.error,
            "amount": self.amount,
            "return_url": self.return_url,
            "checkout_session": self.checkout_session,
            "has_qr": bool(self.qr_path),
            "qr_source": self.qr_source,
            "qr_reason": self.qr_reason,
            "qr_expires_at": self.qr_expires_at,
            "has_upi_uri": self.has_upi_uri,
            "has_qr_image_url": self.has_qr_image_url,
            "backend_exception_count": self.backend_exception_count,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "plan_check": self.plan_check,
            "can_check_plan": bool(self._session_cookies),
            "duration": (
                (self.finished_at or time.time()) - self.started_at if self.started_at else None
            ),
            "log_count": len(self.log_lines),
        }

    def to_dict_full(self) -> dict[str, Any]:
        d = self.to_dict()
        d["log_lines"] = list(self.log_lines)
        d["confirm_attempts"] = list(self.confirm_attempts)
        d["approve_attempts"] = list(self.approve_attempts)
        d["page_refresh_attempts"] = list(self.page_refresh_attempts)
        d["qr_path"] = self.qr_path
        return d


class UpiJobManager:
    """Quản lý UPI QR jobs.

    KHÔNG persist DB (in-memory only — lifecycle ngắn, user chạy lại nếu mất).
    Multi-mode = không stagger giữa job start (theo yêu cầu UI).
    """

    def __init__(self, *, max_concurrent: int = 1):
        self.jobs: dict[str, UpiJob] = {}
        self.order: list[str] = []
        self._max = max_concurrent
        self._job_timeout = _DEFAULT_UPI_JOB_TIMEOUT
        self._approve_retries = _DEFAULT_UPI_APPROVE_RETRIES
        self._restart_threshold = _DEFAULT_UPI_RESTART_THRESHOLD
        self._max_restarts = _DEFAULT_UPI_MAX_RESTARTS
        self._proxy_from_step = _DEFAULT_UPI_PROXY_FROM_STEP
        self._tasks: dict[str, asyncio.Task] = {}
        self._job_queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._worker_started = False
        self._shutting_down: bool = False

    # ── Properties ──────────────────────────────────────────────────────
    @property
    def max_concurrent(self) -> int:
        return self._max

    def set_max_concurrent(self, n: int) -> None:
        if n < 1 or n > 50:
            raise ValueError("max_concurrent phải trong [1, 50]")
        self._max = n
        self._ensure_workers()

    @property
    def job_timeout(self) -> float:
        return self._job_timeout

    def set_job_timeout(self, seconds: float) -> None:
        if seconds < 60 or seconds > 7200:
            raise ValueError("job_timeout phải trong [60, 7200]")
        self._job_timeout = float(seconds)

    @property
    def approve_retries(self) -> int:
        return self._approve_retries

    def set_approve_retries(self, n: int) -> None:
        if n < 1 or n > 2000:
            raise ValueError("approve_retries phải trong [1, 2000]")
        self._approve_retries = n

    @property
    def restart_threshold(self) -> int:
        return self._restart_threshold

    def set_restart_threshold(self, n: int) -> None:
        if n < 0 or n > 1000:
            raise ValueError("restart_threshold phải trong [0, 1000]")
        self._restart_threshold = n

    @property
    def max_restarts(self) -> int:
        return self._max_restarts

    def set_max_restarts(self, n: int) -> None:
        if n < 0 or n > 100:
            raise ValueError("max_restarts phải trong [0, 100]")
        self._max_restarts = n

    @property
    def proxy_from_step(self) -> int:
        return self._proxy_from_step

    def set_proxy_from_step(self, n: int) -> None:
        if n < 1 or n > 6:
            raise ValueError("proxy_from_step phải trong [1, 6]")
        self._proxy_from_step = n

    def apply_settings(self, settings: dict) -> None:
        """Hydrate fields từ settings dict (startup boot)."""
        _hydrate_proxy_pool_from_settings(settings)
        if "upi.max_concurrent" in settings:
            val = int(settings["upi.max_concurrent"])
            if 1 <= val <= 50:
                self._max = val
        if "upi.job_timeout" in settings:
            val = float(settings["upi.job_timeout"])
            if 60 <= val <= 7200:
                self._job_timeout = val
        if "upi.approve_retries" in settings:
            val = int(settings["upi.approve_retries"])
            if 1 <= val <= 2000:
                self._approve_retries = val
        if "upi.approve.restart_threshold" in settings:
            val = int(settings["upi.approve.restart_threshold"])
            if 0 <= val <= 1000:
                self._restart_threshold = val
        if "upi.approve.max_restarts" in settings:
            val = int(settings["upi.approve.max_restarts"])
            if 0 <= val <= 100:
                self._max_restarts = val
        if "upi.proxy_from_step" in settings:
            val = int(settings["upi.proxy_from_step"])
            if 1 <= val <= 6:
                self._proxy_from_step = val

    # ── SSE broadcast ───────────────────────────────────────────────────
    def _broadcast(self, event: dict[str, Any]) -> None:
        if _sse_mux is not None:
            _sse_mux.publish("upi", event)

    def _broadcast_job(self, job: UpiJob) -> None:
        self._broadcast({"type": "job", "job": job.to_dict()})

    def _job_log(self, job: UpiJob, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        job.log_lines.append(line)
        if len(job.log_lines) > 2000:
            job.log_lines = job.log_lines[-2000:]
        self._broadcast({"type": "log", "job_id": job.id, "line": line})

    # ── Worker pool (no stagger) ───────────────────────────────────────
    def _ensure_workers(self) -> None:
        if not self._worker_started:
            self._worker_started = True
        self._workers = [t for t in self._workers if not t.done()]
        while len(self._workers) < self._max:
            task = asyncio.create_task(self._worker_loop())
            self._workers.append(task)
        while len(self._workers) > self._max:
            task = self._workers.pop()
            task.cancel()

    async def _worker_loop(self) -> None:
        try:
            while True:
                job_id = await self._job_queue.get()
                job = self.jobs.get(job_id)
                if job is None or job.status != "queued":
                    continue
                inner = asyncio.create_task(self._run_job(job))
                self._tasks[job_id] = inner
                try:
                    await inner
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelled():
                        raise
                    if inner.cancelled():
                        continue
                    raise
                finally:
                    self._tasks.pop(job_id, None)
        except asyncio.CancelledError:
            pass

    # ── Public CRUD ─────────────────────────────────────────────────────
    def add_jobs(self, combos: list[str]) -> list[UpiJob]:
        existing_emails = {j.email.lower() for j in self.jobs.values() if j.status != "cancelled"}
        out: list[UpiJob] = []
        for raw in combos:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 2:
                jid = uuid.uuid4().hex[:12]
                job = UpiJob(
                    id=jid, email="<invalid>", password="",
                    status="error",
                    error=f"format sai, cần email|password|secret: {line[:60]}",
                    finished_at=time.time(),
                )
                self.jobs[jid] = job
                self.order.append(jid)
                self._broadcast_job(job)
                out.append(job)
                continue
            email = parts[0]
            password = parts[1]
            secret = parts[2] if len(parts) >= 3 and parts[2] else None
            if "@" not in email:
                jid = uuid.uuid4().hex[:12]
                job = UpiJob(
                    id=jid, email=email or "<invalid>", password="",
                    status="error",
                    error=f"email không hợp lệ: {email!r}",
                    finished_at=time.time(),
                )
                self.jobs[jid] = job
                self.order.append(jid)
                self._broadcast_job(job)
                out.append(job)
                continue
            if email.lower() in existing_emails:
                continue
            existing_emails.add(email.lower())
            jid = uuid.uuid4().hex[:12]
            job = UpiJob(id=jid, email=email, password=password, secret=secret)
            self.jobs[jid] = job
            self.order.append(jid)
            self._broadcast_job(job)
            out.append(job)

        self._ensure_workers()
        for j in out:
            if j.status == "queued":
                self._job_queue.put_nowait(j.id)
        return out

    def list_jobs(self) -> list[dict[str, Any]]:
        return [self.jobs[jid].to_dict() for jid in self.order if jid in self.jobs]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        return job.to_dict_full() if job else None

    def get_log(self, job_id: str) -> list[str]:
        job = self.jobs.get(job_id)
        return list(job.log_lines) if job else []

    def get_qr_path(self, job_id: str) -> Path | None:
        job = self.jobs.get(job_id)
        if not job or not job.qr_path:
            return None
        path = Path(job.qr_path)
        return path if path.exists() else None

    async def stop_all(self) -> int:
        while not self._job_queue.empty():
            try:
                self._job_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        count = 0
        for job_id, job in list(self.jobs.items()):
            if job.status in ("running", "queued"):
                task = self._tasks.get(job_id)
                if task and not task.done():
                    task.cancel()
                job.status = "cancelled"
                job.finished_at = time.time()
                self._broadcast_job(job)
                count += 1
        return count

    def shutdown(self) -> None:
        self._shutting_down = True
        for w in self._workers:
            if not w.done():
                w.cancel()
        self._workers.clear()

    def clear_finished(self) -> int:
        removed = 0
        for jid in list(self.order):
            job = self.jobs.get(jid)
            if job and job.status in ("success", "error"):
                # Cleanup QR file để không tích tụ.
                if job.qr_path:
                    try:
                        Path(job.qr_path).unlink(missing_ok=True)
                    except OSError as exc:
                        _log.warning("UpiMgr: unlink QR %s failed: %s", job.qr_path, exc)
                self.jobs.pop(jid, None)
                self.order.remove(jid)
                self._tasks.pop(jid, None)
                removed += 1
        if removed:
            self._broadcast({"type": "clear_finished", "removed": removed})
        return removed

    def remove_job(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None:
            return False
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        if job.qr_path:
            try:
                Path(job.qr_path).unlink(missing_ok=True)
            except OSError as exc:
                _log.warning("UpiMgr: unlink QR %s failed: %s", job.qr_path, exc)
        self.jobs.pop(job_id, None)
        if job_id in self.order:
            self.order.remove(job_id)
        self._tasks.pop(job_id, None)
        self._broadcast({"type": "remove", "job_id": job_id})
        return True

    async def check_plan(self, job_id: str) -> dict[str, Any]:
        """Gọi /api/auth/session bằng cookies đã lưu để biết plan hiện tại.

        Dùng khi QR đã hết hạn — user muốn xác nhận giao dịch UPI có pump
        account lên Plus chưa. Cookies được lưu vào job._session_cookies sau
        khi step 1 (login) thành công, KHÔNG persist DB nên mất khi restart
        server.

        Trả dict với keys:
            ok: bool
            plan: str | None     (e.g. "plus", "free", "team")
            is_plus: bool        (heuristic: plan chứa "plus")
            expires: str | None  (ISO timestamp từ session.expires)
            checked_at: int      (unix seconds)
            error: str | None    (chỉ khi ok=False)

        Không raise — fail-soft, lưu error message trong dict để frontend hiển
        thị bên cạnh badge HẾT HẠN. Cache vào job.plan_check để các request
        sau (re-render UI) không gọi lại.
        """
        job = self.jobs.get(job_id)
        if job is None:
            return {
                "ok": False,
                "plan": None,
                "is_plus": False,
                "expires": None,
                "checked_at": int(time.time()),
                "error": "job không tồn tại",
            }

        if not job._session_cookies:
            result = {
                "ok": False,
                "plan": None,
                "is_plus": False,
                "expires": None,
                "checked_at": int(time.time()),
                "error": "không có session cookies (job chưa login thành công hoặc server đã restart)",
            }
            job.plan_check = result
            self._broadcast_job(job)
            return result

        from ..session_phase import fetch_session_via_http, SessionError

        try:
            data = await fetch_session_via_http(
                cookies=job._session_cookies,
                proxy=None,  # cookies-based auth không kén IP
                timeout=20.0,
            )
        except SessionError as exc:
            self._job_log(job, f"[check-plan] session fail: {exc}")
            result = {
                "ok": False,
                "plan": None,
                "is_plus": False,
                "expires": None,
                "checked_at": int(time.time()),
                "error": f"session error: {exc}",
            }
            job.plan_check = result
            self._broadcast_job(job)
            return result
        except Exception as exc:  # noqa: BLE001
            self._job_log(job, f"[check-plan] unexpected: {exc}")
            result = {
                "ok": False,
                "plan": None,
                "is_plus": False,
                "expires": None,
                "checked_at": int(time.time()),
                "error": f"unexpected: {exc}",
            }
            job.plan_check = result
            self._broadcast_job(job)
            return result

        plan = _extract_plan_from_session(data)
        is_plus = bool(plan and "plus" in plan.lower())
        expires_raw = data.get("expires") if isinstance(data, dict) else None
        expires_str = expires_raw if isinstance(expires_raw, str) else None
        result = {
            "ok": True,
            "plan": plan,
            "is_plus": is_plus,
            "expires": expires_str,
            "checked_at": int(time.time()),
            "error": None,
        }
        self._job_log(job, f"[check-plan] plan={plan or '?'} is_plus={is_plus}")
        job.plan_check = result
        self._broadcast_job(job)
        return result

    async def retry_job(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None:
            return False
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        # Clean QR cũ.
        if job.qr_path:
            try:
                Path(job.qr_path).unlink(missing_ok=True)
            except OSError:
                pass
        job.status = "queued"
        job.error = None
        job.qr_path = None
        job.qr_source = None
        job.qr_reason = None
        job.qr_expires_at = None
        job.amount = 0
        job.return_url = None
        job.checkout_session = None
        job.has_upi_uri = False
        job.has_qr_image_url = False
        job.backend_exception_count = 0
        job.confirm_attempts = []
        job.approve_attempts = []
        job.page_refresh_attempts = []
        job.started_at = None
        job.finished_at = None
        # Reset auth artifacts + plan check — cookies/token cũ không còn ý nghĩa
        # cho run mới (sẽ lấy lại từ login mới).
        job._access_token = None
        job._session_cookies = None
        job.plan_check = None
        retry_line = f"[{datetime.now():%H:%M:%S}] -- retry --"
        job.log_lines.append(retry_line)
        self._broadcast_job(job)
        self._broadcast({"type": "log", "job_id": job_id, "line": retry_line})
        self._ensure_workers()
        self._job_queue.put_nowait(job_id)
        return True

    # ── Telegram notify ─────────────────────────────────────────────────
    async def _notify_telegram(self, job: UpiJob) -> None:
        """Gửi QR + combo qua Telegram (best-effort). Không raise ra ngoài.

        Log mọi nhánh skip/fail vào job log để dễ debug khi user "không thấy
        tin về" — tránh silent fallback.
        """
        from .telegram_notifier import TelegramNotifyError, get_telegram_notifier

        notifier = get_telegram_notifier()
        if not notifier.enabled:
            self._job_log(job, "[tg]   skip            —  toggle 'Gửi Telegram' đang tắt")
            return
        if not notifier.configured:
            self._job_log(job, "[tg]   skip            —  bot_token/chat_id chưa cấu hình (Settings → Telegram)")
            return
        try:
            sent = await notifier.notify_upi_qr(
                email=job.email,
                password=job.password,
                secret=job.secret,
                amount=job.amount,
                qr_path=job.qr_path,
                qr_expires_at=job.qr_expires_at,
                checkout_session=job.checkout_session,
                return_url=job.return_url,
                log=lambda msg: self._job_log(job, msg),
            )
            if sent:
                self._broadcast_job(job)
        except TelegramNotifyError as exc:
            self._job_log(job, f"[tg]   send            ✗  {exc}")
        except Exception as exc:  # noqa: BLE001
            self._job_log(job, f"[tg]   send            ✗  {type(exc).__name__}: {exc}")

    # ── Run job ─────────────────────────────────────────────────────────
    async def _run_job(self, job: UpiJob) -> None:
        self._tasks[job.id] = asyncio.current_task()  # type: ignore[arg-type]
        try:
            if job.id not in self.jobs:
                return
            job.status = "running"
            job.started_at = time.time()
            self._broadcast_job(job)

            def log(msg: str) -> None:
                self._job_log(job, msg)

            # Lấy proxy_pool từ singleton (hot-load — user vừa cập nhật là dùng ngay).
            proxy_pool = list(get_proxy_pool().live_entries())

            _UPI_QR_DIR.mkdir(parents=True, exist_ok=True)
            qr_out_path = _UPI_QR_DIR / f"{job.id}.png"

            result: UpiQrResult = await asyncio.wait_for(
                run_upi_qr_probe(
                    email=job.email,
                    password=job.password,
                    secret=job.secret,
                    proxy_pool=proxy_pool,
                    approve_retries=self._approve_retries,
                    qr_out_path=qr_out_path,
                    log=log,
                    restart_threshold=self._restart_threshold,
                    max_restarts=self._max_restarts,
                    proxy_from_step=self._proxy_from_step,
                ),
                timeout=self._job_timeout,
            )

            # Apply result vào job state.
            job.amount = result.amount
            job.return_url = result.return_url or None
            job.checkout_session = result.checkout_session or None
            job.qr_path = result.qr_path
            job.qr_source = result.qr_source
            job.qr_reason = result.qr_reason
            job.qr_expires_at = result.qr_expires_at
            job.has_upi_uri = result.has_upi_uri
            job.has_qr_image_url = result.has_qr_image_url
            job.backend_exception_count = result.backend_exception_count
            job.confirm_attempts = result.confirm_attempts
            job.approve_attempts = result.approve_attempts
            job.page_refresh_attempts = result.page_refresh_attempts
            # Lưu auth artifacts (chỉ khi login OK — result luôn có khi tới
            # đây). Ngay cả khi result.ok=False vì approve fail, login đã pass
            # nên cookies + token vẫn dùng để check plan.
            job._access_token = result.access_token
            job._session_cookies = result.session_cookies
            # Reset plan_check cũ (có thể còn từ retry trước).
            job.plan_check = None

            if result.ok:
                job.status = "success"
                job.error = None
            else:
                job.status = "error"
                job.error = result.error or "unknown error"
            job.finished_at = time.time()
            self._broadcast_job(job)

            # Telegram notify (best-effort) — chỉ khi success + có QR. Không
            # break job nếu gửi fail; log vào job để user thấy.
            if result.ok and job.qr_path:
                await self._notify_telegram(job)

        except asyncio.TimeoutError:
            error_msg = f"timeout {self._job_timeout:.0f}s exceeded"
            job.status = "error"
            job.error = error_msg
            job.finished_at = time.time()
            self._job_log(job, f"[fatal] {error_msg}")
            self._broadcast_job(job)
        except asyncio.CancelledError:
            if not self._shutting_down and job.id in self.jobs:
                job.status = "cancelled"
                job.finished_at = time.time()
                self._broadcast_job(job)
            raise
        except UpiQrError as exc:
            job.status = "error"
            job.error = str(exc)
            job.finished_at = time.time()
            self._job_log(job, f"[error] {exc}")
            self._broadcast_job(job)
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            # Mark proxy chết nếu phù hợp.
            if _is_proxy_network_error(exc):
                pool = get_proxy_pool()
                # Best-effort — runner đã ưu tiên proxy[0] khi cần.
                for p in list(pool.live_entries())[:1]:
                    if pool.mark_dead(p):
                        self._job_log(job, f"[proxy] {_mask_proxy(p)} lỗi network — loại khỏi pool")
            job.status = "error"
            job.error = error_msg
            job.finished_at = time.time()
            self._job_log(job, f"[fatal] {error_msg}")
            self._broadcast_job(job)
        finally:
            self._tasks.pop(job.id, None)


# Singleton
_upi_manager: UpiJobManager | None = None


def get_upi_manager() -> UpiJobManager:
    global _upi_manager
    if _upi_manager is None:
        _upi_manager = UpiJobManager(max_concurrent=1)
    return _upi_manager
