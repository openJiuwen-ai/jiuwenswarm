# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for cron job model validation, incl. free-model fallbacks.

The frontend lets the user pick a free model for a cron job.
``validate_cron_model`` resolves configured models and login-catalog models.
Zen ids resolve only when the caller passes rows already fetched from
AgentServer (``zen_entries``). An empty list still rejects those names.
"""

from __future__ import annotations

import pytest

from gateway.auth import model_catalog as _model_catalog
from gateway.auth.model_catalog import LoginModel
from gateway.cron.models import (
    CronJob,
    normalize_cron_job_mcp,
    resolve_cron_model,
    validate_cron_model,
)


@pytest.fixture(autouse=True)
def _no_free_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """By default no free models of any source are available (clean baseline).

    Zen 缓存与登录模型目录都置空；需要免费模型的用例在自身内单独覆盖，
    避免测试结果依赖本机登录状态。
    """
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.opencode_zen.get_zen_free_model_entries",
        lambda: [],
    )
    monkeypatch.setattr(
        _model_catalog, "get_models",
        lambda *args, **kwargs: [],
    )


def _user_model_entry() -> dict:
    return {
        "model_client_config": {
            "api_base": "https://api.example.com/v1",
            "api_key": "sk-test",
            "model_name": "my-model",
            "client_provider": "OpenAI",
        },
        "is_default": True,
        "alias": "我的模型",
    }


def _environment_model_entry(model_name: str = "${MODEL_NAME}") -> dict:
    return {
        "model_client_config": {
            "api_base": "${API_BASE}",
            "api_key": "${API_KEY}",
            "model_name": model_name,
            "client_provider": "${MODEL_PROVIDER}",
        },
        "is_default": True,
        "alias": "默认模型",
    }


def _zen_free_entry(
    model_id: str = "deepseek-v4-flash-free", alias: str = "DeepSeek V4 Flash"
) -> dict:
    return {
        "model_client_config": {
            "api_base": "https://opencode.ai/zen/v1",
            "api_key": "public",
            "model_name": model_id,
            "client_provider": "OpenAI",
        },
        "alias": alias,
        "is_free": True,
    }


def test_validate_cron_model_none_or_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    assert validate_cron_model(None) is None
    assert validate_cron_model("") is None
    assert validate_cron_model("   ") is None


def test_validate_cron_model_resolves_user_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get_model_config(name: str, index: int | None = None) -> dict | None:
        # 模拟 config.py 的真实语义：model_name 或 alias 命中都返回条目
        if name in ("my-model", "我的模型"):
            return _user_model_entry()
        return None

    monkeypatch.setattr(
        "gateway.common.config.get_model_config",
        fake_get_model_config,
    )
    monkeypatch.setattr(
        "gateway.common.config.get_model_names", lambda: ["我的模型"]
    )
    assert validate_cron_model("my-model") == "my-model"
    # alias 也解析为 canonical model_name
    assert validate_cron_model("我的模型") == "my-model"


def test_validate_cron_model_resolves_environment_model_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODEL_NAME", "deployed-model")
    monkeypatch.setattr(
        "gateway.common.config.get_config_raw",
        lambda: {"models": {"defaults": [_environment_model_entry()]}},
    )

    assert validate_cron_model("deployed-model") == "deployed-model"
    assert validate_cron_model("默认模型") == "deployed-model"


def test_validate_cron_model_uses_environment_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.setattr(
        "gateway.common.config.get_config_raw",
        lambda: {
            "models": {
                "defaults": [_environment_model_entry("${MODEL_NAME:-fallback-model}")]
            }
        },
    )

    assert validate_cron_model("fallback-model") == "fallback-model"
    assert validate_cron_model("默认模型") == "fallback-model"


def test_validate_cron_model_rejects_empty_environment_model_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.setattr(
        "gateway.common.config.get_config_raw",
        lambda: {"models": {"defaults": [_environment_model_entry()]}},
    )

    with pytest.raises(
        ValueError,
        match=r"Configured model '默认模型'.*resolves to an empty value.*MODEL_NAME",
    ):
        validate_cron_model("默认模型")


def test_validate_cron_model_rejects_whitespace_only_model_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "gateway.common.config.get_config_raw",
        lambda: {"models": {"defaults": [_environment_model_entry("   ")]}},
    )

    with pytest.raises(
        ValueError,
        match=r"Configured model '默认模型'.*resolves to an empty value",
    ):
        validate_cron_model("默认模型")


def test_validate_cron_model_accepts_zen_free_model_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "gateway.common.config.get_model_config",
        lambda name, index=None: None,
    )
    monkeypatch.setattr("gateway.common.config.get_model_names", lambda: [])
    assert validate_cron_model(
        "deepseek-v4-flash-free",
        zen_entries=[_zen_free_entry()],
    ) == "deepseek-v4-flash-free"


def test_validate_cron_model_accepts_zen_free_model_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "gateway.common.config.get_model_config",
        lambda name, index=None: None,
    )
    monkeypatch.setattr("gateway.common.config.get_model_names", lambda: [])
    assert validate_cron_model(
        "DeepSeek V4 Flash",
        zen_entries=[_zen_free_entry()],
    ) == "deepseek-v4-flash-free"


def test_validate_cron_model_unknown_model_still_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "gateway.common.config.get_model_config",
        lambda name, index=None: None,
    )
    monkeypatch.setattr(
        "gateway.common.config.get_model_names",
        lambda: ["my-model"],
    )
    # 调用方带来的 Zen 行里没有这个名字 → 仍拒绝
    with pytest.raises(ValueError, match="Unknown model 'no-such-model'"):
        validate_cron_model("no-such-model", zen_entries=[_zen_free_entry()])


def test_validate_cron_model_zen_cache_empty_still_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """免费模型缓存为空（Zen 不可达/开关关闭）时，免费模型 id 仍被拒绝。"""
    monkeypatch.setattr(
        "gateway.common.config.get_model_config",
        lambda name, index=None: None,
    )
    monkeypatch.setattr("gateway.common.config.get_model_names", lambda: [])
    with pytest.raises(ValueError, match="Unknown model 'deepseek-v4-flash-free'"):
        validate_cron_model("deepseek-v4-flash-free")


# ---------------------------------------------------------------------------
# 登录送的免费模型（华为账号登录，Zen 停用后的唯一免费来源）
# ---------------------------------------------------------------------------


def _login_catalog(names: list[str]) -> list[LoginModel]:
    return [LoginModel(model_name=name, display_name=name) for name in names]


def test_validate_cron_model_accepts_login_free_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """登录免费模型命中登录模型目录即放行，返回目录里的规范名。"""
    monkeypatch.setattr(
        "gateway.common.config.get_model_config",
        lambda name, index=None: None,
    )
    monkeypatch.setattr("gateway.common.config.get_model_names", lambda: [])
    calls: list[int] = []

    def _counting_get_models(*args, **kwargs):
        calls.append(1)
        return _login_catalog(["GLM-5.2", "Kimi-K2.6"])

    monkeypatch.setattr(_model_catalog, "get_models", _counting_get_models)
    # CI-only 故障诊断探针：定位登录块内的失败段（本地全组合无法复现）
    from gateway.auth.login_credentials import bare_model_name as _bmn

    assert _bmn("GLM-5.2") == "GLM-5.2", "bare_model_name 变换了名字"
    assert validate_cron_model("GLM-5.2") == "GLM-5.2"
    assert calls, "get_models 从未被调用——登录块在循环前死亡"
    assert validate_cron_model("Kimi-K2.6") == "Kimi-K2.6"


def test_validate_cron_model_accepts_login_free_model_index_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """名字带 "#index" 通道侧全局序号后缀时取前半段匹配（与 passthrough 一致）。"""
    monkeypatch.setattr(
        "gateway.common.config.get_model_config",
        lambda name, index=None: None,
    )
    monkeypatch.setattr("gateway.common.config.get_model_names", lambda: [])
    monkeypatch.setattr(
        _model_catalog, "get_models",
        lambda *args, **kwargs: _login_catalog(["GLM-5.2"]),
    )
    assert validate_cron_model("GLM-5.2#3") == "GLM-5.2"


def test_validate_cron_model_login_catalog_empty_still_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未登录/活动未生效（登录模型目录为空）时，免费模型名仍被拒绝。"""
    monkeypatch.setattr(
        "gateway.common.config.get_model_config",
        lambda name, index=None: None,
    )
    monkeypatch.setattr("gateway.common.config.get_model_names", lambda: [])
    with pytest.raises(ValueError, match="Unknown model 'GLM-5.2'"):
        validate_cron_model("GLM-5.2")


