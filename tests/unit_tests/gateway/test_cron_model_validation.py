# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for cron job model validation, incl. Opencode Zen free-model fallback.

The frontend appends Zen free models (in-memory only, never written to
config.yaml) to ``models.list`` and lets the user pick one for a cron job.
``validate_cron_model`` must resolve such an id/alias so job creation does not
fail with ``Unknown model``, while still rejecting genuinely unknown models.
"""

from __future__ import annotations

import pytest

from jiuwenswarm.gateway.cron.models import (
    CronJob,
    CronTargetChannel,
    normalize_cron_job_mcp,
    validate_cron_model,
)


@pytest.fixture(autouse=True)
def _no_zen_free_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """By default no Zen free models are cached (clean baseline)."""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.opencode_zen.get_zen_free_model_entries",
        lambda: [],
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
        "jiuwenswarm.common.config.get_model_config",
        fake_get_model_config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_model_names", lambda: ["我的模型"]
    )
    assert validate_cron_model("my-model") == "my-model"
    # alias 也解析为 canonical model_name
    assert validate_cron_model("我的模型") == "my-model"


def test_validate_cron_model_resolves_environment_model_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODEL_NAME", "deployed-model")
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config_raw",
        lambda: {"models": {"defaults": [_environment_model_entry()]}},
    )

    assert validate_cron_model("deployed-model") == "deployed-model"
    assert validate_cron_model("默认模型") == "deployed-model"


def test_validate_cron_model_uses_environment_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config_raw",
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
        "jiuwenswarm.common.config.get_config_raw",
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
        "jiuwenswarm.common.config.get_config_raw",
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
        "jiuwenswarm.common.config.get_model_config",
        lambda name, index=None: None,
    )
    monkeypatch.setattr("jiuwenswarm.common.config.get_model_names", lambda: [])
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.opencode_zen.get_zen_free_model_entries",
        lambda: [_zen_free_entry()],
    )
    assert validate_cron_model("deepseek-v4-flash-free") == "deepseek-v4-flash-free"


def test_validate_cron_model_accepts_zen_free_model_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_model_config",
        lambda name, index=None: None,
    )
    monkeypatch.setattr("jiuwenswarm.common.config.get_model_names", lambda: [])
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.opencode_zen.get_zen_free_model_entries",
        lambda: [_zen_free_entry()],
    )
    assert validate_cron_model("DeepSeek V4 Flash") == "deepseek-v4-flash-free"


def test_validate_cron_model_unknown_model_still_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_model_config",
        lambda name, index=None: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_model_names",
        lambda: ["my-model"],
    )
    # 免费模型缓存存在，但请求的模型不在其中 → 仍拒绝
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.opencode_zen.get_zen_free_model_entries",
        lambda: [_zen_free_entry()],
    )
    with pytest.raises(ValueError, match="Unknown model 'no-such-model'"):
        validate_cron_model("no-such-model")


def test_validate_cron_model_zen_cache_empty_still_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """免费模型缓存为空（Zen 不可达/开关关闭）时，免费模型 id 仍被拒绝。"""
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_model_config",
        lambda name, index=None: None,
    )
    monkeypatch.setattr("jiuwenswarm.common.config.get_model_names", lambda: [])
    with pytest.raises(ValueError, match="Unknown model 'deepseek-v4-flash-free'"):
        validate_cron_model("deepseek-v4-flash-free")


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


def test_cron_job_without_mcp_round_trip_keeps_none() -> None:
    """旧数据无 mcp 字段 → from_dict 兜底 None，行为与改造前一致。"""
    job = _mcp_round_trip_job()
    legacy = {k: v for k, v in job.to_dict().items() if k != "mcp"}
    assert "mcp" not in legacy
    assert CronJob.from_dict(legacy).mcp is None


# ---------------------------------------------------------------------------
# targets 校验：未知推送频道必须报错，不得静默改写成 web
# ---------------------------------------------------------------------------


def _job_dict(targets: str) -> dict:
    return {
        "id": "job-targets-1",
        "name": "daily",
        "enabled": True,
        "cron_expr": "0 0 9 * * ? *",
        "timezone": "Asia/Shanghai",
        "description": "hello",
        "targets": targets,
    }


@pytest.mark.parametrize("targets", ["telegram", "slack", "discord", "no-such-channel"])
def test_cron_job_from_dict_rejects_unknown_target(targets: str) -> None:
    """未知频道之前被改写成 web：任务创建成功，结果却推到 Web 面板。"""
    with pytest.raises(ValueError, match=f"Invalid targets '{targets}'"):
        CronJob.from_dict(_job_dict(targets))


def test_cron_job_from_dict_error_lists_accepted_targets() -> None:
    with pytest.raises(ValueError) as exc:
        CronJob.from_dict(_job_dict("telegram"))
    message = str(exc.value)
    for channel in CronTargetChannel:
        assert channel.value in message
    assert "feishu_enterprise:<app_id>" in message


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("web", "web"),
        ("WEB", "web"),
        ("dingtalk", "dingtalk"),
        ("feishu_enterprise:cli_123", "feishu_enterprise:cli_123"),
        ("feishu_enterprise:cli_123:chat:abc", "feishu_enterprise:cli_123"),
    ],
)
def test_cron_job_from_dict_keeps_accepted_targets(raw: str, expected: str) -> None:
    assert CronJob.from_dict(_job_dict(raw)).targets == expected


def test_cron_job_store_create_rejects_unknown_target(tmp_path) -> None:
    """store.create_job 经 build_job round-trip 校验，未知频道不得落盘。"""
    import asyncio

    from jiuwenswarm.runtime.cron.store import FileCronJobStore

    path = tmp_path / "cron_jobs.json"
    store = FileCronJobStore(path=path)
    with pytest.raises(ValueError, match="Invalid targets 'telegram'"):
        asyncio.run(
            store.create_job(
                name="daily",
                cron_expr="0 0 9 * * ? *",
                timezone="Asia/Shanghai",
                description="hello",
                targets="telegram",
            )
        )
    assert not path.exists()
