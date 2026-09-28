# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""登录免费模型定时任务的凭据绑定（``CronJob.credential_ref``）。

两套用户体系独立的回归：项目路由 user_id（Web URL/localStorage）≠ 华为账号
openid，登录凭据只能来自创建连接的服务端登录会话（``_auth_session``）。
绑定即"所选模型是登录模型"的标记——自配模型即使与登录模型同名也不注入凭据。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.auth.model_catalog import LoginModel
from jiuwenswarm.gateway.cron.controller import CronController
from jiuwenswarm.gateway.cron.store import CronJobStore

_LOGIN_REF = "0123456789abcdef0123456789abcdef"
_AUTH_SESSION = "auth-session-1"


def _config_entry(model_name: str) -> dict:
    return {
        "model_client_config": {
            "api_base": "https://api.example.com/v1",
            "api_key": "sk-self",
            "model_name": model_name,
            "client_provider": "OpenAI",
        },
        "is_default": True,
        "alias": "",
    }


def _patch_model_sources(
    monkeypatch: pytest.MonkeyPatch,
    *,
    login_models: list[str],
    config_models: list[str] | None = None,
) -> None:
    """登录模型目录 + 自配模型查找的内存桩（测试不依赖本机登录/配置状态）。"""
    monkeypatch.setattr(
        "jiuwenswarm.common.auth.model_catalog.get_models",
        lambda *a, **k: [
            LoginModel(model_name=name, display_name=name) for name in login_models
        ],
    )
    if config_models:
        entries = {name: _config_entry(name) for name in config_models}

        def fake_get_model_config(name, index=None):
            return entries.get(name)

        monkeypatch.setattr(
            "jiuwenswarm.common.config.get_model_config", fake_get_model_config
        )
        monkeypatch.setattr(
            "jiuwenswarm.common.config.get_model_names", lambda: list(config_models)
        )
    else:
        monkeypatch.setattr(
            "jiuwenswarm.common.config.get_model_config",
            lambda name, index=None: None,
        )
        monkeypatch.setattr("jiuwenswarm.common.config.get_model_names", lambda: [])


def _patch_login_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    logged_in: bool = True,
    user_id: str = "openid-1",
) -> None:
    """服务端登录会话（华为账号，与路由 user_id 独立）的内存桩。"""
    from jiuwenswarm.common.auth.service import ModelAuthRequired

    def fake_live_session(session_id, allow_refresh=True):
        if not logged_in or not session_id:
            raise ModelAuthRequired("登录已过期，请重新登录", "session_expired")
        return SimpleNamespace(user_id=user_id)

    monkeypatch.setattr(
        "jiuwenswarm.common.auth.service.live_session", fake_live_session
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.auth.login_credentials.credential_ref_for_user",
        lambda uid: _LOGIN_REF,
    )


def _patch_project_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """项目绑定解析桩：create/update 不依赖 Gateway 本地项目表。"""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.resolve_cron_project_binding",
        lambda raw_pid, project_dir, work_mode: SimpleNamespace(
            project_id="p1", work_mode="work", error=None
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.resolve_cron_job_patch",
        lambda patch, **kwargs: None,
    )


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
async def test_create_job_with_login_model_binds_credential_ref(
    tmp_path, monkeypatch
):
    """登录免费模型创建 → 绑定创建连接的华为账号句柄，随任务持久化。"""
    controller, store = _make_controller(tmp_path)
    _patch_model_sources(monkeypatch, login_models=["GLM-5.2"])
    _patch_login_session(monkeypatch)
    _patch_project_binding(monkeypatch)

    job = await controller.create_job(
        _job_params(model_name="GLM-5.2", _auth_session=_AUTH_SESSION)
    )

    assert job["model_name"] == "GLM-5.2"
    assert job["credential_ref"] == _LOGIN_REF
    # _auth_session 是服务端专有键，用后即弃，不落任务数据
    assert "_auth_session" not in job
    stored = await store.get_job(job["id"])
    assert stored.credential_ref == _LOGIN_REF


