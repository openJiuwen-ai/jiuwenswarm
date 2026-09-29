# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""兜底总开关（spec 7.3）：读取/strict 档忽略/变更落审计。"""
from __future__ import annotations

import pytest

import jiuwenswarm.common.config as config_mod
from jiuwenswarm.agents.harness.common.rails.security_lists import audit, fallback


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("permissions:\n  enabled: true\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_YAML_PATH", cfg_path)
    monkeypatch.setattr(config_mod, "get_config_file", lambda: cfg_path)
    monkeypatch.setattr(audit, "_audit_file", lambda: tmp_path / "security_audit.jsonl")
    return {"cfg": cfg_path, "tmp": tmp_path}


def _write(cfg_path, text: str) -> None:
    cfg_path.write_text(text, encoding="utf-8")


def test_switch_default_true_when_missing(env):
    assert fallback.p2_fail_closed_switch() is True


def test_switch_explicit_false(env):
    _write(env["cfg"], "security:\n  fallback:\n    p2_fail_closed: false\n")
    assert fallback.p2_fail_closed_switch() is False


def test_active_non_strict_follows_switch(env):
    # 非 strict 档（enabled=true 且无 permission_mode=strict → auto_approve）；
    # 注意 permissions 段缺失会 fail-safe 回退 strict 档，必须显式写出
    _write(
        env["cfg"],
        "permissions:\n  enabled: true\n"
        "security:\n  fallback:\n    p2_fail_closed: false\n",
    )
    assert fallback.p2_fail_closed_active() is False


def test_active_strict_ignores_switch(env):
    # strict 档（permission_mode=strict → profile default）：开关 false 也恒 true
    _write(
        env["cfg"],
        "permissions:\n  enabled: true\n  permission_mode: strict\n"
        "security:\n  fallback:\n    p2_fail_closed: false\n",
    )
    assert fallback.p2_fail_closed_active() is True
    assert fallback.is_strict_profile() is True


def test_set_p2_fail_closed_writes_and_audits(env):
    assert fallback.set_p2_fail_closed(False) is True
    assert fallback.p2_fail_closed_switch() is False
    events = audit.query_events(kind=audit.AUDIT_FALLBACK_SWITCH)
    assert len(events) == 1
    assert events[0]["before"] is True
    assert events[0]["after"] is False

    assert fallback.set_p2_fail_closed(True) is True
    events = audit.query_events(kind=audit.AUDIT_FALLBACK_SWITCH)
    assert len(events) == 2
    assert events[1]["before"] is False
    assert events[1]["after"] is True
    # 重复读回确认落盘
    assert fallback.p2_fail_closed_switch() is True


def test_switch_read_error_fail_safe_default(env, monkeypatch):
    monkeypatch.setattr(
        config_mod, "get_config", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert fallback.p2_fail_closed_switch() is True
    assert fallback.is_strict_profile() is True
