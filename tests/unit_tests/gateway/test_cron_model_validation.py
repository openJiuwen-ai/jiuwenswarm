# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for cron job model validation, incl. free-model fallbacks.

The frontend lets the user pick a free model for a cron job.
``validate_cron_model`` resolves configured models and login-catalog models.
Zen ids resolve only when the caller passes rows already fetched from
AgentServer (``zen_entries``). An empty list still rejects those names.
"""

from __future__ import annotations

import pytest

from agent_gateway.auth import model_catalog as _model_catalog
from agent_gateway.auth.model_catalog import LoginModel
from agent_gateway.cron.models import (
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


# ---------------------------------------------------------------------------
# 登录送的免费模型（华为账号登录，Zen 停用后的唯一免费来源）
# ---------------------------------------------------------------------------


def _login_catalog(names: list[str]) -> list[LoginModel]:
    return [LoginModel(model_name=name, display_name=name) for name in names]


# ---------------------------------------------------------------------------
# 会话级 MCP 选择（mcp）：类型规范化 + CronJob 序列化 round-trip
# ---------------------------------------------------------------------------


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
