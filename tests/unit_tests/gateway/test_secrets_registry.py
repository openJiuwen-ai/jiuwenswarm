# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Secrets registry 与 env tip 写入口的回归锚定。

- bundled_registry_path 必须指向 gateway 包根下的 resources（与搬迁前
  jiuwenswarm/resources 的包内语义一致），防止未来再搬迁时静默漂移；
- bundled registry 文件缺失时按既有设计静默回退 user-only；
- set_os_environ 的两个写出口必须失效 resolved-config 缓存，
  与 promote_staged_env 等其余 tip 写入口对齐（检视意见回归）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import gateway.common.local_env_config as lec
from gateway.common.secrets.registry import (
    SecretRegistry,
    bundled_registry_path,
)


@pytest.fixture(autouse=True)
def _reset_env_tip_state():
    yield
    lec.reset_local_env_state_for_tests()


def test_bundled_registry_path_anchors_to_gateway_package_resources():
    repo_root = Path(__file__).resolve().parents[3]
    assert bundled_registry_path() == repo_root / "gateway" / "resources" / "secret_registry.yaml"


def test_missing_bundled_registry_falls_back_to_user_only(tmp_path):
    reg = SecretRegistry(config_dir=tmp_path, workspace_dir=tmp_path)
    assert not reg._entries
    (tmp_path / "secret_registry.yaml").write_text(
        "TEST_KEY:\n  medium: env\n  path: TEST_KEY\n", encoding="utf-8"
    )
    reg = SecretRegistry(config_dir=tmp_path, workspace_dir=tmp_path)
    assert "TEST_KEY" in reg._entries


@pytest.mark.parametrize("value", ["v", None])
def test_set_os_environ_invalidates_resolved_config_cache(monkeypatch, value):
    calls: list[tuple] = []
    monkeypatch.setattr(
        lec,
        "_invalidate_resolved_config_cache",
        lambda service_id=None, agent_id=None: calls.append((service_id, agent_id)),
    )
    lec.set_os_environ("SOME_TIP_KEY", value, service_id="svc", agent_id="agt")
    assert calls == [("svc", "agt")]
