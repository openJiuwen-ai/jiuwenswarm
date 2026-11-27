# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Windows DLL 运行时 Guard 的宿主接线（``sandbox.startup_mode=dll``）。

本模块只做宿主适配：策略判定、原生注入与审批通道复用 agent-security 的
``agent_shield.intercepts.native_call.dll_guard``。开关默认关闭，由
``sandbox.enabled`` + ``sandbox.startup_mode=dll`` 同时为真才启用；失败一律
fail-closed，不退回无防护的本地执行。

审批卡沿用现有 ``chat.ask_user_question`` + ``permission_interrupt`` 应答通道：
卡片 ``request_id`` 以 ``dll-runtime:`` 为前缀，回答由
:func:`resolve_runtime_reply` 在进入 Agent 分发前消费，不会重启工具或续跑 Agent。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import contextmanager
from copy import deepcopy
from functools import wraps
from pathlib import Path
from typing import Any

from agent_shield.intercepts.native_call.dll_guard import (
    RuntimeApprovalRequests,
    RuntimeGuardManager,
    validate_guard_platform,
)
from agent_shield.intercepts.native_call.dll_guard import (
    validate_guard_bundle as _validate_guard_bundle,
)
from agent_shield.intercepts.native_call.dll_guard.launcher import (
    bind_process_launcher as _bind_shield_launcher,
)
from agent_shield.tool_permission.runtime.engine import PermissionEngine
from agent_shield.tool_permission.runtime.models import (
    PermissionConfirmResponse as _ShieldConfirmResponse,
)
from openjiuwen.core.foundation.tool.execution_context import (
    bind_tool_execution_context,
)
from openjiuwen.core.sys_operation.local.process_launcher import (
    bind_process_launcher as _bind_core_launcher,
)

from jiuwenswarm.common.config import is_dll_sandbox_enabled

logger = logging.getLogger(__name__)

#: 进程级审批通道：所有会话共享，按 ``request_id`` 关联到具体执行。
APPROVALS = RuntimeApprovalRequests()

#: 会话 sid -> manager，供诊断/关闭使用；生命周期仍由 agent 实例持有 manager 决定。
_MANAGERS: dict[str, RuntimeGuardManager] = {}

_DLL_ENV = "JIUWEN_RUNTIME_GUARD_DLL"

_ACTION_LABELS = {
    "read": "读取",
    "write": "写入",
    "delete": "删除",
    "exec": "执行",
    "access": "访问",
}


@contextmanager
def bind_process_launchers(adapter):
    """同时绑定两套启动适配器。

    agent-shield 的 binder 负责安装 ``subprocess.Popen`` audit hook（工具绕过
    ``create_process`` 时告警），core 的 binder 负责 ``create_process`` 实际读取的
    ContextVar；只绑一个都会让另一侧失效。
    """
    with _bind_shield_launcher(adapter), _bind_core_launcher(adapter):
        yield


def resolve_guard_dll(path: Path | str | None = None) -> Path:
    """解析并校验原生 Guard。

    显式参数优先，其次 ``JIUWEN_RUNTIME_GUARD_DLL``，都没有时交给
    ``agent-security-dll`` 安装包按平台解析。唯一出口，其他模块不要各自拼路径。
    """
    if path is None:
        override = os.environ.get(_DLL_ENV, "").strip()
        if override:
            path = override
    if path is not None:
        return _validate_guard_bundle(Path(path))
    return validate_guard_platform()


def require_dll_sandbox_runtime() -> Path | None:
    """开关开启时校验 Guard 可用；关闭时不做任何解析。"""
    if not is_dll_sandbox_enabled():
        return None
    return resolve_guard_dll()


def _permission_config(permission_rail: Any, session_id: str) -> dict[str, Any]:
    """读取当前生效的 permissions 段；优先使用权限 rail 已发布的配置。"""
    if callable(permission_rail):
        try:
            permission_rail = permission_rail()
        except Exception:  # noqa: BLE001 - 装配期 rail 尚不可用时回落到 YAML
            permission_rail = None
    key = getattr(permission_rail, "permission_key", None) if permission_rail is not None else None
    if isinstance(key, str) and key.strip():
        try:
            from agent_shield.tool_permission import get_permission_manager

            manager = get_permission_manager()
            if manager.has_config(key):
                return dict(manager.get_config(key) or {})
        except Exception:
            logger.warning("[runtime_guard] 读取已发布权限配置失败, 回落 YAML", exc_info=True)
    from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
        get_effective_permissions_config,
    )

    return dict(get_effective_permissions_config(session_id=session_id) or {})


