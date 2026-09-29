# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""沙箱 fallback_policy（设计 5.6 / spec 7.2）jiuwenswarm 侧归一与透传测试。

覆盖：
- ``config._normalize_fallback_policy`` 合法/非法/空值归一
- ``_ensure_sandbox_runtime_shape`` / ``get_sandbox_runtime`` 透传
- ``update_sandbox_runtime`` merge + 落盘
- ``interface_deep._effective_sandbox_fallback_policy`` strict 档强制 never
- ``sysop_builder.create_sandbox_sysop_card`` extra_params 透传（None 不写）
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jiuwenswarm.common import config as cfg_mod
from jiuwenswarm.server.runtime.agent_adapter import sysop_builder


class TestNormalizeFallbackPolicy:
    @staticmethod
    @pytest.mark.parametrize("value", ["never", "inline_only", "always"])
    def test_legal_passthrough(value: str) -> None:
        assert cfg_mod._normalize_fallback_policy(value) == value

    @staticmethod
    @pytest.mark.parametrize("value", ["", None, "   "])
    def test_empty_defaults_inline_only(value) -> None:
        assert cfg_mod._normalize_fallback_policy(value) == "inline_only"

    @staticmethod
    @pytest.mark.parametrize("value", ["sometimes", "NEVER", 1, True])
    def test_illegal_falls_back_inline_only(value) -> None:
        assert cfg_mod._normalize_fallback_policy(value) == "inline_only"


class TestSandboxRuntimeShape:
    @staticmethod
    def test_default_contains_inline_only() -> None:
        shaped = cfg_mod._ensure_sandbox_runtime_shape(None)
        assert shaped["fallback_policy"] == "inline_only"

    @staticmethod
    def test_valid_passthrough() -> None:
        shaped = cfg_mod._ensure_sandbox_runtime_shape({"fallback_policy": "never"})
        assert shaped["fallback_policy"] == "never"

    @staticmethod
    def test_invalid_normalized() -> None:
        shaped = cfg_mod._ensure_sandbox_runtime_shape({"fallback_policy": "bogus"})
        assert shaped["fallback_policy"] == "inline_only"

    @staticmethod
    def test_get_sandbox_runtime_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            cfg_mod,
            "get_config",
            lambda: {"sandbox": {"fallback_policy": "always"}},
        )
        assert cfg_mod.get_sandbox_runtime()["fallback_policy"] == "always"


class TestUpdateSandboxRuntime:
    @staticmethod
    def _patch_env(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sandbox: dict
    ) -> Path:
        target = tmp_path / "config.yaml"
        target.write_text("sandbox:\n  enabled: true\n", encoding="utf-8")
        # update_config 读写的模块常量为 CONFIG_YAML_PATH；旧名 _CONFIG_YAML_PATH
        # 仅为向后兼容别名，两个都 patch 才能确保落到 tmp 文件而非真实用户配置。
        monkeypatch.setattr(cfg_mod, "CONFIG_YAML_PATH", target)
        if hasattr(cfg_mod, "_CONFIG_YAML_PATH"):
            monkeypatch.setattr(cfg_mod, "_CONFIG_YAML_PATH", target)
        monkeypatch.setattr(cfg_mod, "get_config", lambda: {"sandbox": dict(sandbox)})
        return target

    def test_merge_and_persist(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        target = self._patch_env(monkeypatch, tmp_path, {"enabled": True})

        merged = cfg_mod.update_sandbox_runtime({"fallback_policy": "never"})

        assert merged["fallback_policy"] == "never"
        saved = yaml.safe_load(target.read_text(encoding="utf-8"))
        assert saved["sandbox"]["fallback_policy"] == "never"

    def test_invalid_policy_normalized(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._patch_env(monkeypatch, tmp_path, {"fallback_policy": "always"})

        merged = cfg_mod.update_sandbox_runtime({"fallback_policy": "bogus"})

        assert merged["fallback_policy"] == "inline_only"


class TestEffectiveSandboxFallbackPolicy:
    @staticmethod
    def _helper():
        from jiuwenswarm.server.runtime.agent_adapter import interface_deep

        return interface_deep

    def test_strict_forces_never(self, monkeypatch: pytest.MonkeyPatch) -> None:
        interface_deep = self._helper()
        monkeypatch.setattr(interface_deep, "is_strict_profile", lambda: True)
        assert (
            interface_deep._effective_sandbox_fallback_policy(
                {"fallback_policy": "always"}
            )
            == "never"
        )

    def test_non_strict_follows_runtime(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        interface_deep = self._helper()
        monkeypatch.setattr(interface_deep, "is_strict_profile", lambda: False)
        assert (
            interface_deep._effective_sandbox_fallback_policy(
                {"fallback_policy": "always"}
            )
            == "always"
        )

    def test_non_strict_invalid_normalized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        interface_deep = self._helper()
        monkeypatch.setattr(interface_deep, "is_strict_profile", lambda: False)
        assert (
            interface_deep._effective_sandbox_fallback_policy(
                {"fallback_policy": "bogus"}
            )
            == "inline_only"
        )

    def test_non_strict_missing_defaults_inline_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        interface_deep = self._helper()
        monkeypatch.setattr(interface_deep, "is_strict_profile", lambda: False)
        assert interface_deep._effective_sandbox_fallback_policy({}) == "inline_only"
        assert (
            interface_deep._effective_sandbox_fallback_policy(None) == "inline_only"
        )


class TestCreateSandboxSysopCard:
    @staticmethod
    def _extra_params(card) -> dict:
        return card.gateway_config.launcher_config.extra_params

    def test_fallback_policy_written_when_given(self) -> None:
        card = sysop_builder.create_sandbox_sysop_card(
            "http://127.0.0.1:19001", "jiuwenbox", fallback_policy="never"
        )
        assert card is not None
        assert self._extra_params(card)["fallback_policy"] == "never"

    def test_fallback_policy_omitted_when_none(self) -> None:
        card = sysop_builder.create_sandbox_sysop_card(
            "http://127.0.0.1:19001", "jiuwenbox"
        )
        assert card is not None
        assert "fallback_policy" not in self._extra_params(card)
