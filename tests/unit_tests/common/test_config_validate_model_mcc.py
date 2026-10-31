"""``config.validate_model`` 探测配置以持久化条目为基线的回归测试。

背景：探测此前只拼表单可见字段，``custom_headers`` / ``ssl_cert`` 等配置页
未暴露的 write-only 字段不进探测请求，导致依赖请求头鉴权的网关
「对话正常、测试 401」。修复后探测以持久化条目 mcc 为基线，表单草稿值
覆盖，探测预算固定。
"""

import pytest

from jiuwenswarm.common.config_panel.models_handlers import (
    build_probe_model_client_config,
    resolve_persisted_mcc_for_validate,
)


def _entry(model_name: str, mcc_extra: dict, alias: str = "") -> dict:
    mcc = {"model_name": model_name, "api_base": "https://persisted.test/v1"}
    mcc.update(mcc_extra)
    entry = {"model_client_config": mcc, "model_config_obj": {}}
    if alias:
        entry["alias"] = alias
    return entry


class TestResolvePersistedMccForValidate:
    """Tests for ``resolve_persisted_mcc_for_validate``."""

    @pytest.fixture
    def mock_models(self, monkeypatch):
        """Patch ``get_config`` and ``get_default_models`` with a list of model entries."""

        def _patch(entries):
            def fake_get_config():
                return {"models": {"defaults": entries}}

            def fake_get_default_models(_cfg):
                return entries

            monkeypatch.setattr(
                "jiuwenswarm.common.config_panel.models_handlers.get_config",
                fake_get_config,
            )
            monkeypatch.setattr(
                "jiuwenswarm.common.config_panel.models_handlers.get_default_models",
                fake_get_default_models,
            )

        return _patch

    def test_match_by_model_name_returns_mcc(self, mock_models):
        mock_models([_entry("gpt-4", {"custom_headers": {"Authorization": "Bearer t"}})])
        result = resolve_persisted_mcc_for_validate("gpt-4")
        assert result["custom_headers"] == {"Authorization": "Bearer t"}

    def test_match_by_alias_returns_mcc(self, mock_models):
        mock_models([_entry("gpt-4-turbo", {"ssl_cert": "/tmp/ca.pem"}, alias="my-gpt")])
        result = resolve_persisted_mcc_for_validate("my-gpt")
        assert result["ssl_cert"] == "/tmp/ca.pem"

    def test_no_match_returns_empty_dict(self, mock_models):
        mock_models([_entry("gpt-4", {})])
        assert resolve_persisted_mcc_for_validate("unknown-model") == {}

    def test_get_config_exception_returns_empty_dict(self, monkeypatch):
        def raise_exc():
            raise RuntimeError("config broken")

        monkeypatch.setattr(
            "jiuwenswarm.common.config_panel.models_handlers.get_config",
            raise_exc,
        )
        assert resolve_persisted_mcc_for_validate("any") == {}

    def test_non_dict_entry_skipped(self, mock_models):
        mock_models(
            [
                "not-a-dict-entry",
                _entry("gpt-4", {"custom_headers": {"X-Trace": "1"}}),
            ]
        )
        result = resolve_persisted_mcc_for_validate("gpt-4")
        assert result["custom_headers"] == {"X-Trace": "1"}

    def test_returns_deep_copy(self, mock_models):
        entries = [_entry("gpt-4", {"custom_headers": {"X-Trace": "1"}})]
        mock_models(entries)
        result = resolve_persisted_mcc_for_validate("gpt-4")
        result["custom_headers"]["X-Trace"] = "mutated"
        assert entries[0]["model_client_config"]["custom_headers"] == {"X-Trace": "1"}

    def test_first_matching_entry_wins_over_later_alias_match(self, mock_models):
        """First hit in ``models.defaults`` order wins (same lookup as
        ``resolve_model_config_obj_for_validate``): an earlier model_name
        match beats a later alias match; order reversal flips the result."""
        mock_models(
            [
                _entry("gpt-4", {"custom_headers": {"A": "1"}}, alias="custom-gpt"),
                _entry("gpt-4-turbo", {"custom_headers": {"B": "2"}}, alias="gpt-4"),
            ]
        )
        result = resolve_persisted_mcc_for_validate("gpt-4")
        assert result["custom_headers"] == {"A": "1"}


