# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import threading
import time

import pytest

from jiuwenswarm.common.auth import service as service_mod
from jiuwenswarm.common.auth import session_store as store_mod
from jiuwenswarm.common.auth.account_kit import Credential, LoginOutcome
from jiuwenswarm.common.auth.service import REFRESH_AHEAD_S, AuthService, ModelAuthRequired


@pytest.fixture
def store(tmp_path, monkeypatch) -> store_mod.AuthSessionStore:
    monkeypatch.setattr(store_mod, "auth_dir", lambda: tmp_path)
    return store_mod.AuthSessionStore()


def _credential(ttl_s: float, id_token: str = "id-old") -> Credential:
    return Credential(id_token=id_token, refresh_token="rt-1", expires_at=time.time() + ttl_s)


def _outcome(ttl_s: float = -10, user_id: str = "openid-1") -> LoginOutcome:
    return LoginOutcome(credential=_credential(ttl_s), user_id=user_id, user_name="u")


class CountingFlow:

    def __init__(self, delay: float = 0.05, result: Credential | None | str = "fresh") -> None:
        self.calls = 0
        self._delay = delay
        self._result = result
        self._lock = threading.Lock()

    def refresh(self, credential):
        with self._lock:
            self.calls += 1
        time.sleep(self._delay)  # 没有这个耗时，线程可能根本没机会重叠
        if self._result == "fresh":
            return Credential(id_token="id-new", refresh_token=credential.refresh_token,
                              expires_at=time.time() + 3600)
        return self._result


def test_concurrent_refresh_happens_only_once(store):
    flow = CountingFlow()
    service = AuthService(flow=flow, store=store)
    session = store.create(_outcome())

    results = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(20)

    def worker():
        barrier.wait()  # 尽量让 20 个线程同时进入
        refreshed = service.try_refresh(session)
        with results_lock:
            results.append(refreshed)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert flow.calls == 1, f"续期被调用了 {flow.calls} 次，应该只有 1 次"
    assert len(results) == 20
    assert all(r is not None and r.credential.id_token == "id-new" for r in results), (
        "合并掉的请求也必须拿到新凭据，而不是失败"
    )
    assert store.get(session.session_id).credential.id_token == "id-new"


def test_second_caller_sees_fresh_credential_without_refreshing(store):
    flow = CountingFlow(delay=0)
    service = AuthService(flow=flow, store=store)
    session = store.create(_outcome())

    assert service.try_refresh(session) is not None
    assert service.try_refresh(session).credential.id_token == "id-new"
    assert flow.calls == 1, "凭据已经是新的，不该再刷一次"


def test_refresh_result_discarded_when_session_logged_out(store):

    class LogoutDuringRefresh(CountingFlow):
        def refresh(self, credential):
            store.remove(session.session_id)  # 续期途中用户登出
            return super().refresh(credential)

    service = AuthService(flow=LogoutDuringRefresh(delay=0), store=store)
    session = store.create(_outcome())

    assert service.try_refresh(session) is None
    assert store.get(session.session_id) is None, "已注销的会话不该被续期结果复活"


def test_failed_refresh_of_expired_token_reports_none(store):
    service = AuthService(flow=CountingFlow(delay=0, result=None), store=store)
    session = store.create(_outcome(ttl_s=-10))
    assert service.try_refresh(session) is None


def test_failed_early_refresh_keeps_the_still_valid_token(store):
    service = AuthService(flow=CountingFlow(delay=0, result=None), store=store)
    session = store.create(_outcome(ttl_s=REFRESH_AHEAD_S / 2))
    kept = service.try_refresh(session)
    assert kept is not None and kept.credential.id_token == "id-old"


def test_different_users_refresh_in_parallel(store):
    flow = CountingFlow(delay=0.05)
    service = AuthService(flow=flow, store=store)
    sessions = [store.create(_outcome(user_id=u)) for u in ("openid-1", "openid-2", "openid-3")]

    started = time.monotonic()
    threads = [threading.Thread(target=service.try_refresh, args=(s,)) for s in sessions]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    elapsed = time.monotonic() - started

    assert flow.calls == 3, "三个不同账号各刷一次"
    assert elapsed < 0.13, f"不同账号被互相阻塞了（耗时 {elapsed:.3f}s）"


