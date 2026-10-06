"""Gateway → AgentServer 统一薄代理（E2A 转发）。

完成既有认证鉴权与路由后，把 Web/TUI/IM 入口的用户业务请求经 E2A /
``AgentServerClient`` 转发到目标 AgentServer，并将适配器响应按原外部协议
（``channel.send_response``）返回。

约束（方案 §4/§6/§8）：
- Gateway 不直接读写用户态文件，也不保留依赖用户 ``.jiuwenswarm`` 的
  业务逻辑；目标 AgentServer 不可达时返回可重试错误，**禁止**用部署侧
  目录代替用户目录执行。
- 单用户共享目录布局（默认本地 WebSocket client）**不再**在
  ``server_ready=False`` 时跑本地 adapter。Session/Project/Config/History/Health
  由 AgentServer Front 提供；执行类请求在 Front 不可达时返回可重试错误。
- user_id 只用于路由/观测关联，不要求 AgentServer 据此选择目录。
- 传输层客户端由配置驱动（websocket / agentos_router），本薄代理对
  两者透明。

覆盖会话、配置、工作区/文件、项目/Git、记忆、HarmonyOS 等 E2A 方法；
``fetch_agent_unary`` / ``fetch_git_diff_status`` 用于非通道上下文
（轮询 fetcher、/ws/git 首次快照等）的 E2A 请求。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.routing.agent_client import (
    DuplicateRequestIdError,
    WebSocketAgentServerClient,
)
from jiuwenswarm.gateway.routing.agent_request_timeout import (
    AGENT_SERVER_TIMEOUT_CODE,
    AGENT_SERVER_TIMEOUT_ERROR,
    AgentRequestTimeoutError,
    send_agent_request_with_timeout,
)

logger = logging.getLogger(__name__)

#: 目标 AgentServer 不可达/请求失败时返回的外部协议错误码
SERVICE_UNAVAILABLE_CODE = "SERVICE_UNAVAILABLE"
DEFAULT_PROXY_LABEL = "agent-proxy"


def _new_fetch_request_id() -> str:
    """生成 Gateway 侧主动发起的 E2A request_id（``fetch-`` 前缀）。

    request_id 是 ``WebSocketAgentServerClient`` 路由响应的唯一键：同一连接上
    若两个在途请求撞号，客户端会拒绝注册第二个队列并抛错（请求根本发不出去）。

    因此不能只用 ``time.time_ns()``：

    - 墙钟分辨率有限（Windows 实测只有 100ns 步进，部分环境更粗），相邻两次
      生成可能完全相同；归档/删除等长耗时 RPC 与 2s 生命周期轮询共用连接的
      情况下，撞号会直接表现为偶发失败。
    - 墙钟非单调，NTP 校正或休眠唤醒回拨时可能重放出已用过的取值。

    保留时间戳前缀便于日志排序与排查，后缀补 8 位随机量保证进程内唯一。
    """
    return f"fetch-{time.time_ns()}-{uuid.uuid4().hex[:8]}"


def is_agentos_routing_client(agent_client: Any) -> bool:
    """Whether ``agent_client`` routes requests to per-user AgentOS runtimes.

    Keep the check local to the Gateway routing boundary: importing the optional
    AgentOS extension here would make the regular single-user installation depend
    on that extension.  The concrete client is deliberately identified by its
    stable module/class name instead.
    """
    client_type = type(agent_client)
    return (
        client_type.__name__ == "AgentOSRouterClient"
        and client_type.__module__.startswith("jiuwenswarm.extensions.agentos.")
    )


def is_legacy_shared_directory_client(agent_client: Any) -> bool:
    """Whether this is the default local WebSocket AgentServer client.

    Channel handlers use this to distinguish the single-user shared-directory
    layout from remote/AgentOS clients.  It does not authorize a Gateway-side
    adapter substitute for AgentServer RPCs.
    """
    return isinstance(agent_client, WebSocketAgentServerClient)


async def proxy_unary_request(
    *,
    channel: Any,
    agent_client: Any,
    ws: Any,
    req_id: str,
    params: dict[str, Any] | None,
    session_id: str | None,
    user_id: str | None,
    req_method: ReqMethod,
    label: str = DEFAULT_PROXY_LABEL,
    timeout_seconds: float | None = None,
    extra_params: dict[str, Any] | None = None,
    drop_params: tuple[str, ...] = (),
    on_done: Callable[[bool, dict[str, Any]], Awaitable[None] | None] | None = None,
    preserve_error_payload: bool = False,
    default_error_code: str = "BAD_REQUEST",
) -> bool:
    """向目标 AgentServer 发起一次 unary E2A 请求并按原外部协议返回。

    Args:
        channel: 通道对象，提供 ``send_response(ws, req_id, *, ok, payload,
            error, code)``。
        agent_client: 已解析的 AgentServerClient（WebSocketAgentServerClient /
            AgentOSRouterClient）；None 视为不可达。
        extra_params: 追加到请求 params 的字段（如 create_token、user_id）。
        drop_params: 需从请求 params 剔除的字段（如内部 session_id）。
        timeout_seconds: 显式超时；None 走既有超时策略
            （``resolve_agent_request_timeout_seconds``）。
        on_done: 收到 AgentServer 响应后的非阻塞 Gateway 侧维护回调，接收
            ``(ok, payload)``。用于连接/预热、watcher 唤醒等部署侧维护动作；
            由调用方自行判断是否只在成功/特定失败结果时行动。回调失败不影响
            已完成的业务请求。
        preserve_error_payload: 将 AgentServer 失败 payload 原样回写。仅用于
            Git 等已定义结构化错误明细的外部协议。
        default_error_code: AgentServer 失败响应未携带 code 时回写的默认错误码
            （保持各入口迁移前的外部协议 code 不变）。

    Returns:
        True 表示已向通道回写响应（调用方无需再 send_response）。
    """
    # Some compatible/extension clients do not expose ``server_ready``.  They
    # remain callable (and may apply their own timeout), so only an explicit
    # False denotes an unavailable transport here.
    if agent_client is None or getattr(agent_client, "server_ready", True) is False:
        await channel.send_response(
            ws,
            req_id,
            ok=False,
            error="AgentServer is unavailable",
            code=SERVICE_UNAVAILABLE_CODE,
        )
        return True

    proxy_params = dict(params or {})
    for key in drop_params:
        proxy_params.pop(key, None)
    if extra_params:
        proxy_params.update(extra_params)
    # user_id 以独立参数为准（经 envelope.user_id 承载，路由/观测唯一权威）；
    # 独立 user_id 非空时移除 extra_params 中重复的 user_id，避免 params 双份
    # 导致后续适配器遍历 params 时意外吸入用户身份字段。
    if user_id:
        proxy_params.pop("user_id", None)

    env = e2a_from_agent_fields(
        request_id=req_id,
        channel_id=channel.channel_id,
        session_id=session_id,
        req_method=req_method,
        params=proxy_params,
        is_stream=False,
        timestamp=time.time(),
        user_id=user_id or None,
    )
    try:
        response = await send_agent_request_with_timeout(
            agent_client,
            env,
            label=f"{label} {req_method.value}",
            timeout_seconds=timeout_seconds,
        )
    except AgentRequestTimeoutError:
        await channel.send_response(
            ws,
            req_id,
            ok=False,
            error=AGENT_SERVER_TIMEOUT_ERROR,
            code=AGENT_SERVER_TIMEOUT_CODE,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[%s] %s 转发失败: request_id=%s error=%s",
            label,
            req_method.value,
            req_id,
            exc,
        )
        await channel.send_response(
            ws,
            req_id,
            ok=False,
            error=str(exc),
            code=SERVICE_UNAVAILABLE_CODE,
        )
        return True

    payload = (
        dict(response.payload)
        if isinstance(response.payload, dict)
        else (response.payload if response.payload is not None else {})
    )
    if on_done is not None:
        try:
            callback_result = on_done(bool(response.ok), payload)
            if callback_result is not None:
                await callback_result
        except Exception as exc:  # noqa: BLE001 - maintenance must not fail the RPC
            logger.warning("[%s] %s done callback failed: %s", label, req_method.value, exc)
    await channel.send_response(
        ws,
        req_id,
        ok=bool(response.ok),
        payload=payload if response.ok or preserve_error_payload else None,
        error=None if response.ok else str(payload.get("error") or f"{req_method.value} failed"),
        code=None if response.ok else str(payload.get("code") or default_error_code),
    )
    return True


async def fetch_agent_unary(
    *,
    agent_client: Any,
    req_method: ReqMethod,
    params: dict[str, Any] | None,
    session_id: str | None,
    user_id: str | None,
    channel_id: str,
    label: str = DEFAULT_PROXY_LABEL,
    timeout_seconds: float | None = None,
) -> tuple[bool, dict[str, Any]]:
    """向目标 AgentServer 发起一次 unary E2A 请求，返回 ``(ok, payload)``。

    与 ``proxy_unary_request`` 不同，本函数不回写通道响应，供 Gateway 侧
    需要消费 AgentServer 结果后自行决策的桥接场景（如 Git watch 快照计算）
    使用。目标 AgentServer 不可达/超时/失败时返回 ``(False, {error, code})``。
    """
    if agent_client is None or getattr(agent_client, "server_ready", True) is False:
        return False, {
            "error": "AgentServer is unavailable",
            "code": SERVICE_UNAVAILABLE_CODE,
        }

    response = None
    # request_id 撞号时请求尚未发出（拒绝发生在注册响应队列阶段），换号重试
    # 一次是安全的；不做这层重试，id 生成器偶发重复会直接变成用户可见的失败。
    for attempt in (0, 1):
        env = e2a_from_agent_fields(
            request_id=_new_fetch_request_id(),
            channel_id=channel_id,
            session_id=session_id,
            req_method=req_method,
            params=dict(params or {}),
            is_stream=False,
            timestamp=time.time(),
            user_id=user_id or None,
        )
        try:
            response = await send_agent_request_with_timeout(
                agent_client,
                env,
                label=f"{label} {req_method.value}",
                timeout_seconds=timeout_seconds,
            )
            break
        except AgentRequestTimeoutError:
            return False, {
                "error": AGENT_SERVER_TIMEOUT_ERROR,
                "code": AGENT_SERVER_TIMEOUT_CODE,
            }
        except DuplicateRequestIdError as exc:
            if attempt == 0:
                logger.warning(
                    "[%s] %s request_id 撞号，换号重试: request_id=%s",
                    label,
                    req_method.value,
                    exc.request_id,
                )
                continue
            logger.error(
                "[%s] %s request_id 重复撞号，放弃: request_id=%s",
                label,
                req_method.value,
                exc.request_id,
            )
            return False, {"error": str(exc), "code": SERVICE_UNAVAILABLE_CODE}
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[%s] %s 转发失败: request_id=%s error=%s",
                label,
                req_method.value,
                getattr(env, "request_id", ""),
                exc,
            )
            return False, {"error": str(exc), "code": SERVICE_UNAVAILABLE_CODE}
    if response is None:  # pragma: no cover - 防御：循环内所有异常分支均已返回
        return False, {
            "error": "AgentServer is unavailable",
            "code": SERVICE_UNAVAILABLE_CODE,
        }

    payload = (
        dict(response.payload)
        if isinstance(response.payload, dict)
        else (response.payload if response.payload is not None else {})
    )
    if not response.ok:
        return False, {
            "error": str(payload.get("error") or f"{req_method.value} failed"),
            "code": str(payload.get("code") or "BAD_REQUEST"),
        }
    return True, payload


async def resolve_agent_cron_project_binding(
    *,
    agent_client: Any,
    params: dict[str, Any],
    user_id: str | None,
    channel_id: str,
    session_id: str | None,
) -> tuple[bool, dict[str, Any]]:
    """Resolve a cron's project in the routed AgentServer user directory.

    The Gateway remains the cron job-store owner.  It only persists the
    concrete project binding returned by the target AgentServer and never
    probes its own deployment-side project store for AgentOS requests.
    """
    return await fetch_agent_unary(
        agent_client=agent_client,
        req_method=ReqMethod.PROJECT_CRON_RESOLVE_BINDING,
        params=params,
        session_id=session_id,
        user_id=user_id,
        channel_id=channel_id,
        label="cron.project_binding",
    )


async def fetch_git_diff_status(
    *,
    agent_client: Any,
    project_id: str,
    session_id: str | None = None,
    include_files: bool = False,
    include_hunks: bool = False,
    hunk_paths: list[str] | None = None,
    user_id: str | None = None,
    channel_id: str = "web",
) -> tuple[bool, dict[str, Any]]:
    """经 E2A 获取项目 Git diff 状态（项目解析 + diff 计算在目标 AgentServer 注入目录）。

    Git watch 轮询（``GitDiffWatcherRegistry`` 注入的 fetcher）与 /ws/git 首次
    快照共用同一请求构造；失败返回 ``(False, {error, code})`` 供调用方决定
    watcher 退避/暂停。
    """
    params: dict[str, Any] = {
        "project_id": project_id,
        "include_files": include_files,
        "include_hunks": include_hunks,
    }
    if session_id:
        params["session_id"] = session_id
    if hunk_paths:
        params["hunk_paths"] = list(hunk_paths)
    return await fetch_agent_unary(
        agent_client=agent_client,
        req_method=ReqMethod.PROJECT_GIT_DIFF_STATUS,
        params=params,
        session_id=session_id or None,
        user_id=user_id,
        channel_id=channel_id,
        label="git-watch",
    )