class TestBuildProbeModelClientConfig:
    """Tests for ``build_probe_model_client_config``."""

    def test_persisted_custom_headers_enter_probe(self):
        cfg = build_probe_model_client_config(
            "OpenAI",
            "sk-draft",
            "https://draft.test/v1",
            persisted_mcc={
                "model_name": "m1",
                "custom_headers": {"Authorization": "Bearer stored"},
            },
        )
        assert cfg.custom_headers == {"Authorization": "Bearer stored"}
        # 表单草稿值权威覆盖
        assert cfg.api_key == "sk-draft"
        assert cfg.api_base == "https://draft.test/v1"

    def test_draft_overrides_persisted_draft_fields(self):
        cfg = build_probe_model_client_config(
            "Anthropic",
            "sk-new",
            "https://new.test/v1",
            persisted_mcc={
                "api_key": "sk-stale",
                "api_base": "https://stale.test/v1",
                "client_provider": "OpenAI",
            },
        )
        assert cfg.api_key == "sk-new"
        assert cfg.api_base == "https://new.test/v1"
        assert cfg.client_provider == "Anthropic"

    def test_probe_budget_not_polluted_by_persisted_entry(self):
        cfg = build_probe_model_client_config(
            "OpenAI",
            "k",
            "https://x.test/v1",
            persisted_mcc={"timeout": 1800, "max_retries": 5, "client_id": "runtime"},
        )
        assert cfg.timeout == 25.0
        assert cfg.max_retries == 0
        assert cfg.client_id == "config-validate"

    def test_endpoint_profile_draft_wins(self):
        cfg = build_probe_model_client_config(
            "OpenAI",
            "k",
            "https://x.test/v1",
            endpoint_profile="qwen38",
            persisted_mcc={"endpoint_profile": "stale-profile"},
        )
        assert cfg.endpoint_profile == "qwen38"

    def test_persisted_endpoint_profile_dropped_when_no_draft(self):
        """无草稿方言时按 api_base host 推断的语义由调用方决定，基线值不得残留。"""
        cfg = build_probe_model_client_config(
            "OpenAI",
            "k",
            "https://x.test/v1",
            persisted_mcc={"endpoint_profile": "stale-profile"},
        )
        assert cfg.endpoint_profile is None

    def test_persisted_endpoint_profile_kept_when_caller_falls_back(self):
        """调用方（TUI）推断未命中时回传持久化方言：探测与对话走同一路由。"""
        cfg = build_probe_model_client_config(
            "OpenAI",
            "k",
            "https://x.test/v1",
            endpoint_profile="persisted-profile",
            persisted_mcc={"endpoint_profile": "persisted-profile"},
        )
        assert cfg.endpoint_profile == "persisted-profile"

    def test_persisted_endpoint_profile_stale_when_host_inference_wins(self):
        """host 推断命中时覆盖持久化方言：方言与 api_base 绑定，旧值不残留。"""
        cfg = build_probe_model_client_config(
            "OpenAI",
            "k",
            "https://new-gateway.test/v1",
            endpoint_profile="inferred-vllm",
            persisted_mcc={"endpoint_profile": "stale-profile"},
        )
        assert cfg.endpoint_profile == "inferred-vllm"

    def test_verify_ssl_none_keeps_persisted_value(self):
        cfg = build_probe_model_client_config(
            "OpenAI",
            "k",
            "https://x.test/v1",
            persisted_mcc={"verify_ssl": True},
        )
        assert cfg.verify_ssl is True

    def test_verify_ssl_none_defaults_false_without_persisted(self):
        cfg = build_probe_model_client_config(
            "OpenAI",
            "k",
            "https://x.test/v1",
        )
        assert cfg.verify_ssl is False

    def test_verify_ssl_draft_overrides(self):
        cfg = build_probe_model_client_config(
            "OpenAI",
            "k",
            "https://x.test/v1",
            verify_ssl=False,
            persisted_mcc={"verify_ssl": True},
        )
        assert cfg.verify_ssl is False

    def test_no_persisted_entry_matches_legacy_behavior(self):
        """未知模型（尚未保存）：无基线，仅表单字段——与修复前行为一致。"""
        cfg = build_probe_model_client_config(
            "OpenAI",
            "k",
            "https://x.test/v1",
            endpoint_profile="",
            verify_ssl=False,
        )
        assert cfg.custom_headers is None
        assert cfg.api_key == "k"
        assert cfg.timeout == 25.0

    def test_model_name_never_enters_client_config(self):
        cfg = build_probe_model_client_config(
            "OpenAI",
            "k",
            "https://x.test/v1",
            persisted_mcc={"model_name": "m1", "custom_headers": {}},
        )
        assert "model_name" not in cfg.model_dump()

    @pytest.mark.parametrize(
        "bad_mcc",
        [
            {"custom_headers": "not-a-dict"},
            {"ssl_cert": 123},
            {"verify_ssl": "not-a-bool"},
        ],
    )
    def test_invalid_persisted_field_types_fall_back_to_draft(self, bad_mcc):
        """手改 YAML 导致字段类型非法时，探测退化为仅表单字段，不抛异常。

        修复前探测字段全部来自代码字面量，构造不可能失败；引入持久化基线
        后用户数据可触发 ValidationError——必须兜底，否则脏配置打断探测。
        """
        cfg = build_probe_model_client_config(
            "OpenAI",
            "k",
            "https://x.test/v1",
            persisted_mcc=bad_mcc,
        )
        assert cfg.custom_headers is None
        assert cfg.ssl_cert is None
        assert cfg.api_key == "k"
        assert cfg.timeout == 25.0

    def test_invalid_extra_field_does_not_break_probe(self):
        """未知 extra 字段经 core ``extra="allow"`` 透传，与对话路径同口径，不 fallback。"""
        cfg = build_probe_model_client_config(
            "OpenAI",
            "k",
            "https://x.test/v1",
            persisted_mcc={"base_url": "https://alt.test/v1"},
        )
        assert cfg.base_url == "https://alt.test/v1"
        assert cfg.api_base == "https://x.test/v1"
