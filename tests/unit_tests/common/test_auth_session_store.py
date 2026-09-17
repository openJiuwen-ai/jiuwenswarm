# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import json
import time

import pytest

from jiuwenswarm.common.auth import session_store as store_mod
from jiuwenswarm.common.auth.account_kit import Credential, LoginOutcome
from jiuwenswarm.common.auth.session_store import AuthSessionStore


@pytest.fixture(autouse=True)
def _isolated_auth_dir(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth"
    auth_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(store_mod, "auth_dir", lambda: auth_path)
    yield auth_path


def _outcome(user_id: str = "openid-1", refresh: str = "SUPER-SECRET-REFRESH") -> LoginOutcome:
    return LoginOutcome(
        credential=Credential(
            id_token="ID-TOKEN-123",
            refresh_token=refresh,
            expires_at=time.time() + 3600,
        ),
        user_id=user_id,
        user_name="138******00",
    )


def test_create_and_get(_isolated_auth_dir):
    store = AuthSessionStore()
    session = store.create(_outcome())
    assert store.get(session.session_id) is session
    assert store.get_by_user("openid-1") is session
    assert store.any_session() is session


def test_public_view_never_leaks_credentials(_isolated_auth_dir):
    store = AuthSessionStore()
    view = store.create(_outcome()).public_view()
    serialized = json.dumps(view)
    for leaked in ("ID-TOKEN-123", "SUPER-SECRET-REFRESH"):
        assert leaked not in serialized


def test_persisted_file_is_encrypted(_isolated_auth_dir):
    store = AuthSessionStore()
    store.create(_outcome())
    raw = (_isolated_auth_dir / "sessions.json").read_text(encoding="utf-8")
    assert "SUPER-SECRET-REFRESH" not in raw
    assert "ID-TOKEN-123" not in raw
    assert set(json.loads(raw)) == {"v", "payload"}
    assert json.loads(raw)["v"] == 2


def test_reload_restores_session(_isolated_auth_dir):
    first = AuthSessionStore()
    created = first.create(_outcome())

    restored = AuthSessionStore().get(created.session_id)
    assert restored is not None
    assert restored.user_id == "openid-1"
    assert restored.credential.refresh_token == "SUPER-SECRET-REFRESH"
    assert restored.credential.id_token == "ID-TOKEN-123"


def test_relogin_replaces_old_session(_isolated_auth_dir):
    store = AuthSessionStore()
    first = store.create(_outcome())
    second = store.create(_outcome())
    assert first.session_id != second.session_id
    assert store.get(first.session_id) is None
    assert len(store.list_sessions()) == 1


def test_stale_session_is_dropped(_isolated_auth_dir):
    store = AuthSessionStore(ttl_s=1.0)
    session = store.create(_outcome())
    session.created_at = time.time() - 10
    assert store.get(session.session_id) is None
    assert store.any_session() is None


def test_remove_and_clear(_isolated_auth_dir):
    store = AuthSessionStore()
    session = store.create(_outcome())
    store.remove(session.session_id)
    assert store.get(session.session_id) is None

    store.create(_outcome("openid-2"))
    store.clear()
    assert store.list_sessions() == []


def test_corrupt_archive_is_tolerated(_isolated_auth_dir):
    (_isolated_auth_dir / "sessions.json").write_text("not json at all", encoding="utf-8")
    assert AuthSessionStore().any_session() is None


def test_unknown_archive_version_is_treated_as_logged_out(_isolated_auth_dir):
    store_mod.AuthSessionStore().create(_outcome())
    path = _isolated_auth_dir / "sessions.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["v"] = 1
    path.write_text(json.dumps(envelope), encoding="utf-8")
    assert AuthSessionStore().list_sessions() == []


def test_update_credential_persists(_isolated_auth_dir):
    store = AuthSessionStore()
    session = store.create(_outcome())
    store.update_credential(
        session.session_id, Credential(id_token="ID-NEW", refresh_token="R", expires_at=time.time() + 3600)
    )
    assert AuthSessionStore().get(session.session_id).credential.id_token == "ID-NEW"


def test_no_persist_mode_writes_nothing(_isolated_auth_dir):
    store = AuthSessionStore(persist=False)
    store.create(_outcome())
    assert not (_isolated_auth_dir / "sessions.json").exists()


def test_reloads_when_another_process_writes(_isolated_auth_dir):
    reader = AuthSessionStore()
    assert reader.any_session() is None  # 先读一次，此时确实没有会话

    writer = AuthSessionStore()  # 模拟另一个进程完成登录
    created = writer.create(_outcome())

    assert reader.any_session() is not None
    assert reader.get(created.session_id) is not None


def test_logout_does_not_overwrite_what_another_process_just_wrote(_isolated_auth_dir):
    gateway = AuthSessionStore()
    alice = gateway.create(_outcome("openid-alice"))
    bob = gateway.create(_outcome("openid-bob"))

    agent_server = AuthSessionStore()
    assert agent_server.get(bob.session_id) is not None
    agent_server.update_credential(  # 另一个进程续期，写了盘
        bob.session_id, Credential(id_token="ID-RENEWED", refresh_token="R", expires_at=time.time() + 3600)
    )

    gateway.remove(alice.session_id)  # 本进程内存里 bob 还是旧凭据

    fresh = AuthSessionStore()
    assert fresh.get(alice.session_id) is None
    assert fresh.get(bob.session_id).credential.id_token == "ID-RENEWED", "bob 刚续好的凭据被登出操作覆盖了"
