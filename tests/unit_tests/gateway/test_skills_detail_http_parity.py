# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""技能详情只读 RPC（文件预览 / 版本列表）在企业 HTTP 链路上的一致性。

企业版默认 WEB_TRANSPORT=http：前端 ROUTES → Gateway mapped → pod_rest_map
→ AgentServer dispatch，且须通过企业 skills 白名单。漏任一环会前端直接
METHOD_NOT_FOUND 或 Gateway FORBIDDEN，且日志里看不到南向请求。
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

import pytest

from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.extensions.runtime_management_extension.pod_rest_map import (
    REST_ROUTES,
    assemble_rest_request,
)

_ROOT = Path(__file__).resolve().parents[3]
_INVOKE = _ROOT / "gateway" / "channel_manager" / "web" / "invoke.py"
_MAPPED = _ROOT / "gateway" / "channel_manager" / "web" / "http_routes" / "mapped.py"
_DISPATCH = _ROOT / "jiuwenswarm" / "server" / "dispatch.py"
_WEB_HTTP = (
    _ROOT
    / "jiuwenswarm"
    / "channels"
    / "web"
    / "frontend"
    / "src"
    / "services"
    / "webHttpClient.ts"
)

# 技能详情页「只读」RPC：企业版必须可走 HTTP，且不得被 write-forbidden 拦住。
_SKILL_DETAIL_READ_METHODS: dict[str, tuple[str, str]] = {
    "skills.files.list": ("GET", "/skills/{name}/files"),
    "skills.files.get": ("GET", "/skills/{name}/files/content"),
    "skills.versions.list": ("GET", "/skills/{name}/versions"),
}

_ENUM_BY_METHOD = {
    "skills.files.list": "SKILLS_FILES_LIST",
    "skills.files.get": "SKILLS_FILES_GET",
    "skills.versions.list": "SKILLS_VERSIONS_LIST",
}


def _load_mapped():
    spec = importlib.util.spec_from_file_location("skills_detail_mapped_routes", _MAPPED)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _enterprise_skill_allowed() -> set[str]:
    tree = ast.parse(_INVOKE.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "_ENTERPRISE_SKILL_ALLOWED" for t in node.targets):
            continue
        assert isinstance(node.value, ast.Call)
        assert node.value.args and isinstance(node.value.args[0], (ast.Set, ast.Tuple, ast.List))
        out: set[str] = set()
        for elt in node.value.args[0].elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                out.add(elt.value)
        return out
    raise AssertionError("_ENTERPRISE_SKILL_ALLOWED not found in invoke.py")


def _frontend_route_paths() -> dict[str, str]:
    text = _WEB_HTTP.read_text(encoding="utf-8")
    # 'skills.files.list': { verb: 'GET', path: '/skills/{name}/files', kind: 'unary' },
    pattern = re.compile(
        r"'(?P<method>skills\.(?:files\.(?:list|get)|versions\.list))'\s*:\s*\{[^}]*?path:\s*'(?P<path>[^']+)'",
        re.DOTALL,
    )
    return {m.group("method"): m.group("path") for m in pattern.finditer(text)}


@pytest.mark.parametrize("method,expected", sorted(_SKILL_DETAIL_READ_METHODS.items()))
def test_skill_detail_read_methods_have_frontend_http_routes(
    method: str, expected: tuple[str, str]
) -> None:
    paths = _frontend_route_paths()
    assert method in paths, f"webHttpClient.ts missing ROUTES entry for {method}"
    assert paths[method] == expected[1]


@pytest.mark.parametrize("method,expected", sorted(_SKILL_DETAIL_READ_METHODS.items()))
def test_skill_detail_read_methods_have_gateway_mapped_routes(
    method: str, expected: tuple[str, str]
) -> None:
    mapped = _load_mapped()
    verb, path = expected
    matched = [
        r
        for r in mapped.MAPPED_ROUTES
        if r.rpc_method == method and r.http_method == verb and r.path == path
    ]
    assert matched, f"mapped.py missing {verb} {path} → {method}"


@pytest.mark.parametrize("method,expected", sorted(_SKILL_DETAIL_READ_METHODS.items()))
def test_skill_detail_read_methods_have_pod_rest_routes(
    method: str, expected: tuple[str, str]
) -> None:
    assert REST_ROUTES.get(method) == expected


@pytest.mark.parametrize("method", sorted(_SKILL_DETAIL_READ_METHODS))
def test_skill_detail_read_methods_registered_in_dispatch(method: str) -> None:
    enum_name = _ENUM_BY_METHOD[method]
    text = _DISPATCH.read_text(encoding="utf-8")
    needle = f"ReqMethod.{enum_name}: handle_skills_request"
    assert needle in text, f"dispatch.py missing {needle}"


@pytest.mark.parametrize("method", sorted(_SKILL_DETAIL_READ_METHODS))
def test_skill_detail_read_methods_allowed_in_enterprise(method: str) -> None:
    assert method in _enterprise_skill_allowed()


def test_skills_files_and_versions_assemble_dedicated_urls() -> None:
    files = assemble_rest_request(
        E2AEnvelope(
            request_id="req-1",
            channel="web",
            method="skills.files.list",
            session_id="sess-1",
            params={"name": "demo-skill", "session_id": "sess-1"},
        ),
        base_url="http://10.0.0.8:8766",
    )
    assert files.verb == "GET"
    assert files.url == "http://10.0.0.8:8766/api/v1/skills/demo-skill/files"
    assert files.used_rpc_fallback is False

    preview = assemble_rest_request(
        E2AEnvelope(
            request_id="req-2",
            channel="web",
            method="skills.files.get",
            session_id="sess-1",
            params={"name": "demo-skill", "path": "SKILL.md", "session_id": "sess-1"},
        ),
        base_url="http://10.0.0.8:8766",
    )
    assert preview.verb == "GET"
    assert preview.url == "http://10.0.0.8:8766/api/v1/skills/demo-skill/files/content"
    assert preview.query is not None
    assert preview.query["path"] == "SKILL.md"

    versions = assemble_rest_request(
        E2AEnvelope(
            request_id="req-3",
            channel="web",
            method="skills.versions.list",
            session_id="sess-1",
            params={"name": "demo-skill", "session_id": "sess-1"},
        ),
        base_url="http://10.0.0.8:8766",
    )
    assert versions.verb == "GET"
    assert versions.url == "http://10.0.0.8:8766/api/v1/skills/demo-skill/versions"
    assert versions.used_rpc_fallback is False
