# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentServer-owned admission rules for business methods."""

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.enterprise import is_enterprise
from jiuwenswarm.server.control.responses import build_error_response


_ENTERPRISE_BLOCKED_EXACT = frozenset({
    "config.set", "config.save_all", "models.replace_all", "models.save",
    "models.remove", "models.set_active", "path.set", "updater.download",
    "updater.upgrade", "updater.reset_source", "updater.set_conf",
})
_ENTERPRISE_BLOCKED_PREFIXES = ("agents.", "teams.", "extensions.", "plugins.", "mcp.server.", "a2a.")
_ENTERPRISE_SKILL_ALLOWED = frozenset({
    "skills.list", "skills.get", "skills.toggle",
    "skills.files.list", "skills.files.get", "skills.versions.list",
    "skills.source.providers", "skills.source.search", "skills.source.install",
    "skills.uninstall", "skills.updates.check", "skills.update", "skills.import_local",
    "skills.enterprise.list", "skills.enterprise.install", "skills.enterprise.uninstall",
    "skills.enterprise.source.providers", "skills.enterprise.source.search",
})
_SWARMFLOW_CONTROL_METHODS = frozenset({
    ReqMethod.SWARMFLOW_PAUSE, ReqMethod.SWARMFLOW_RESUME, ReqMethod.SWARMFLOW_STOP,
})


def _enterprise_write_forbidden(method: str) -> bool:
    if method in _ENTERPRISE_BLOCKED_EXACT or method.startswith(_ENTERPRISE_BLOCKED_PREFIXES):
        return True
    if method.startswith("channel.") and (method.endswith(".set_conf") or method.endswith(".unbind")):
        return True
    return method.startswith("skills.") and method not in _ENTERPRISE_SKILL_ALLOWED


def _swarmflow_error(params: dict) -> str | None:
    run_id = params.get("run_id")
    workflow_run_id = params.get("workflow_run_id")
    if run_id not in (None, "") and workflow_run_id not in (None, "") and run_id != workflow_run_id:
        return "run_id and workflow_run_id must match"
    value = run_id or workflow_run_id
    if not isinstance(value, str):
        return "run_id must be a string"
    if not value or value != value.strip():
        return "invalid run_id"
    if len(value) > 256:
        return "run_id exceeds maximum length 256"
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return "invalid run_id"
    return None


def request_policy_error(request: AgentRequest, *, web_http: bool = False) -> AgentResponse | None:
    """Validate business rules while preserving the original Web HTTP guard scope."""
    method = request.req_method or ""
    metadata = request.metadata if isinstance(request.metadata, dict) else {}
    web_http = web_http or metadata.get("transport") == "web-http"
    if request.channel_id == "web" and web_http and is_enterprise() and _enterprise_write_forbidden(method):
        return build_error_response(request, "Enterprise configuration is managed centrally", code="FORBIDDEN")
    if method in _SWARMFLOW_CONTROL_METHODS:
        params = request.params if isinstance(request.params, dict) else {}
        error = _swarmflow_error(params)
        if error is not None:
            return build_error_response(request, error, code="BAD_REQUEST")
    return None
