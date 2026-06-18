"""AutoRegRunner — async runner tự động poll icloud_emails + signup ChatGPT.

Pattern mirrors HmeRunner:
- Module-level lazy singleton (khởi tạo trong icloud_routes.py)
- start/stop lifecycle với cancel_event
- LogCallback cho SSE bridging
- Stats tracking (monotonically non-decreasing)
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from db.repositories import ChatGptAccountRepository

_log = logging.getLogger(__name__)

# Async callback: (level, message, payload) -> Awaitable[None]
LogCallback = Callable[[str, str, dict[str, Any]], Awaitable[None]]


@dataclass
class AutoRegConfig:
    """Runtime config cho AutoRegRunner, build từ API request body."""

    concurrency: int = 1
    poll_interval: int = 30
    default_password: str = ""
    logs_url: str = ""
    api_key: str = ""
    # Shared config từ Settings store (reg.* namespace) — inject tại start time
    headless: bool = True
    job_timeout: float = 240.0
    auto_retry: bool = False
    auto_retry_max: int = 3
    auto_retry_delay: float = 30.0


@dataclass
class AutoRegStats:
    """Cumulative stats, monotonically non-decreasing."""

    processed: int = 0
    success: int = 0
    errors: int = 0


class AutoRegRunner:
    """Async runner tự động poll icloud_emails + signup ChatGPT.

    Lifecycle:
        start(config) → spawns poll loop task
        stop() → sets cancel_event, non-blocking
        finally block reset _running=False
    """

    def __init__(
        self,
        *,
        log_callback: LogCallback,
        account_repo: "ChatGptAccountRepository",
    ) -> None:
        self._log_cb = log_callback
        self._account_repo = account_repo
        self._running: bool = False
        self._cancel_event: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._stats = AutoRegStats()
        self._current_cycle: int = 0
        self._config: AutoRegConfig | None = None

    # ── Read-only properties ─────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def stats(self) -> AutoRegStats:
        return self._stats

    @property
    def current_cycle(self) -> int:
        return self._current_cycle

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start(self, config: AutoRegConfig) -> None:
        """Start poll loop. Raises RuntimeError if already running."""
        if self._running:
            raise RuntimeError("AutoRegRunner is already running")

        self._config = config
        self._cancel_event = asyncio.Event()
        self._running = True
        self._current_cycle = 0

        self._task = asyncio.create_task(self._poll_loop())

    def stop(self) -> None:
        """Signal graceful stop via cancel_event. Non-blocking.

        Also cancels the main task directly to force-break workers
        that are stuck in long-running operations (browser automation).
        """
        if self._cancel_event is not None:
            self._cancel_event.set()
        # Force-cancel the task if it's still alive after setting the event
        if self._task is not None and not self._task.done():
            self._task.cancel()

    # ── Internal: poll loop ──────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Main loop: poll → process batch → sleep → repeat.

        Optimization: skip sleep khi batch đầy (còn email chờ xử lý).
        Pipeline pattern: dùng asyncio.Queue + persistent workers thay vì
        batch-then-sleep — tối đa throughput, không lãng phí thời gian idle.
        """
        workers: list[asyncio.Task] = []
        try:
            await self._log("info", "AutoReg started", {
                "concurrency": self._config.concurrency,
                "poll_interval": self._config.poll_interval,
            })

            # Pipeline: queue + persistent worker tasks
            queue: asyncio.Queue[str] = asyncio.Queue()

            async def _worker(worker_id: int) -> None:
                """Persistent worker — pick email từ queue, process, lặp lại."""
                while not self._cancel_event.is_set():
                    try:
                        email = await asyncio.wait_for(
                            queue.get(), timeout=2.0
                        )
                    except asyncio.TimeoutError:
                        continue
                    try:
                        await self._process_email(email)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        _log.warning("Worker %d unhandled: %s", worker_id, exc)
                    finally:
                        queue.task_done()

            # Spawn persistent workers (concurrency cố định)
            for i in range(self._config.concurrency):
                workers.append(asyncio.create_task(_worker(i)))

            while not self._cancel_event.is_set():
                self._current_cycle += 1

                # Query emails với status='created'
                batch_limit = self._config.concurrency * 2
                try:
                    emails = await asyncio.to_thread(
                        self._account_repo.get_created_emails,
                        batch_limit,
                    )
                except Exception as exc:
                    await self._log("error", f"DB query failed: {exc}", {})
                    if await self._interruptible_sleep(self._config.poll_interval):
                        break
                    continue

                if not emails:
                    await self._log(
                        "info",
                        f"Cycle #{self._current_cycle}: no emails available, sleeping {self._config.poll_interval}s",
                        {"cycle": self._current_cycle},
                    )
                    if await self._interruptible_sleep(self._config.poll_interval):
                        break
                    continue

                await self._log(
                    "info",
                    f"Cycle #{self._current_cycle}: enqueue {len(emails)} email(s)",
                    {"cycle": self._current_cycle, "count": len(emails)},
                )

                # Đẩy vào queue — workers tự pick
                for email in emails:
                    if self._cancel_event.is_set():
                        break
                    await queue.put(email)

                # Đợi queue drain (tất cả email trong batch được xử lý)
                # Nhưng interruptible bởi cancel
                drain_task = asyncio.create_task(queue.join())
                cancel_task = asyncio.create_task(self._cancel_event.wait())
                done, pending = await asyncio.wait(
                    {drain_task, cancel_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                if cancel_task in done:
                    break

                # Optimization: nếu batch đầy → còn email → query ngay, không sleep
                if len(emails) >= batch_limit:
                    continue

                # Batch không đầy → hết email hoặc gần hết → sleep rồi poll tiếp
                if await self._interruptible_sleep(self._config.poll_interval):
                    break

            # Cleanup workers
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

        except asyncio.CancelledError:
            # Force-cancel workers when task itself is cancelled
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        except Exception as exc:
            await self._log("error", f"Poll loop crashed: {type(exc).__name__}: {exc}", {})
            _log.exception("AutoRegRunner poll loop crashed")
            # Cleanup workers on crash too
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        finally:
            self._running = False
            self._task = None
            try:
                await self._log("info", "AutoReg stopped", {
                    "stats": {
                        "processed": self._stats.processed,
                        "success": self._stats.success,
                        "errors": self._stats.errors,
                    },
                })
            except Exception:
                pass  # Don't let logging failure prevent cleanup

    # ── Internal: process single email ───────────────────────────────

    async def _process_email(self, email: str) -> None:
        """Single email signup: build request → run_signup → enable_2fa → persist.

        Respects reg config: proxy, headless, job_timeout, auto_retry.
        On final failure: update icloud_emails status to 'disabled' to prevent infinite retry.
        """
        from mfa_phase import MfaError, enable_2fa
        from models import SignupRequest, SignupResult
        from signup import run_signup
        from web.mail_modes import get_spec

        self._stats.processed += 1
        max_attempts = (self._config.auto_retry_max + 1) if self._config.auto_retry else 1

        for attempt in range(1, max_attempts + 1):
            if self._cancel_event.is_set():
                return

            try:
                prefix = f"[{email}]" if max_attempts == 1 else f"[{email}][attempt {attempt}/{max_attempts}]"
                await self._log("info", f"{prefix} Starting signup", {"email": email, "attempt": attempt})

                # Build SignupRequest via worker spec — inject proxy + headless từ Settings
                # Proxy lấy từ pool rotation (round_robin/random) để mỗi email
                # tránh trùng IP. Pool rỗng → None (direct).
                from web.manager import _resolve_job_proxy
                email_proxy = _resolve_job_proxy()
                worker_config = self._resolve_worker_config(self._config)
                spec = get_spec("worker")
                parsed = spec.parse_line(email)
                request = spec.build_request(
                    parsed,
                    worker_config=worker_config,
                    password=self._config.default_password or None,
                    headless=self._config.headless,
                    proxy=email_proxy,
                )

                # Run signup with job_timeout
                _bg_tasks: set[asyncio.Task] = set()

                def log_fn(msg: str) -> None:
                    coro = self._log("info", f"{prefix} {msg}", {"email": email})
                    try:
                        task = asyncio.create_task(coro)
                    except RuntimeError:
                        # Event loop closed — đóng coroutine để tránh RuntimeWarning
                        # "coroutine was never awaited".
                        coro.close()
                        return
                    _bg_tasks.add(task)
                    task.add_done_callback(_bg_tasks.discard)

                result: SignupResult = await asyncio.wait_for(
                    run_signup(request, log=log_fn),
                    timeout=self._config.job_timeout,
                )

                if not result.success:
                    error_msg = result.error or "signup failed"
                    # Retry?
                    if attempt < max_attempts:
                        delay = self._config.auto_retry_delay * attempt
                        await self._log("warn", f"{prefix} Failed: {error_msg} — retry in {delay:.0f}s", {
                            "email": email, "error": error_msg, "attempt": attempt,
                        })
                        if await self._interruptible_sleep(delay):
                            return  # cancelled
                        continue
                    # Final failure — mark email as disabled
                    self._stats.errors += 1
                    await self._log("error", f"{prefix} Signup failed (no more retries): {error_msg}", {
                        "email": email, "error": error_msg,
                    })
                    await self._mark_email_failed(email)
                    return

                # Signup succeeded — enable 2FA
                secret_2fa: str | None = None
                password = result.password or self._config.default_password

                if result.access_token:
                    await self._log("info", f"{prefix} Enabling 2FA...", {"email": email})
                    try:
                        mfa_result = await enable_2fa(
                            access_token=result.access_token,
                            cookies=result.cookies,
                            proxy=email_proxy,
                            log=log_fn,
                        )
                        secret_2fa = mfa_result.get("secret")
                        await self._log("info", f"{prefix} 2FA enabled", {
                            "email": email, "secret": secret_2fa,
                        })
                    except MfaError as exc:
                        await self._log("warn", f"{prefix} 2FA failed: {exc}", {
                            "email": email, "error": str(exc),
                        })
                else:
                    await self._log("warn", f"{prefix} No access_token, skipping 2FA", {
                        "email": email,
                    })

                # Persist success: INSERT chatgpt_accounts + UPDATE icloud_emails
                try:
                    await asyncio.to_thread(
                        self._account_repo.persist_success,
                        email,
                        password,
                        secret_2fa,
                    )
                except Exception as exc:
                    self._stats.errors += 1
                    await self._log("error", f"{prefix} DB persist failed: {exc}", {
                        "email": email, "error": str(exc),
                    })
                    return

                self._stats.success += 1
                await self._log("success", f"{email}|{password}|{secret_2fa or ''}", {
                    "email": email, "password": password, "secret_2fa": secret_2fa,
                })
                return  # done

            except asyncio.TimeoutError:
                error_msg = f"timeout {self._config.job_timeout:.0f}s exceeded"
                if attempt < max_attempts:
                    delay = self._config.auto_retry_delay * attempt
                    await self._log("warn", f"{prefix} {error_msg} — retry in {delay:.0f}s", {
                        "email": email, "error": error_msg, "attempt": attempt,
                    })
                    if await self._interruptible_sleep(delay):
                        return
                    continue
                self._stats.errors += 1
                await self._log("error", f"{prefix} {error_msg} (no more retries)", {
                    "email": email, "error": error_msg,
                })
                await self._mark_email_failed(email)
                return

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error_msg = f"{type(exc).__name__}: {exc}"
                if attempt < max_attempts:
                    delay = self._config.auto_retry_delay * attempt
                    await self._log("warn", f"{prefix} {error_msg} — retry in {delay:.0f}s", {
                        "email": email, "error": error_msg, "attempt": attempt,
                    })
                    if await self._interruptible_sleep(delay):
                        return
                    continue
                self._stats.errors += 1
                await self._log("error", f"{prefix} Unexpected error (no more retries): {error_msg}", {
                    "email": email, "error": error_msg,
                })
                await self._mark_email_failed(email)
                return

    async def _mark_email_failed(self, email: str) -> None:
        """Update icloud_emails status → 'disabled' để không bị pick lại.

        Dùng khi signup fail after max retries — email này không thể dùng nữa.
        """
        try:
            def _do_mark():
                with self._account_repo.engine.get_connection() as conn:
                    conn.execute(
                        "UPDATE icloud_emails SET status = 'disabled' "
                        "WHERE email = ? AND status = 'created'",
                        (email,),
                    )

            await asyncio.to_thread(_do_mark)
        except Exception as exc:
            _log.warning("Failed to mark email %s as disabled: %s", email, exc)

    # ── Internal: helpers ────────────────────────────────────────────

    async def _interruptible_sleep(self, seconds: float) -> bool:
        """Sleep interruptible by cancel_event.

        Returns True if interrupted (should break loop).
        """
        try:
            await asyncio.wait_for(self._cancel_event.wait(), timeout=seconds)
            return True  # cancelled
        except asyncio.TimeoutError:
            return False  # slept full duration

    @staticmethod
    def _resolve_worker_config(config: AutoRegConfig) -> dict[str, str]:
        """Resolve logs_url + api_key. Priority: UI input > env var > hardcoded default."""
        return {
            "logs_url": (
                config.logs_url
                or os.environ.get("HYBRID_WORKER_LOGS_URL", "")
                or "https://icloud-cf-mail.n5pskgzs9g.workers.dev/logs"
            ),
            "api_key": (
                config.api_key
                or os.environ.get("HYBRID_WORKER_API_KEY", "")
            ),
        }

    async def _log(self, level: str, message: str, payload: dict[str, Any]) -> None:
        """Dispatch log event qua callback. Best-effort, không raise."""
        try:
            await self._log_cb(level, message, payload)
        except Exception as exc:
            _log.warning("log_callback failed: %s", exc)