class _EngineProvider:
    """按权限配置指纹重建引擎；配置未变时复用，避免每次资源检查重编译规则。"""

    def __init__(self, permission_rail: Any, session_id: str, workspace: str | None) -> None:
        self._rail = permission_rail
        self._session_id = session_id
        self._workspace = Path(workspace).resolve() if workspace else None
        self._fingerprint = ""
        self._engine: PermissionEngine | None = None

    def _snapshot(self) -> tuple[dict[str, Any], str]:
        config = _permission_config(self._rail, self._session_id)
        serialized = json.dumps(config, sort_keys=True, ensure_ascii=False, default=str)
        fingerprint = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        return config, fingerprint

    def __call__(self) -> PermissionEngine:
        config, fingerprint = self._snapshot()
        if self._engine is None or fingerprint != self._fingerprint:
            self._engine = PermissionEngine(config, workspace_root=self._workspace)
            self._fingerprint = fingerprint
        return self._engine

    def revision(self) -> str:
        _config, fingerprint = self._snapshot()
        workspace = str(self._workspace) if self._workspace is not None else ""
        return f"{workspace}:{fingerprint}"


def _resource_tool_name(resource: dict[str, Any]) -> str:
    kind = str(resource.get("kind") or "file")
    return "file_resource" if kind == "file" else f"dll_runtime_{kind}"


def _approval_question(resource: dict[str, Any]) -> str:
    kind = str(resource.get("kind") or "file")
    if kind == "file":
        lines = [
            f"{_ACTION_LABELS.get(str(action), str(action))} {path}"
            for path, action in resource.get("accesses") or []
        ]
        return "运行中的程序请求访问文件：\n\n" + "\n".join(lines)
    if kind == "url":
        return f"运行中的程序请求访问网址：\n\n{resource.get('url', '')}"
    if kind == "domain":
        return f"运行中的程序请求解析域名：\n\n{resource.get('hostname', '')}"
    if kind == "program":
        return f"运行中的程序请求启动子程序：\n\n{resource.get('exe', '')}"
    address = str(resource.get("address", ""))
    endpoint = f"[{address}]:{resource.get('port', '')}" if ":" in address else (
        f"{address}:{resource.get('port', '')}"
    )
    return f"运行中的程序请求建立网络连接：\n\n{endpoint}"


def _approval_options() -> list[dict[str, Any]]:
    """与 ``parse_hosted_permission_answer`` 的标签保持一致。"""
    options = [
        {"label": "本次允许", "description": "仅本次授权执行"},
        {"label": "本会话内允许", "description": "本次会话内自动放行同类操作"},
        {"label": "拒绝", "description": "拒绝执行"},
    ]
    from jiuwenswarm.edition import is_enterprise

    if not is_enterprise():
        options.insert(2, {"label": "永久记住", "description": "写回配置，所有会话均自动放行"})
    return options


def _approval_card(event: dict[str, Any]) -> dict[str, Any]:
    resource = deepcopy(event.get("resource") or {})
    tool_name = _resource_tool_name(resource)
    return {
        "event_type": "chat.ask_user_question",
        "request_id": event["request_id"],
        "source": "permission_interrupt",
        "session_id": event.get("session_id"),
        "questions": [
            {
                "header": "运行中程序请求权限",
                "question": _approval_question(resource),
                "options": _approval_options(),
                "multi_select": False,
                "tool_call_id": event.get("tool_call_id"),
                "tool_name": tool_name,
                "tool_args": resource,
            }
        ],
    }


async def _push_approval(event: dict[str, Any]) -> None:
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    message = {
        "request_id": event["request_id"],
        "session_id": event.get("session_id"),
        "tool_call_id": event.get("tool_call_id"),
        "execution_id": event.get("execution_id"),
        "channel_id": "web",
        "payload": _approval_card(event),
        "is_complete": False,
    }
    delivered = await AgentWebSocketServer.get_instance().send_push(message)
    if not delivered:
        raise RuntimeError("runtime approval has no active gateway connection")


def _persist_grant(
    session_id: str,
    config: dict[str, Any],
    grant: dict[str, Any],
    *,
    permanent: bool,
) -> bool:
    """永久记住时把授权写入 ``permissions.approval_overrides``；会话级不落盘。"""
    if not permanent:
        return False
    entry = {
        key: deepcopy(grant[key])
        for key in ("id", "tools", "match_type", "pattern", "action")
        if key in grant
    }
    if not entry.get("id") or not entry.get("tools") or not entry.get("pattern"):
        return False
    from jiuwenswarm.common.config import invalidate_merged_config_cache, update_config

    def _mutate(data: Any) -> Any:
        permissions = data.get("permissions")
        if not isinstance(permissions, dict):
            permissions = {}
            data["permissions"] = permissions
        overrides = permissions.get("approval_overrides")
        if not isinstance(overrides, list):
            overrides = []
            permissions["approval_overrides"] = overrides
        if not any(isinstance(item, dict) and item.get("id") == entry["id"] for item in overrides):
            overrides.append(entry)
        return data

    try:
        update_config(_mutate)
        invalidate_merged_config_cache()
    except Exception:
        logger.warning(
            "[runtime_guard] 保存授权失败 session=%s grant=%s",
            session_id,
            entry.get("id"),
            exc_info=True,
        )
        return False
    logger.info(
        "[runtime_guard] 已保存运行时授权 session=%s grant=%s tools=%s",
        session_id,
        entry.get("id"),
        entry.get("tools"),
    )
    return True


