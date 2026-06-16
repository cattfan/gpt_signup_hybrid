"""ProxyPool — xoay vòng nhiều proxy URL để tránh trùng IP khi chạy batch.

Single source of truth runtime cho danh sách proxy rotation. Được hydrate từ
Settings Store (`proxy.pool` + `proxy.rotation_mode`) lúc startup
(`JobManager.apply_settings`) và reconfigure khi user lưu qua endpoint
`POST /api/proxy/pool`.

Thiết kế:
- Một singleton dùng CHUNG cho cả 3 manager (reg / session / link) để rotation
  là toàn cục → 2 job song song không pick cùng 1 IP liên tiếp.
- ``pick()`` chọn proxy kế tiếp trong số proxy còn "live" theo mode
  (round_robin | random). Proxy đã bị ``mark_dead`` sẽ bị loại khỏi vòng xoay.
- Pool rỗng hoặc tất cả proxy đã chết → ``pick()`` trả None; caller chạy direct
  (không proxy). Đây là nguồn cấu hình proxy DUY NHẤT của hệ thống.

Concurrency: FastAPI chạy single event loop, các method mutate state đồng bộ
(không ``await`` giữa chừng) nên không cần lock.
"""
from __future__ import annotations

import random
import threading

_VALID_MODES: tuple[str, ...] = ("round_robin", "random")


def normalize_proxies(proxies) -> list[str]:
    """Strip + bỏ rỗng + dedupe (giữ thứ tự). Bỏ qua phần tử không phải str."""
    if not proxies:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in proxies:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


class ProxyPool:
    """Danh sách proxy xoay vòng + theo dõi proxy chết.

    Attributes (read qua property):
        entries: list proxy URL đã cấu hình (đã normalize).
        mode: "round_robin" | "random".
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[str] = []
        self._mode: str = "round_robin"
        self._dead: set[str] = set()
        self._cursor: int = 0

    # ── Config ──────────────────────────────────────────────────────────

    def configure(self, proxies, mode: str | None = None) -> None:
        """Cập nhật danh sách proxy + mode.

        - ``proxies=None`` → giữ nguyên danh sách hiện tại (chỉ đổi mode nếu có).
        - ``proxies=[...]`` → thay thế toàn bộ; dead-set giữ lại entry vẫn còn,
          loại entry đã bị gỡ.
        - ``mode`` không hợp lệ → giữ mode cũ.
        """
        with self._lock:
            if mode in _VALID_MODES:
                self._mode = mode
            if proxies is not None:
                normalized = normalize_proxies(proxies)
                present = set(normalized)
                self._entries = normalized
                # Giữ dead-mark cho proxy vẫn còn trong pool, xoá phần đã gỡ.
                self._dead = {d for d in self._dead if d in present}
                if self._cursor >= len(normalized):
                    self._cursor = 0

    def reset_dead(self) -> None:
        """Xoá toàn bộ đánh dấu chết — cho mọi proxy cơ hội chạy lại."""
        with self._lock:
            self._dead.clear()

    # ── Runtime selection ───────────────────────────────────────────────

    def pick(self) -> str | None:
        """Trả proxy kế tiếp trong số proxy còn live, hoặc None nếu không có.

        round_robin: xoay tuần tự qua danh sách live.
        random: chọn ngẫu nhiên trong danh sách live.
        """
        with self._lock:
            live = [p for p in self._entries if p not in self._dead]
            if not live:
                return None
            if self._mode == "random":
                return random.choice(live)
            url = live[self._cursor % len(live)]
            self._cursor += 1
            return url

    def mark_dead(self, url: str | None) -> bool:
        """Đánh dấu proxy chết → loại khỏi vòng xoay. True nếu vừa mới đánh dấu."""
        if not url:
            return False
        with self._lock:
            if url not in self._entries or url in self._dead:
                return False
            self._dead.add(url)
            return True

    def mark_alive(self, url: str | None) -> None:
        """Gỡ đánh dấu chết cho 1 proxy (vd sau khi test lại OK)."""
        if not url:
            return
        with self._lock:
            self._dead.discard(url)

    # ── Introspection ───────────────────────────────────────────────────

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def entries(self) -> list[str]:
        with self._lock:
            return list(self._entries)

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def live_entries(self) -> list[str]:
        with self._lock:
            return [p for p in self._entries if p not in self._dead]

    def is_active(self) -> bool:
        """True nếu pool có ít nhất 1 proxy live → override proxy đơn."""
        with self._lock:
            return any(p not in self._dead for p in self._entries)

    def status(self) -> dict:
        """Snapshot trạng thái pool cho UI (đếm + danh sách dead)."""
        with self._lock:
            live = [p for p in self._entries if p not in self._dead]
            return {
                "mode": self._mode,
                "total": len(self._entries),
                "live": len(live),
                "dead": sorted(self._dead),
            }


# Module-level singleton dùng chung cho cả 3 manager + endpoint.
_PROXY_POOL: ProxyPool | None = None


def get_proxy_pool() -> ProxyPool:
    """Trả singleton ProxyPool (lazy-init)."""
    global _PROXY_POOL  # noqa: PLW0603
    if _PROXY_POOL is None:
        _PROXY_POOL = ProxyPool()
    return _PROXY_POOL


__all__ = ["ProxyPool", "get_proxy_pool", "normalize_proxies"]
