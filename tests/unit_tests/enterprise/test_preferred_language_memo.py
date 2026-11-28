# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""AS preferred_language 每请求 memo + 点查。"""

from __future__ import annotations

import pytest

from jiuwenswarm.enterprise.config import preferred_language as pref


@pytest.mark.asyncio
async def test_bind_prefills_memo_and_sync_resolve_once(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.edition.is_enterprise",
        lambda: True,
    )
    calls: list[str] = []

    async def fake_db_get(user_id: str):
        calls.append(user_id)
        if user_id == "alice":
            return "zh"
        return None

    monkeypatch.setattr(pref, "_db_get_language", fake_db_get)
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.tenant.tenant_identity.extract_triple",
        lambda _req: ("g", "b", "alice"),
    )

    async with pref.bind_preferred_language(object()) as lang:
        assert lang == "zh"
        assert pref.resolve_preferred_language() == "zh"
        assert pref.resolve_preferred_language() == "zh"
        # 同轮只点查用户行一次（miss 后再查 default 的路径不走）
        assert calls == ["alice"]

    # 请求结束后 memo 清空，同步 resolve 回退 yaml/en
    monkeypatch.setattr(pref, "_yaml_or_en", lambda: "en")
    assert pref.resolve_preferred_language() == "en"


@pytest.mark.asyncio
async def test_miss_user_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.edition.is_enterprise",
        lambda: True,
    )

    async def fake_db_get(user_id: str):
        if user_id == pref.DEFAULT_USER_ID:
            return "en"
        return None

    monkeypatch.setattr(pref, "_db_get_language", fake_db_get)
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.tenant.tenant_identity.extract_triple",
        lambda _req: ("g", "b", "bob"),
    )

    async with pref.bind_preferred_language(object()) as lang:
        assert lang == "en"
        assert pref.resolve_preferred_language() == "en"


@pytest.mark.asyncio
async def test_async_resolve_without_bind_does_not_leak_memo(monkeypatch):
    """未 bind 时允许本调用内写 memo，但返回后必须 reset，避免 worker 脏缓存。"""
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.edition.is_enterprise",
        lambda: True,
    )
    calls: list[str] = []

    async def fake_db_get(user_id: str):
        calls.append(user_id)
        return "zh" if user_id == "carol" else None

    monkeypatch.setattr(pref, "_db_get_language", fake_db_get)
    monkeypatch.setattr(pref, "_yaml_or_en", lambda: "en")

    assert await pref.resolve_preferred_language_async("carol") == "zh"
    # 无外层 bind：第二次仍应点查，且 ContextVar memo 已清空
    assert pref._MEMO_CV.get() is None
    assert await pref.resolve_preferred_language_async("carol") == "zh"
    assert calls == ["carol", "carol"]
    assert pref.resolve_preferred_language() == "en"


def test_personal_edition_reads_yaml(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.edition.is_enterprise",
        lambda: False,
    )
    monkeypatch.setattr(pref, "_yaml_or_en", lambda: "zh")
    assert pref.resolve_preferred_language() == "zh"


def test_personal_edition_ignores_memo_and_keeps_yaml(monkeypatch):
    """个人版语言以 yaml 为准；即使 bind 了 memo / ui_language 也不覆盖。"""
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.edition.is_enterprise",
        lambda: False,
    )
    monkeypatch.setattr(pref, "_yaml_or_en", lambda: "zh")
    with pref.bind_preferred_language_memo(user_id="alice", language="en"):
        assert pref.resolve_preferred_language() == "zh"


def test_bind_memo_sync_helper(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.edition.is_enterprise",
        lambda: True,
    )
    with pref.bind_preferred_language_memo(user_id="u1", language="zh"):
        assert pref.resolve_preferred_language() == "zh"
        assert pref.current_preferred_language_user_id() == "u1"


def test_framework_language_from_params_prefers_ui_language(monkeypatch):
    from jiuwenswarm.common.i18n import t

    monkeypatch.setattr(
        "jiuwenswarm.enterprise.edition.is_enterprise",
        lambda: True,
    )
    effective = pref.framework_language_from_params({"ui_language": "en"}, "zh")
    assert effective == "en"
    with pref.bind_preferred_language_memo(user_id="alice", language=effective):
        assert pref.resolve_preferred_language() == "en"
        assert "silent mode" in t(
            "tools.send_file.silent_forbidden",
            language=effective,
        ).lower()


def test_framework_language_from_params_falls_back_to_preferred():
    assert pref.framework_language_from_params({}, "zh") == "zh"
    assert pref.framework_language_from_params(None, "en") == "en"


def test_enterprise_memo_en_overrides_yaml_zh(monkeypatch):
    """UI/user preference memo must win over AS template yaml zh."""
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.edition.is_enterprise",
        lambda: True,
    )
    monkeypatch.setattr(pref, "_yaml_or_en", lambda: "zh")
    with pref.bind_preferred_language_memo(user_id="alice", language="en"):
        assert pref.resolve_preferred_language() == "en"
