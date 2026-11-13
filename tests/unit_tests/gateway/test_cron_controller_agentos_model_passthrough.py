# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentOS 下 Gateway 无本地模型目录时的 cron 模型名校验。

AgentOS 把对话模型放在各用户沙箱的 ``models.agentos``，Gateway 自己的
``models.defaults`` 只剩 ``${MODEL_NAME}`` 占位符且 ``.env`` 为空。网关侧仍按
自身配置校验模型名会误报 ``Unknown model ... (no models configured)``，把沙箱
已校验通过的任务挡在创建/修改环节。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common import config as config_mod
from jiuwenswarm.gateway.cron.controller import CronController
from jiuwenswarm.gateway.cron.store import CronJobStore


def _entry(model_name: str) -> dict:
    return {
        "model_client_config": {
            "model_name": model_name,
            "api_base": "https://example.com/v1",
            "api_key": "sk-test",
            "client_provider": "OpenAI",
        },
        "model_config_obj": {},
    }


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "MODEL_NAME",
        "MODEL_ALIAS",
        "API_BASE",
        "API_KEY",
        "MODEL_PROVIDER",
        "JIUWENSWARM_RUNTIME_PROFILE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "jiuwenswarm.common.auth.model_catalog.get_models",
        lambda *args, **kwargs: [],
    )
    # 项目绑定解析桩：create/update 不依赖 Gateway 本地项目表。
    monkeypatch.setattr(
        "jiuwenswarm.gateway.embedded.runtime.session.project_store.resolve_cron_project_binding",
        lambda raw_pid, project_dir, work_mode: SimpleNamespace(
            project_id="p1", work_mode="work", error=None
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.embedded.runtime.session.project_store.resolve_cron_job_patch",
        lambda patch, **kwargs: None,
    )


def _patch_config(monkeypatch: pytest.MonkeyPatch, models: dict) -> None:
    raw = {"models": models}
    monkeypatch.setattr(config_mod, "get_config_raw", lambda: raw)
    monkeypatch.setattr(config_mod, "get_config", lambda *args, **kwargs: raw)


def _make_controller(tmp_path) -> tuple[CronController, CronJobStore]:
    store = CronJobStore(path=tmp_path / "cron_jobs.json")
    scheduler = SimpleNamespace(
        reload=AsyncMock(),
        project_execution_allowed=AsyncMock(return_value=True),
    )
    return CronController(store=store, scheduler=scheduler), store


def _job_params(**overrides) -> dict:
    params: dict = {
        "name": "job",
        "cron_expr": "0 0 9 * * ? *",
        "timezone": "Asia/Shanghai",
        "description": "reminder",
        "targets": "web",
    }
    params.update(overrides)
    return params


@pytest.mark.asyncio
async def test_create_accepts_name_when_gateway_has_no_models(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AgentOS：模型配置在用户侧，透传沙箱已校验的模型名。"""
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", "agentos")
    _patch_config(monkeypatch, {"defaults": [_entry("${MODEL_NAME}")]})
    controller, store = _make_controller(tmp_path)

    job = await controller.create_job(_job_params(model_name="GLM-5.2"))

    assert job["model_name"] == "GLM-5.2"
    assert not job.get("credential_ref")
    stored = await store.get_job(job["id"])
    assert stored is not None
    assert stored.model_name == "GLM-5.2"


@pytest.mark.asyncio
async def test_update_accepts_name_when_gateway_has_no_models(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """修改任务里的模型名同样透传，不因 Gateway 无模型配置而失败。"""
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", "agentos")
    _patch_config(monkeypatch, {"defaults": [_entry("${MODEL_NAME}")]})
    controller, store = _make_controller(tmp_path)
    job = await controller.create_job(_job_params())

    updated = await controller.update_job(job["id"], {"model_name": "GLM-5.2"})

    assert updated["model_name"] == "GLM-5.2"
    stored = await store.get_job(job["id"])
    assert stored is not None
    assert stored.model_name == "GLM-5.2"


@pytest.mark.asyncio
async def test_unknown_name_still_rejected_outside_agentos(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非 AgentOS 部署行为不变：模型名必须能在本地配置中解析。"""
    _patch_config(monkeypatch, {"defaults": [_entry("${MODEL_NAME}")]})
    controller, _ = _make_controller(tmp_path)

    with pytest.raises(ValueError, match="Unknown model"):
        await controller.create_job(_job_params(model_name="GLM-5.2"))
