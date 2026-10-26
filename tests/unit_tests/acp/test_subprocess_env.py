# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from __future__ import annotations

import pytest

from jiuwenswarm.acp.subprocess_env import build_acp_subprocess_env


def test_parent_openai_key_is_not_inherited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stale-parent")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = build_acp_subprocess_env(None)

    assert "OPENAI_API_KEY" not in env
    assert env["PATH"] == "/usr/bin"


def test_profile_env_supplies_scoped_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stale-parent")

    env = build_acp_subprocess_env({"OPENAI_API_KEY": "sk-from-profile"})

    assert env["OPENAI_API_KEY"] == "sk-from-profile"


def test_profile_env_resolves_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-resolved-at-spawn")

    env = build_acp_subprocess_env({"OPENAI_API_KEY": "${OPENAI_API_KEY}"})

    assert env["OPENAI_API_KEY"] == "sk-resolved-at-spawn"


def test_empty_profile_env_has_no_scoped_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    env = build_acp_subprocess_env(None)

    assert "OPENAI_API_KEY" not in env


def test_non_scoped_keys_still_from_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSTOM_FLAG", "parent-value")

    env = build_acp_subprocess_env(None)

    assert env["CUSTOM_FLAG"] == "parent-value"