def _manager_for(
    instance: Any,
    *,
    session_id: str,
    permission_rail: Any,
    workspace: str | None,
) -> RuntimeGuardManager:
    manager = getattr(instance, "dll_guard_manager", None)
    if isinstance(manager, RuntimeGuardManager):
        return manager
    provider = _EngineProvider(permission_rail, session_id, workspace)
    manager = RuntimeGuardManager(
        session_id=session_id,
        engine=provider,
        dll=resolve_guard_dll,
        revision=provider.revision,
        emit=_push_approval,
        persist=_persist_grant,
        approvals=APPROVALS,
        enabled=is_dll_sandbox_enabled,
        # 工具实际走 core 的 ``create_process``，必须绑定 core 的同名 ContextVar；
        # 同时保留 agent-shield 的 audit hook，否则绕过 create_process 的子进程无告警。
        bind_launcher=bind_process_launchers,
    )
    _MANAGERS[session_id] = manager
    return manager


def _bind_execution_entries(target: Any, manager: RuntimeGuardManager) -> None:
    """在真正的 Agent 入口绑定执行上下文；不会改动工具参数或权限判定。"""
    if target is None or getattr(target, "dll_guard_entry_manager", None) is manager:
        return
    invoke = getattr(target, "invoke", None)
    if not callable(invoke):
        return
    stream = getattr(target, "stream", None)

    @wraps(invoke)
    async def guarded_invoke(*args: Any, **kwargs: Any) -> Any:
        with bind_tool_execution_context(manager.tool_execution):
            return await invoke(*args, **kwargs)

    target.invoke = guarded_invoke
    if callable(stream):
        @wraps(stream)
        async def guarded_stream(*args: Any, **kwargs: Any):
            with bind_tool_execution_context(manager.tool_execution):
                async for chunk in stream(*args, **kwargs):
                    yield chunk

        target.stream = guarded_stream
    target.dll_guard_entry_manager = manager


def assemble_guarded_agent(
    instance: Any,
    *,
    session_id: str,
    permission_rail: Any,
    workspace: str | None,
) -> RuntimeGuardManager | None:
    """为会话级 Agent 装配 DLL Guard；开关关闭时原样返回 ``None``。"""
    if not is_dll_sandbox_enabled():
        return None
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    manager = getattr(instance, "dll_guard_manager", None)
    if isinstance(manager, RuntimeGuardManager):
        _bind_execution_entries(getattr(instance, "react_agent", None), manager)
        return manager
    require_dll_sandbox_runtime()
    manager = _manager_for(
        instance,
        session_id=session_id,
        permission_rail=permission_rail,
        workspace=workspace,
    )
    # Desktop 的 task loop 可能直接驱动 react_agent，两个入口都要绑定。
    _bind_execution_entries(instance, manager)
    _bind_execution_entries(getattr(instance, "react_agent", None), manager)
    # 记录在实例上：adapter 重建时复用同一 manager，避免重复装配。
    instance.dll_guard_manager = manager
    logger.info(
        "[runtime_guard] DLL Guard 已装配 session=%s entry=%s",
        manager.session_id,
        type(instance).__name__,
    )
    return manager


def resolve_runtime_reply(session_id: str, params: dict[str, Any]) -> bool | None:
    """消费 ``dll-runtime:`` 审批应答；``None`` 表示普通请求。

    无效/过期的运行时 id 返回 ``False``，由调用方直接回执而不进入 Agent 分发，
    避免把一次审批回答误当成新的对话轮次。
    """
    request_id = params.get("request_id")
    if not isinstance(request_id, str) or not request_id.startswith("dll-runtime:"):
        return None
    answers = params.get("answers")
    if params.get("source") != "permission_interrupt" or not isinstance(answers, list):
        return False
    if len(answers) != 1 or not isinstance(answers[0], dict):
        return False
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        parse_hosted_permission_answer,
    )

    parsed = parse_hosted_permission_answer(answers[0])
    if parsed is None:
        return False
    try:
        # 宿主的 frozen dataclass 没有 wants_* 方法；bridge 对 frozen 对象会跳过
        # ``from_host``，这里先转成 agent-shield 自己的模型再投递。
        answer = _ShieldConfirmResponse.from_host(parsed)
    except (TypeError, ValueError):
        return False
    return APPROVALS.resolve(session_id, request_id, answer)


def get_manager(session_id: str) -> RuntimeGuardManager | None:
    """诊断入口：返回会话当前 Guard（不存在时为 ``None``）。"""
    return _MANAGERS.get(session_id)


async def release_manager(
    session_id: str, manager: RuntimeGuardManager | None = None
) -> None:
    """关闭并移除会话 Guard；会话 adapter 清理时调用。"""
    cached = _MANAGERS.pop(session_id, None)
    target = manager if manager is not None else cached
    if target is None:
        return
    try:
        await target.close(permanent=True)
    except Exception:
        logger.warning("[runtime_guard] 关闭 DLL Guard 失败 session=%s", session_id, exc_info=True)


__all__ = [
    "APPROVALS",
    "assemble_guarded_agent",
    "bind_process_launchers",
    "get_manager",
    "release_manager",
    "require_dll_sandbox_runtime",
    "resolve_guard_dll",
    "resolve_runtime_reply",
]
