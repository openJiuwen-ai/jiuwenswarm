"""企业技能 HTTP 路由要落到 SkillManager handler，而不是 METHOD_NOT_FOUND。"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import sys
from pathlib import Path

from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

_ROOT = Path(__file__).resolve().parents[3]


def _mapped_routes():
    path = _ROOT / "gateway/channel_manager/web/http_routes/mapped.py"
    spec = importlib.util.spec_from_file_location("enterprise_skill_mapped_routes", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.MAPPED_ROUTES


def _assigned_handler(source: Path, enum_name: str) -> str:
    text = source.read_text(encoding="utf-8")
    needle = f"ReqMethod.{enum_name}:"
    for line in text.splitlines():
        if needle in line and "handle_" in line:
            return line.split(":", 1)[1].strip().strip('",')
    raise AssertionError(f"{enum_name} is not in _SKILL_ROUTES")


def _forward_sets_contain(method: str) -> bool:
    source = _ROOT / "gateway/channel_manager/web/app_web_handlers.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    found = {name: False for name in ("_FORWARD_REQ_METHODS", "_FORWARD_NO_LOCAL_HANDLER_METHODS")}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in found:
                blob = ast.get_source_segment(source.read_text(encoding="utf-8"), node.value) or ""
                found[target.id] = method in blob
    return all(found.values())


def test_enterprise_install_http_route_reaches_web_install_handler(tmp_path) -> None:
    method = "skills.enterprise.install"
    interface = _ROOT / "jiuwenswarm/server/runtime/agent_adapter/interface.py"
    handler_name = _assigned_handler(interface, "SKILLS_ENTERPRISE_INSTALL")
    from jiuwenswarm.common.schema.message import ReqMethod
    assert ReqMethod.SKILLS_ENTERPRISE_INSTALL.value == method
    assert handler_name == "handle_skills_web_install"
    assert _forward_sets_contain(method)
    matched = [
        route for route in _mapped_routes()
        if route.rpc_method == method and route.path == "/skills/enterprise/actions/install"
    ]
    assert matched and matched[0].http_method == "POST"

    manager = SkillManager(workspace_dir=str(tmp_path), service_id="svc", agent_id="agent")
    seen: dict[str, object] = {}

    async def _install(params: dict) -> dict:
        seen["params"] = params
        return {"success": True, "skill": {"name": "from-http"}}

    manager.handle_skills_web_install = _install  # type: ignore[method-assign]
    payload = asyncio.run(getattr(manager, handler_name)({"url": "https://skills.example/pkg.zip"}))
    assert payload["success"] is True
    assert seen["params"]["url"] == "https://skills.example/pkg.zip"