def test_validate_cron_model_config_model_wins_over_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同名时自配模型优先（与 get_available_models 的去重规则一致）。"""
    monkeypatch.setattr(
        "gateway.common.config.get_model_config",
        lambda name, index=None: _user_model_entry(),
    )
    monkeypatch.setattr(
        _model_catalog, "get_models",
        lambda *args, **kwargs: _login_catalog(["my-model"]),
    )
    assert validate_cron_model("my-model") == "my-model"


def test_resolve_cron_model_reports_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_cron_model 额外返回来源标记，供 controller 决定是否绑定登录凭据。"""
    # 未指定模型
    assert resolve_cron_model(None) == (None, "")
    assert resolve_cron_model("   ") == (None, "")
    # 自配模型
    monkeypatch.setattr(
        "gateway.common.config.get_model_config",
        lambda name, index=None: _user_model_entry(),
    )
    assert resolve_cron_model("my-model") == ("my-model", "config")
    # 登录模型（自配查不到时）
    monkeypatch.setattr(
        "gateway.common.config.get_model_config",
        lambda name, index=None: None,
    )
    monkeypatch.setattr(
        _model_catalog, "get_models",
        lambda *args, **kwargs: _login_catalog(["GLM-5.2"]),
    )
    assert resolve_cron_model("GLM-5.2") == ("GLM-5.2", "login")
    # 同名时自配优先（来源仍是 config）
    monkeypatch.setattr(
        "gateway.common.config.get_model_config",
        lambda name, index=None: _user_model_entry(),
    )
    assert resolve_cron_model("my-model") == ("my-model", "config")
    # Zen 免费模型：调用方传入 AgentServer 返回的行
    monkeypatch.setattr(
        "gateway.common.config.get_model_config",
        lambda name, index=None: None,
    )
    assert resolve_cron_model(
        "deepseek-v4-flash-free",
        zen_entries=[_zen_free_entry()],
    ) == (
        "deepseek-v4-flash-free",
        "zen",
    )


