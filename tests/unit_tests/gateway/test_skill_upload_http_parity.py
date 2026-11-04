# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业版 skills.import_upload 的 HTTP 链路一致性。

企业版默认 WEB_TRANSPORT=http：前端 ROUTES → Gateway mapped → pod_rest_map
→ AgentServer dispatch。漏任一环，上传会 METHOD_NOT_FOUND / FORBIDDEN。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

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
_POD_REST_MAP = (
    _ROOT
    / "jiuwenswarm"
    / "extensions"
    / "runtime_management_extension"
    / "pod_rest_map.py"
)


def test_import_upload_registered_across_http_chain() -> None:
    pod_rest_map = _POD_REST_MAP.read_text(encoding="utf-8")
    assert '("skills.import_upload", "POST", "/skills/import-upload")' in pod_rest_map

    mapped = _MAPPED.read_text(encoding="utf-8")
    assert '"skills.import_upload"' in mapped
    assert '"/skills/actions/import-upload"' in mapped

    web_http = _WEB_HTTP.read_text(encoding="utf-8")
    assert "'skills.import_upload'" in web_http
    assert "'/skills/actions/import-upload'" in web_http

    dispatch = _DISPATCH.read_text(encoding="utf-8")
    assert "ReqMethod.SKILLS_IMPORT_UPLOAD: handle_skills_request" in dispatch

    invoke = _INVOKE.read_text(encoding="utf-8")
    assert '"skills.import_upload"' in invoke


def test_guard_only_covers_import_upload() -> None:
    from gateway.enterprise.capabilities.skill_upload import guard as guard_mod

    assert guard_mod._GUARDED_METHOD == "skills.import_upload"
