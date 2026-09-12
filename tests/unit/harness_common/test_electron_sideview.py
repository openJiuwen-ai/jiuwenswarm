# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import io
import json
import urllib.error
from dataclasses import dataclass
from urllib.parse import quote

import pytest

from jiuwenswarm.agents.harness.common.electron_sideview import (
    apply_session_sideview_target,
    resolve_session_sideview_target,
)


@dataclass(frozen=True)
class _FakeMcpConfig:
    params: dict

    def model_copy(self, *, update: dict) -> "_FakeMcpConfig":
        fields = {f.name: getattr(self, f.name) for f in self.__dataclass_fields__.values()}
        fields.update(update)
        return _FakeMcpConfig(**fields)


@dataclass(frozen=True)
class _FakeSettings:
    mcp_cfg: _FakeMcpConfig


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _install_resolver(monkeypatch: pytest.MonkeyPatch, payload: dict, seen: list[str]) -> None:
    def fake_urlopen(url, timeout):  # noqa: ANN001
        seen.append((url, timeout))
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.electron_sideview.urllib.request.urlopen",
        fake_urlopen,
    )


def test_resolve_session_sideview_target_returns_target_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLAYWRIGHT_MCP_TARGET_RESOLVER", "http://127.0.0.1:43124/")
    seen: list[tuple[str, float]] = []
    _install_resolver(monkeypatch, {"sessionId": "sess_a", "targetId": "target-1"}, seen)

    assert resolve_session_sideview_target("sess_a") == "target-1"
    assert seen == [(f"http://127.0.0.1:43124/{quote('sess_a', safe='')}", 15.0)]


def test_resolve_session_sideview_target_absent_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLAYWRIGHT_MCP_TARGET_RESOLVER", raising=False)

    assert resolve_session_sideview_target("sess_a") is None


def test_resolve_session_sideview_target_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAYWRIGHT_MCP_TARGET_RESOLVER", "http://127.0.0.1:43124")

    def fake_urlopen(url, timeout):  # noqa: ANN001
        raise urllib.error.HTTPError(url, 500, "boom", None, None)

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.electron_sideview.urllib.request.urlopen",
        fake_urlopen,
    )

    assert resolve_session_sideview_target("sess_a") is None


def test_apply_session_sideview_target_injects_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAYWRIGHT_MCP_TARGET_RESOLVER", "http://127.0.0.1:43124")
    seen: list[tuple[str, float]] = []
    _install_resolver(monkeypatch, {"sessionId": "sess_a", "targetId": "target-9"}, seen)
    settings = _FakeSettings(
        _FakeMcpConfig(
            {
                "command": "npx",
                "env": {
                    "PLAYWRIGHT_MCP_CDP_ENDPOINT": "http://127.0.0.1:43123",
                    "PLAYWRIGHT_MCP_TARGET_RESOLVER": "http://127.0.0.1:43124",
                },
            }
        )
    )

    updated = apply_session_sideview_target(settings, "sess_a")

    assert updated is not settings
    assert updated.mcp_cfg is not settings.mcp_cfg
    assert updated.mcp_cfg.params["env"]["PLAYWRIGHT_MCP_TARGET_ID"] == "target-9"
    # 原 env 其它键保留，原始 settings 对象不被修改（frozen 副本语义）。
    assert "PLAYWRIGHT_MCP_CDP_ENDPOINT" in updated.mcp_cfg.params["env"]
    assert "PLAYWRIGHT_MCP_TARGET_ID" not in settings.mcp_cfg.params["env"]


def test_apply_session_sideview_target_noop_without_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PLAYWRIGHT_MCP_TARGET_RESOLVER", raising=False)
    settings = _FakeSettings(_FakeMcpConfig({"command": "npx"}))

    assert apply_session_sideview_target(settings, "sess_a") is settings
