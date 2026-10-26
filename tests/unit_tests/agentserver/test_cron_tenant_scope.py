# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for per-tenant CronTools path + AgentCronRegistry."""

from __future__ import annotations

from jiuwenswarm.agents.harness.common.tools.cron.cron_tools import (
    CronTools,
    resolve_cron_jobs_path,
)
from jiuwenswarm.server.runtime.cron_local_runtime import AgentCronRegistry


def test_resolve_cron_jobs_path_isolates_tenants(tmp_path, monkeypatch):
    # 多租户分桶是企业版语义；个人版固定 service_default/agent_default。
    # cron_tools 模块级 import 绑定了 get_multi_tenant_user_workspace_dir，
    # 需 patch utils 内的 is_enterprise/get_user_workspace_dir 让旧函数对象走分桶。
    monkeypatch.setattr("jiuwenswarm.common.utils.is_enterprise", lambda: True)
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path
    )
    office = resolve_cron_jobs_path("default", "office", workspace_key="office")
    default = resolve_cron_jobs_path("default", "default", workspace_key="default")
    assert office != default
    assert office.name == "cron_jobs.json"
    assert "workspace_office" in str(office)
    assert "workspace_default" in str(default)


def test_cron_tools_store_path_follows_tenant(tmp_path, monkeypatch):
    monkeypatch.setattr("jiuwenswarm.common.utils.is_enterprise", lambda: True)
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path
    )
    AgentCronRegistry.reset_for_tests()
    tools = CronTools(service_id="svc", agent_id="office", workspace_key="office")
    assert "workspace_office" in str(tools._local_store.path)
    assert tools._service_id == "svc"
    assert tools._agent_id == "office"
    assert tools._workspace_key == "office"


def test_agent_cron_registry_get_or_create_shares_instance():
    AgentCronRegistry.reset_for_tests()
    a = AgentCronRegistry.get_or_create(
        "s1", "a1", factory=lambda: CronTools(service_id="s1", agent_id="a1")
    )
    b = AgentCronRegistry.get_or_create(
        "s1", "a1", factory=lambda: CronTools(service_id="s1", agent_id="a1")
    )
    assert a is b