# ---------------------------------------------------------------------------
# 会话级 MCP 选择（mcp）：类型规范化 + CronJob 序列化 round-trip
# ---------------------------------------------------------------------------


def test_normalize_cron_job_mcp_variants() -> None:
    assert normalize_cron_job_mcp(None) is None
    assert normalize_cron_job_mcp("not-a-list") is None
    assert normalize_cron_job_mcp(42) is None
    assert normalize_cron_job_mcp([]) is None
    # strip / 去空 / 去重 / 过滤非字符串元素
    assert normalize_cron_job_mcp([" a ", "b", "b", "", None, 123]) == ["a", "b"]
    assert normalize_cron_job_mcp(("x",)) == ["x"]


def _mcp_round_trip_job() -> CronJob:
    return CronJob(
        id="job-mcp-1",
        name="daily",
        enabled=True,
        cron_expr="0 0 9 * * ? *",
        timezone="Asia/Shanghai",
        description="hello",
        targets="web",
        mcp=["feishu-doc", "github"],
    )


def test_cron_job_mcp_round_trip() -> None:
    job = _mcp_round_trip_job()
    d = job.to_dict()
    assert d["mcp"] == ["feishu-doc", "github"]
    restored = CronJob.from_dict(d)
    assert restored.mcp == ["feishu-doc", "github"]