def test_fresh_token_is_not_refreshed(store):
    flow = CountingFlow(delay=0)
    service = AuthService(flow=flow, store=store)
    session = store.create(_outcome(ttl_s=3600))
    assert service.ensure_fresh(session) is session
    assert flow.calls == 0


def test_blocking_caller_refreshes_ahead_of_expiry(store):
    flow = CountingFlow(delay=0)
    service = AuthService(flow=flow, store=store)
    session = store.create(_outcome(ttl_s=REFRESH_AHEAD_S / 2))
    assert service.ensure_fresh(session).credential.id_token == "id-new"
    assert flow.calls == 1


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_non_blocking_caller_uses_old_token_and_refreshes_in_background(store):
    release = threading.Event()

    class BlockedFlow(CountingFlow):
        def refresh(self, credential):
            release.wait(5)  # 续期卡住期间，热路径必须已经返回
            return super().refresh(credential)

    flow = BlockedFlow(delay=0)
    service = AuthService(flow=flow, store=store)
    session = store.create(_outcome(ttl_s=REFRESH_AHEAD_S / 2))

    current = service.ensure_fresh(session, blocking=False)
    assert current is not None and current.credential.id_token == "id-old", "热路径不能等续期"
    release.set()

    assert _wait_for(lambda: store.get(session.session_id).credential.id_token == "id-new")
    assert flow.calls == 1


def test_non_blocking_caller_with_expired_token_gets_none_but_triggers_refresh(store):
    flow = CountingFlow(delay=0)
    service = AuthService(flow=flow, store=store)
    session = store.create(_outcome(ttl_s=-10))

    assert service.ensure_fresh(session, blocking=False) is None
    assert _wait_for(lambda: store.get(session.session_id).credential.id_token == "id-new")


def test_background_refresh_is_not_spawned_per_request(store):
    flow = CountingFlow(delay=0.2)
    service = AuthService(flow=flow, store=store)
    session = store.create(_outcome(ttl_s=REFRESH_AHEAD_S / 2))

    for _ in range(20):
        service.ensure_fresh(session, blocking=False)
    assert _wait_for(lambda: store.get(session.session_id).credential.id_token == "id-new")
    assert flow.calls == 1


def test_live_session_reasons(store, monkeypatch):
    service = AuthService(flow=CountingFlow(delay=0, result=None), store=store)
    monkeypatch.setattr(service_mod, "_service", service)

    with pytest.raises(ModelAuthRequired) as excinfo:
        service_mod.live_session("no-such-session")
    assert excinfo.value.reason == "not_logged_in"

    session = store.create(_outcome(ttl_s=-10))
    with pytest.raises(ModelAuthRequired) as excinfo:
        service_mod.live_session(session.session_id)
    assert excinfo.value.reason == "session_expired"


def test_no_session_id_never_borrows_the_machines_login(store, monkeypatch):
    service = AuthService(flow=CountingFlow(delay=0), store=store)
    monkeypatch.setattr(service_mod, "_service", service)
    store.create(_outcome(ttl_s=3600))  # 另一个窗口已经登录

    for missing in (None, ""):
        with pytest.raises(ModelAuthRequired) as excinfo:
            service_mod.resolve_id_token(missing)
        assert excinfo.value.reason == "not_logged_in"


def test_resolve_id_token_returns_the_session_token(store, monkeypatch):
    service = AuthService(flow=CountingFlow(delay=0), store=store)
    monkeypatch.setattr(service_mod, "_service", service)
    session = store.create(_outcome(ttl_s=3600))
    assert service_mod.resolve_id_token(session.session_id) == "id-old"


def test_logout_without_session_id_touches_nobody(store):
    service = AuthService(flow=CountingFlow(delay=0), store=store)
    alice = store.create(_outcome(ttl_s=3600, user_id="openid-alice"))
    bob = store.create(_outcome(ttl_s=3600, user_id="openid-bob"))

    service.logout(None)
    service.logout("")

    assert store.get(alice.session_id) is not None
    assert store.get(bob.session_id) is not None


def test_logout_removes_only_that_session(store):
    service = AuthService(flow=CountingFlow(delay=0), store=store)
    alice = store.create(_outcome(ttl_s=3600, user_id="openid-alice"))
    bob = store.create(_outcome(ttl_s=3600, user_id="openid-bob"))

    service.logout(alice.session_id)

    assert store.get(alice.session_id) is None
    assert store.get(bob.session_id) is not None
