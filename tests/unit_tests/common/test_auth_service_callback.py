# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.auth import session_store as store_mod
from jiuwenswarm.common.auth.account_kit import Credential, LoginOutcome, OAuthError
from jiuwenswarm.common.auth.service import AuthService


class _Pending:
    def __init__(self) -> None:
        self.finished: list[tuple[str, str | None, OAuthError | None]] = []

    def begin_callback(self, state):
        return SimpleNamespace(code_verifier="verifier")

    def finish(self, state, session_id=None, error=None):
        self.finished.append((state, session_id, error))


class _Flow:
    def __init__(self) -> None:
        self.pending = _Pending()

    def exchange_code(self, code, verifier):
        return LoginOutcome(
            credential=Credential(id_token="id", refresh_token="rt", expires_at=time.time() + 3600),
            user_id="openid-1",
            user_name="u",
        )


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "auth_dir", lambda: tmp_path)
    return store_mod.AuthSessionStore()


def test_login_result_is_released_without_waiting_for_model_discovery(store, monkeypatch):
    flow = _Flow()
    service = AuthService(flow=flow, store=store)
    discovery_started = threading.Event()
    release = threading.Event()

    def _slow_discovery(session_id=None):
        discovery_started.set()
        release.wait(2)
        return 0

    monkeypatch.setattr(service, "refresh_model_catalog", _slow_discovery)
    try:
        session = service.complete_callback("state-1", code="code-1")
        assert flow.pending.finished == [("state-1", session.session_id, None)], "认领结果要立刻登记"
        assert discovery_started.wait(2), "模型目录仍要在后台拉"
    finally:
        release.set()


def test_session_store_failure_is_reported_to_the_waiting_client(store, monkeypatch):
    flow = _Flow()
    service = AuthService(flow=flow, store=store)

    def _broken(_outcome):
        raise OSError("disk full")

    monkeypatch.setattr(store, "create", _broken)
    with pytest.raises(OAuthError) as excinfo:
        service.complete_callback("state-1", code="code-1")
    assert excinfo.value.code == "session_store_failed"
    [(state, session_id, error)] = flow.pending.finished
    assert state == "state-1" and session_id is None and error is excinfo.value