@pytest.mark.asyncio
async def test_create_job_login_model_without_session_rejected(
    tmp_path, monkeypatch
):
    """无可用登录会话 → 创建即拒绝（明确文案），不留一个到点必失败的任务。"""
    controller, store = _make_controller(tmp_path)
    _patch_model_sources(monkeypatch, login_models=["GLM-5.2"])
    _patch_login_session(monkeypatch, logged_in=False)
    _patch_project_binding(monkeypatch)

    with pytest.raises(ValueError, match="该模型需要登录华为账号后使用"):
        await controller.create_job(_job_params(model_name="GLM-5.2"))

    assert await store.list_jobs() == []


@pytest.mark.asyncio
async def test_create_job_same_name_config_model_not_bound(tmp_path, monkeypatch):
    """P2 回归：自配模型与登录模型同名 → 走自配来源，不绑定、不要求登录。"""
    controller, store = _make_controller(tmp_path)
    _patch_model_sources(
        monkeypatch, login_models=["GLM-5.2"], config_models=["GLM-5.2"]
    )
    # 未登录也不影响自配模型创建
    _patch_login_session(monkeypatch, logged_in=False)
    _patch_project_binding(monkeypatch)

    job = await controller.create_job(_job_params(model_name="GLM-5.2"))

    assert job["model_name"] == "GLM-5.2"
    # 未绑定不输出该字段
    assert "credential_ref" not in job
    stored = await store.get_job(job["id"])
    assert stored.credential_ref == ""


@pytest.mark.asyncio
async def test_update_job_rebinds_on_model_switch(tmp_path, monkeypatch):
    """改选登录模型 → 绑定；换回自配/清除模型 → 解绑。"""
    controller, store = _make_controller(tmp_path)
    _patch_model_sources(
        monkeypatch, login_models=["GLM-5.2"], config_models=["my-model"]
    )
    _patch_login_session(monkeypatch)
    _patch_project_binding(monkeypatch)

    created = await controller.create_job(
        _job_params(model_name="my-model", _auth_session=_AUTH_SESSION)
    )
    job_id = created["id"]
    assert created.get("credential_ref", "") == ""

    # 改选登录模型 → 绑定当前连接的华为账号
    updated = await controller.update_job(
        job_id, {"model_name": "GLM-5.2", "_auth_session": _AUTH_SESSION}
    )
    assert updated["credential_ref"] == _LOGIN_REF

    # 换回自配模型 → 解绑
    updated2 = await controller.update_job(
        job_id, {"model_name": "my-model", "_auth_session": _AUTH_SESSION}
    )
    assert updated2.get("credential_ref", "") == ""

    # 清除模型 → 保持解绑
    updated3 = await controller.update_job(job_id, {"model_name": None})
    assert updated3.get("credential_ref", "") == ""
    assert "model_name" not in updated3
    stored = await store.get_job(job_id)
    assert stored.credential_ref == ""
    assert stored.model_name is None


@pytest.mark.asyncio
async def test_update_job_without_model_change_keeps_binding(tmp_path, monkeypatch):
    """patch 不含 model_name（如仅改名/启停）→ 绑定保持不变。"""
    controller, _ = _make_controller(tmp_path)
    _patch_model_sources(monkeypatch, login_models=["GLM-5.2"])
    _patch_login_session(monkeypatch)
    _patch_project_binding(monkeypatch)

    created = await controller.create_job(
        _job_params(name="old-name", model_name="GLM-5.2", _auth_session=_AUTH_SESSION)
    )
    job_id = created["id"]
    assert created["credential_ref"] == _LOGIN_REF

    updated = await controller.update_job(job_id, {"name": "new-name"})
    assert updated["name"] == "new-name"
    assert updated["credential_ref"] == _LOGIN_REF