def test_cron_job_tenant_ids_round_trip() -> None:
    """三个身份没填时读写都是 default，表示个人版。旧文件没有这三个字段也能读。"""
    job = _mcp_round_trip_job()
    job.group_id = "g1"
    job.bot_id = ""
    job.user_id = ""
    saved = job.to_dict()
    assert saved["group_id"] == "g1"
    assert saved["bot_id"] == "default"
    assert saved["user_id"] == "default"
    assert "service_id" not in saved
    assert "agent_id" not in saved

    restored = CronJob.from_dict(saved)
    assert restored.group_id == "g1"
    assert restored.bot_id == "default"
    assert restored.user_id == "default"

    legacy = {k: v for k, v in saved.items() if k not in {"group_id", "bot_id", "user_id"}}
    old = CronJob.from_dict(legacy)
    assert old.group_id == "default"
    assert old.bot_id == "default"
    assert old.user_id == "default"


def test_cron_job_without_mcp_round_trip_keeps_none() -> None:
    """旧数据无 mcp 字段 → from_dict 兜底 None，行为与改造前一致。"""
    job = _mcp_round_trip_job()
    legacy = {k: v for k, v in job.to_dict().items() if k != "mcp"}
    assert "mcp" not in legacy
    assert CronJob.from_dict(legacy).mcp is None


@pytest.mark.asyncio
async def test_enterprise_cron_model_accepts_library_model_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """企业版核对走企业库 default_model，不认本机 your-model-name。"""
    from types import SimpleNamespace

    from gateway.cron.enterprise_model import resolve_enterprise_cron_model
    from gateway.enterprise.config.effective.schemas import TemplateRefSlot

    async def _fake_load(request, slots):
        assert TemplateRefSlot.DEFAULT_MODEL in slots
        return SimpleNamespace(
            models={
                TemplateRefSlot.DEFAULT_MODEL.value: [
                    {
                        "model_id": "GLM-5.2",
                        "template_name": "我自己用的",
                        "api_base": "https://example.invalid",
                        "api_key": "secret",
                    }
                ]
            }
        )

    monkeypatch.setattr(
        "gateway.enterprise.config.effective.loader.load_effective_enterprise_config",
        _fake_load,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_model_names",
        lambda: ["your-model-name"],
    )
    assert (
        await resolve_enterprise_cron_model(
            "GLM-5.2",
            group_id="g1",
            bot_id="b1",
            user_id="u1",
        )
        == "GLM-5.2"
    )
    assert (
        await resolve_enterprise_cron_model(
            "我自己用的",
            group_id="g1",
            bot_id="b1",
            user_id="u1",
        )
        == "GLM-5.2"
    )
    assert (
        await resolve_enterprise_cron_model(
            None,
            group_id="g1",
            bot_id="b1",
            user_id="u1",
        )
        is None
    )


@pytest.mark.asyncio
async def test_enterprise_cron_model_rejects_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from gateway.cron.enterprise_model import resolve_enterprise_cron_model
    from gateway.enterprise.config.effective.schemas import TemplateRefSlot

    async def _fake_load(request, slots):
        return SimpleNamespace(
            models={
                TemplateRefSlot.DEFAULT_MODEL.value: [
                    {"model_id": "GLM-5.2", "template_name": "我自己用的"}
                ]
            }
        )

    monkeypatch.setattr(
        "gateway.enterprise.config.effective.loader.load_effective_enterprise_config",
        _fake_load,
    )
    with pytest.raises(ValueError, match="Unknown model 'no-such-model'"):
        await resolve_enterprise_cron_model(
            "no-such-model",
            group_id="g1",
            bot_id="b1",
            user_id="u1",
        )


def test_agent_cron_model_enterprise_passes_session_model_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """企业版工具侧不读本机名单，把会话模型原样交给 Gateway 核对。"""
    from jiuwenswarm.agents.harness.common.tools.cron.cron_tools import (
        resolve_agent_cron_model,
    )

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_model_names",
        lambda: ["your-model-name"],
    )
    assert resolve_agent_cron_model("GLM-5.2") == "GLM-5.2"
    assert resolve_agent_cron_model("  ") is None
