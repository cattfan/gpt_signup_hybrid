"""Check core session-cookie-cache: SessionStore + SessionProvider + SessionRecord.

Chạy: python3 test/check_session_cache_core.py
Không cần network — fetch_session_via_http được monkeypatch.
In [PASS]/[FAIL] realtime theo từng case.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GSH_DB_PATH", str(ROOT / "runtime" / "data.db"))

import session_provider as sp  # noqa: E402
from session_store import SessionStore, has_session_token, safe_email_slug  # noqa: E402
from session_provider import SessionProvider, SessionRecord, strip_cookies  # noqa: E402

_FAILS = 0


def _check(cond: bool, cid: str, desc: str, detail: str = "") -> None:
    global _FAILS
    tag = "PASS" if cond else "FAIL"
    if not cond:
        _FAILS += 1
    print(f"[{tag}] {cid} — {desc} :: {detail}", flush=True)


def _cookie(name: str, value: str = "v") -> dict:
    return {"name": name, "value": value, "domain": "chatgpt.com"}


SESSION_COOKIES = [_cookie("__Secure-next-auth.session-token", "tok"), _cookie("cf_clearance")]


def test_store(tmp: Path) -> None:
    print("--- SessionStore ---", flush=True)
    s = SessionStore(instance_id="inst_a", runtime_dir=tmp)

    # TC-01 save/load roundtrip
    s.save("a@x.com", {"cookies": SESSION_COOKIES, "access_token": "T"})
    rec = s.load("a@x.com")
    _check(rec is not None and rec["access_token"] == "T", "TC-01", "save/load roundtrip",
           f"rec={'ok' if rec else None}")

    # TC-02 atomic + 0600
    p = s._path_for("a@x.com")
    mode = oct(p.stat().st_mode & 0o777)
    _check(mode == "0o600", "TC-02", "file 0600", mode)

    # TC-03 isolation: instance khác → file khác, không đè
    s_b = SessionStore(instance_id="inst_b", runtime_dir=tmp)
    s_b.save("a@x.com", {"cookies": SESSION_COOKIES, "access_token": "OTHER"})
    _check(s.load("a@x.com")["access_token"] == "T"
           and s_b.load("a@x.com")["access_token"] == "OTHER",
           "TC-03", "isolation theo instance", "2 instance không đè nhau")

    # TC-04 collision: 2 email khác nhau cùng slug → 2 file khác nhau
    e1, e2 = "a+b@x.com", "a_b@x.com"
    _check(safe_email_slug(e1) == safe_email_slug(e2), "TC-04a", "slug trùng (đúng kỳ vọng)",
           f"{safe_email_slug(e1)}")
    _check(s._path_for(e1) != s._path_for(e2), "TC-04b", "path KHÁC nhau nhờ hash",
           f"{s._path_for(e1).name} vs {s._path_for(e2).name}")
    s.save(e1, {"cookies": SESSION_COOKIES, "access_token": "E1"})
    s.save(e2, {"cookies": SESSION_COOKIES, "access_token": "E2"})
    _check(s.load(e1)["access_token"] == "E1" and s.load(e2)["access_token"] == "E2",
           "TC-04c", "không phục vụ nhầm account", "E1/E2 tách biệt")

    # TC-05 email-guard: file bị sửa email lệch → load trả None
    bad = s._path_for("guard@x.com")
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text('{"email":"OTHER@x.com","access_token":"X"}', encoding="utf-8")
    _check(s.load("guard@x.com") is None, "TC-05", "guard email-mismatch → None")

    # TC-06 fail-soft: file hỏng JSON → None (không raise)
    broken = s._path_for("broken@x.com")
    broken.write_text("{not json", encoding="utf-8")
    _check(s.load("broken@x.com") is None, "TC-06", "JSON hỏng → None fail-soft")

    # TC-07 delete
    _check(s.delete("a@x.com") is True and s.load("a@x.com") is None and s.delete("a@x.com") is False,
           "TC-07", "delete idempotent")

    # TC-08 has_session_token
    _check(has_session_token(SESSION_COOKIES) is True and has_session_token([]) is False
           and has_session_token([_cookie("cf_clearance")]) is False,
           "TC-08", "has_session_token đúng")


def test_record() -> None:
    print("--- SessionRecord ---", flush=True)
    data = {"accessToken": "AT", "__cookies": SESSION_COOKIES, "user": {"email": "x"}}
    r = SessionRecord.from_session_json("a@x.com", data, proxy="http://p")
    _check(r.access_token == "AT" and r.session_token == "tok" and r.proxy == "http://p"
           and r.cookies == SESSION_COOKIES,
           "TC-09", "from_session_json trích đúng (session_token từ cookie)")

    class _R:
        email = "r@x.com"; cookies = SESSION_COOKIES; access_token = "RT"
        session_token = "ST"; two_factor = {"secret": "s"}
    r2 = SessionRecord.from_signup_result(_R(), proxy=None)
    _check(r2.email == "r@x.com" and r2.session_token == "ST" and r2.two_factor == {"secret": "s"},
           "TC-10", "from_signup_result trích đúng")

    _check("__cookies" not in strip_cookies(dict(data)) and "accessToken" in strip_cookies(dict(data)),
           "TC-11", "strip_cookies bỏ __cookies giữ phần khác")


class _FakeRepo:
    def __init__(self, vals: dict): self._v = vals
    def get(self, k): return self._v.get(k)


async def test_provider(tmp: Path) -> None:
    print("--- SessionProvider ---", flush=True)
    store = SessionStore(instance_id="prov", runtime_dir=tmp)
    prov = SessionProvider(store, settings_repo=None)  # defaults: reuse on, revalidate http on

    calls = {"login": 0, "fetch": 0}

    async def login_fn():
        calls["login"] += 1
        return {"accessToken": "FRESH", "__cookies": SESSION_COOKIES, "user": {}}

    # TC-12 miss → login + save
    out = await prov.acquire(email="m@x.com", proxy=None, login_fn=login_fn, log=lambda *_: None)
    _check(out["accessToken"] == "FRESH" and calls["login"] == 1 and store.load("m@x.com") is not None,
           "TC-12", "miss → login_fn + save", f"login_calls={calls['login']}")

    # TC-13 reuse hit → KHÔNG login (mock fetch_session_via_http 200)
    async def fake_fetch(*, cookies, proxy, **kw):
        calls["fetch"] += 1
        return {"accessToken": "REMINTED", "user": {}}
    sp.fetch_session_via_http = fake_fetch  # monkeypatch trong namespace provider
    out2 = await prov.acquire(email="m@x.com", proxy=None, login_fn=login_fn, log=lambda *_: None)
    _check(out2["accessToken"] == "REMINTED" and calls["login"] == 1 and "__cookies" in out2,
           "TC-13", "reuse hit → mint token, không login", f"login_calls={calls['login']}")

    # TC-14 force_fresh → bỏ reuse, login lại
    out3 = await prov.acquire(email="m@x.com", proxy=None, login_fn=login_fn, force_fresh=True, log=lambda *_: None)
    _check(out3["accessToken"] == "FRESH" and calls["login"] == 2,
           "TC-14", "force_fresh bỏ reuse → login", f"login_calls={calls['login']}")

    # TC-15 revalidate fail → fallback login
    async def fail_fetch(*, cookies, proxy, **kw):
        from session_phase import SessionError
        raise SessionError("HTTP 401")
    sp.fetch_session_via_http = fail_fetch
    before = calls["login"]
    out4 = await prov.acquire(email="m@x.com", proxy=None, login_fn=login_fn, log=lambda *_: None)
    _check(out4["accessToken"] == "FRESH" and calls["login"] == before + 1,
           "TC-15", "revalidate fail → full login", f"login_calls={calls['login']}")

    # TC-16 fatal login → xoá record
    store.save("f@x.com", {"cookies": SESSION_COOKIES, "access_token": "OLD"})
    async def fatal_login():
        from session_phase import SessionError
        raise SessionError("password verify failed: HTTP 400")
    try:
        await prov.acquire(email="f@x.com", proxy=None, login_fn=fatal_login,
                           force_fresh=True, log=lambda *_: None)
    except Exception:
        pass
    _check(store.load("f@x.com") is None, "TC-16", "fatal login → delete record")

    # TC-17 reuse_enabled=false → luôn login, vẫn save
    prov2 = SessionProvider(store, settings_repo=_FakeRepo({"session.reuse_enabled": False}))
    store.save("g@x.com", {"cookies": SESSION_COOKIES, "access_token": "CACHED"})
    calls["login"] = 0
    out5 = await prov2.acquire(email="g@x.com", proxy=None, login_fn=login_fn, log=lambda *_: None)
    _check(out5["accessToken"] == "FRESH" and calls["login"] == 1,
           "TC-17", "reuse_enabled=false → luôn login")

    # TC-18 revalidate_http=false → reuse theo TTL, trả ĐÚNG key 'accessToken'
    prov3 = SessionProvider(store, settings_repo=_FakeRepo({
        "session.revalidate_http": False, "session.cookie_max_age_hours": 24,
    }))
    from session_store import now_iso
    store.save("ttl@x.com", {
        "cookies": SESSION_COOKIES, "access_token": "TTLTOK",
        "session_token": "s", "two_factor": None, "proxy": None,
        "created_at": now_iso(), "last_validated_at": now_iso(),
    })
    calls["login"] = 0
    out6 = await prov3.acquire(email="ttl@x.com", proxy=None, login_fn=login_fn, log=lambda *_: None)
    _check(out6.get("accessToken") == "TTLTOK" and calls["login"] == 0 and "__cookies" in out6,
           "TC-18", "revalidate_http=false → reuse TTL, key accessToken đúng",
           f"at={out6.get('accessToken')} login={calls['login']}")

    # TC-19 revalidate_http=false + record QUÁ hạn → full login
    prov4 = SessionProvider(store, settings_repo=_FakeRepo({
        "session.revalidate_http": False, "session.cookie_max_age_hours": 1,
    }))
    store.save("old@x.com", {
        "cookies": SESSION_COOKIES, "access_token": "OLD",
        "session_token": "s", "two_factor": None, "proxy": None,
        "created_at": "2020-01-01T00:00:00", "last_validated_at": "2020-01-01T00:00:00",
    })
    calls["login"] = 0
    out7 = await prov4.acquire(email="old@x.com", proxy=None, login_fn=login_fn, log=lambda *_: None)
    _check(out7["accessToken"] == "FRESH" and calls["login"] == 1,
           "TC-19", "revalidate_http=false + quá TTL → full login")


async def _main() -> None:
    with tempfile.TemporaryDirectory(prefix="sesscache_") as d:
        tmp = Path(d)
        test_store(tmp)
        test_record()
        await test_provider(tmp)
    print(f"\n=== {'ALL PASS' if _FAILS == 0 else str(_FAILS) + ' FAIL'} ===", flush=True)
    sys.exit(1 if _FAILS else 0)


if __name__ == "__main__":
    asyncio.run(_main())
