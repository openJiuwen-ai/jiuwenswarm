# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""专家包专用网址只在企业版挂出，AgentServer 不读取 Gateway 企业包。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI

from jiuwenswarm.extensions.runtime_management_extension.pod_rest_map import (
    declared_http_routes,
)
from jiuwenswarm.server.agent_http_routes import _register_enterprise_expert_routes


def test_personal_declared_routes_omit_agent_templates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    methods = {method for method, _verb, _path in declared_http_routes()}
    assert "agent_templates.list" not in methods


def test_enterprise_agent_server_registers_template_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    app = FastAPI()
    _register_enterprise_expert_routes(app, app_services=None)  # type: ignore[arg-type]
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/api/v1/agent-templates" in paths


def test_agent_server_expert_dispatch_does_not_import_gateway_enterprise() -> None:
    source = (
        Path(__file__).resolve().parents[3]
        / "jiuwenswarm/enterprise/capabilities/experts/http_dispatch.py"
    ).read_text(encoding="utf-8")
    assert "gateway.enterprise" not in source
