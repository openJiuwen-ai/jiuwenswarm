# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentServer的HTTP+SSE入口"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable

from jiuwenswarm.common.e2a.constants import E2A_RESPONSE_STATUS_FAILED
from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.edition import is_enterprise
from jiuwenswarm.server.transports.sink import STREAM_DONE, SSESink, UnaryHTTPSink

logger = logging.getLogger(__name__)

DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8766
API_PREFIX = "/api/v1"

#: 端口被占时向上扫描的候选个数与步进，与实例端口组（base + index*1000）保持一致。
PORT_SCAN_RANGE = 10
PORT_SCAN_STEP = 1000

#: 企业版北向允许的 Skill 方法白名单（必须与 Gateway ``invoke.py`` 的
#: ``_ENTERPRISE_SKILL_ALLOWED`` 保持一致）。企业版经 SPI 开放只读列表/详情
#: 与用户自装/卸载/更新写操作；技能卸载走 ``skills.enterprise.uninstall``。
_ENTERPRISE_SKILL_ALLOWED = frozenset({
    "skills.list",
    "skills.get",
    "skills.toggle",
    "skills.source.providers",
    "skills.source.search",
    "skills.source.install",
    "skills.updates.check",
    "skills.update",
    "skills.enterprise.list",
    "skills.enterprise.install",
    "skills.enterprise.uninstall",
    "skills.enterprise.source.providers",
    "skills.enterprise.source.search",
})


def _is_enterprise_skill_forbidden(method: str) -> bool:
    """AgentServer 侧防御性校验：企业版下拒绝白名单外的 ``skills.*`` 写操作。

    正常链路已由 Gateway 的 ``is_enterprise_write_forbidden`` 拦截；此处兜底
    AgentServer 被直连（HTTP 直连）时绕过 Gateway 改启停/安装/卸载的情况。
    """
    if not is_enterprise():
        return False
    return method.startswith("skills.") and method not in _ENTERPRISE_SKILL_ALLOWED

#: HTTP 侧交给业务层的 ``RequestContext.connection_id``。
#:
#: **必须跨请求稳定。** 业务层用它给"按连接分槽"的逻辑做键 —— 目前是
#: ``handlers/session.py`` 的 ``session.switch`` 切换锁与 ACP 客户端能力。
#: 取 ``id(sink)`` / ``request_id`` 这类**每请求唯一**的值，会让每个请求各造一把锁，
#: 互斥彻底失效（HTTP 侧曾如此，由 ``test_http_connection_id_is_stable_across_requests``
#: 钉住）。
#:
#: 为什么是常量而不是从请求里推：
#:
#: 1. HTTP 的 TCP 连接对这里不可见，且它与"逻辑客户端"并非一一对应
#:    （keep-alive 一对多、连接池多对一、重连即换），扛不起连接身份的语义；
#: 2. ``dispatch_raw_envelope`` 解析失败时会以 ``request=None`` 调 ``_make_ctx``
#:    （``/e2a`` 收到未知方法名即走此路），任何 ``request.xxx`` 的推导都会在那里抛；
#: 3. 两个消费者要的粒度本就不是"请求"：切换锁的键已自带 ``channel_id``，
#:    ACP 能力另有 channel 级兜底（``pipeline.py`` 的 ``ws_caps or ...``）。
#:
#: 语义即"HTTP 侧视作单一逻辑连接"。这与 WS 的实际粒度一致 —— Gateway 南向只有
#: 一条连接，``id(ws)`` 在那里同样是个恒定值。
#:
#: 已知边界：WS 客户端与 HTTP 客户端在**同一 channel** 上并发切换不会互相串行
#: （两者键不同）。这是既有状态，修法见 ``handlers/session.py`` 切换锁处的说明。
HTTP_CONNECTION_ID = "http"

#: 正常关闭时等待 uvicorn 收尾的秒数。
SHUTDOWN_TIMEOUT = 10
#: 启动失败清理时的等待秒数 —— 此时在启动关键路径上，不能拖慢 WebSocket。
SHUTDOWN_TIMEOUT_ON_START_FAILURE = 2

#: ``error.code`` → HTTP 状态码。未列出的错误码统一 500。
ERROR_CODE_STATUS: dict[str, int] = {
    "BAD_REQUEST": 400,
    "VALIDATION_ERROR": 422,
    "UNAUTHORIZED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "CONFLICT": 409,
    "RESPONSE_TOO_LARGE": 413,
    "UNKNOWN_METHOD": 404,
    "SERVICE_UNAVAILABLE": 503,
    "SANDBOX_BAD_REQUEST": 503,
    "INTERNAL_ERROR": 500,
}

#: 不携带语义的通用错误码 —— 业务层用它包装各类失败，
#: 直接映射 500 会把「会话不存在」这类客户端错误误报为服务故障，
#: 导致对接方盲目重试。对这些码按 message 关键字细化状态码。
GENERIC_ERROR_CODES = frozenset({"E2A.AGENT_ERROR", "INTERNAL_ERROR", "AGENT_ERROR", ""})

#: message 关键字 → HTTP 状态码。按顺序首个命中生效。
MESSAGE_STATUS_HINTS: tuple[tuple[str, int], ...] = (
    ("not found", 404),
    ("不存在", 404),
    ("already exists", 409),
    ("已存在", 409),
    ("unauthorized", 401),
    ("forbidden", 403),
    ("permission denied", 403),
    ("is required", 400),
    ("缺少参数", 400),
    ("invalid", 400),
)


def resolve_error_status(code: str, message: str) -> int:
    """错误码 + 描述 → HTTP 状态码。

    明确的错误码优先；通用码则按 message 关键字启发式细化，
    仍无法判断时回落 500。
    """
    if code and code not in GENERIC_ERROR_CODES and code in ERROR_CODE_STATUS:
        return ERROR_CODE_STATUS[code]
    if code in GENERIC_ERROR_CODES or code not in ERROR_CODE_STATUS:
        lowered = (message or "").lower()
        for needle, status in MESSAGE_STATUS_HINTS:
            if needle in lowered:
                return status
    return ERROR_CODE_STATUS.get(code, 500)


def resolve_cors_origins() -> tuple[list[str], bool]:
    """允许跨域的来源 → ``(origins, allow_credentials)``。
    优先级 **env > config.yaml > 按端口推导**：

    - ``AGENT_HTTP_CORS_ORIGINS``：逗号分隔；``*`` 表示显式放开
    - ``config.yaml`` 的 ``http_server.cors_origins``：字符串或列表
    - 都没有时按 ``FRONTEND_PORT`` / ``WEB_PORT`` 推导本机来源。**端口不写死** ——
      多实例下 ``jiuwenswarm-start`` 会按实例 index 下发不同端口
      （``instance_manager.BASE_PORTS``：frontend 5173 / web 19000）。

    ``allow_credentials`` 随之联动：CORS 规范**禁止**通配源携带凭证，浏览器会直接拒绝，
    所以 ``origins == ["*"]`` 时必须返回 ``False``，否则是个既不安全又不生效的组合。
    """
    import os

    def _split(raw: str) -> list[str]:
        return [item.strip() for item in raw.split(",") if item.strip()]

    origins: list[str] = []

    env_raw = (os.getenv("AGENT_HTTP_CORS_ORIGINS") or "").strip()
    if env_raw:
        origins = _split(env_raw)
    else:
        try:
            from jiuwenswarm.common.config import get_config

            cfg = get_config().get("http_server")
            configured = cfg.get("cors_origins") if isinstance(cfg, dict) else None
            if isinstance(configured, str):
                origins = _split(configured)
            elif isinstance(configured, (list, tuple)):
                origins = [str(item).strip() for item in configured if str(item).strip()]
        except Exception:  # noqa: BLE001 - 配置不可用时回退到端口推导
            logger.debug("[AgentHTTPServer] 读取 cors_origins 失败，回退端口推导", exc_info=True)

    if not origins:
        ports: list[str] = []
        for env_name, fallback in (("FRONTEND_PORT", "5173"), ("WEB_PORT", "19000")):
            value = (os.getenv(env_name) or "").strip() or fallback
            if value.isdigit() and value not in ports:
                ports.append(value)
        for port_value in ports:
            origins.append(f"http://localhost:{port_value}")
            origins.append(f"http://127.0.0.1:{port_value}")

    if "*" in origins:
        # 显式放开：必须关掉 credentials，否则浏览器会拒绝整个跨域请求。
        logger.warning(
            "[AgentHTTPServer] CORS 允许任意来源（*）——本入口无鉴权，"
            "任意网站的页面都能跨域调用它。仅限受控环境使用。"
        )
        return ["*"], False

    logger.info("[AgentHTTPServer] CORS 允许来源: %s", ", ".join(origins))
    return origins, True


def new_request_id() -> str:
    return f"http_{uuid.uuid4().hex[:16]}"


def is_port_available(host: str, port: int) -> bool:
    """端口预检：能 bind 上即视为可用。

    存在极小的 TOCTOU 窗口，但 :meth:`AgentHTTPServer.start` 里的 guard 协程
    会兜住随后的绑定失败，不会影响 WebSocket。
    """
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host or "127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    except (TypeError, ValueError, OverflowError):
        return False


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            return True
        if v in {"0", "false", "no", "off"}:
            return False
    return default


def resolve_http_server_settings(agent_host: str) -> tuple[bool, str, int]:
    """解析 HTTP 入口配置，返回 ``(enabled, host, port)``。

    优先级：**环境变量 > config.yaml > 默认值**。

    - **config.yaml 是主配置入口
        http_server:
          enabled: true
          host: 127.0.0.1      # 留空则跟随 AgentServer host
          port: 8766           # 经启动器拉起时由 AGENT_HTTP_PORT 覆盖

    - 环境变量 ``AGENT_HTTP_ENABLED`` / ``AGENT_HTTP_HOST`` / ``AGENT_HTTP_PORT``

    """
    import os

    cfg: dict[str, Any] = {}
    try:
        from jiuwenswarm.common.config import get_config

        raw = get_config().get("http_server")
        if isinstance(raw, dict):
            cfg = raw
    except Exception:  # noqa: BLE001 - 配置不可用时回退到 env/默认
        logger.debug("[AgentHTTPServer] 读取 http_server 配置失败，回退 env/默认", exc_info=True)

    env_enabled = os.getenv("AGENT_HTTP_ENABLED")
    enabled = (
        _as_bool(env_enabled, False)
        if env_enabled is not None and env_enabled.strip() != ""
        else _as_bool(cfg.get("enabled"), False)
    )

    host = (os.getenv("AGENT_HTTP_HOST") or "").strip() or str(cfg.get("host") or "").strip()
    if not host:
        host = agent_host or DEFAULT_HTTP_HOST

    port_raw = (os.getenv("AGENT_HTTP_PORT") or "").strip() or cfg.get("port")
    try:
        port = int(port_raw) if port_raw not in (None, "") else DEFAULT_HTTP_PORT
    except (TypeError, ValueError):
        logger.warning("[AgentHTTPServer] 非法端口 %r，回退 %d", port_raw, DEFAULT_HTTP_PORT)
        port = DEFAULT_HTTP_PORT

    return enabled, host, port


def build_envelope_json(
    *,
    method: str,
    params: dict[str, Any],
    request_id: str,
    session_id: str | None,
    channel_id: str,
    user_id: str | None,
    is_stream: bool,
    routing: dict[str, str] | None = None,
    tenant_ids: dict[str, str] | None = None,
    request_ext: dict[str, Any] | None = None,
) -> str:
    """组装与 WS 客户端等价的 E2A 信封 JSON。

    本函数保留作为那条等价契约的参照实现，测试使用：
    ``tests/transport/test_route_matrix.py::test_direct_agent_request_equals_json_roundtrip``
    用它比对新路径，防止两者漂移。删掉它等于删掉那份契约。
    """
    env = E2AEnvelope(
        request_id=request_id,
        session_id=session_id,
        channel=channel_id,
        method=method,
        params=params or {},
        is_stream=is_stream,
        user_id=user_id,
        channel_context=_build_channel_context(routing, request_ext),
        service_id=(tenant_ids or {}).get("service_id"),
        agent_id=(tenant_ids or {}).get("agent_id"),
        workspace_key=(tenant_ids or {}).get("workspace_key"),
    )
    return json.dumps(env.to_dict(), ensure_ascii=False)


def _build_channel_context(
    routing: dict[str, str] | None,
    request_ext: dict[str, Any] | None,
) -> dict[str, Any]:
    """重建结构化 REST 入口丢弃的 E2A 请求上下文子集。"""
    from jiuwenswarm.common.request_ext import attach_to_metadata
    from jiuwenswarm.common.request_identity import apply_routing_metadata

    channel_context = apply_routing_metadata({}, routing)
    if not request_ext:
        return channel_context
    return attach_to_metadata(channel_context, ext=request_ext) or {}


def build_agent_request(
    *,
    method: str,
    params: dict[str, Any],
    request_id: str,
    session_id: str | None,
    channel_id: str,
    user_id: str | None,
    is_stream: bool,
    routing: dict[str, str] | None = None,
    tenant_ids: dict[str, str] | None = None,
    request_ext: dict[str, Any] | None = None,
) -> Any:
    """构造``AgentRequest``。

    ``routing`` 来自 Gateway REST 身份头（``X-User-Id`` / ``X-Bot-Id`` 等），
    经 :func:`apply_routing_metadata` 写入顶层 ``user_id`` 与
    ``channel_context.routing``（group/bot/gateway）。

    ``tenant_ids`` 来自 ``X-Service-Id`` / ``X-Agent-Id`` / ``X-Workspace-Key``，
    写入信封顶层后由 :func:`e2a_to_agent_request` 落到 ``AgentRequest``。
    """
    from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
    tenant = tenant_ids or {}
    env = E2AEnvelope(
        request_id=request_id,
        session_id=session_id,
        channel=channel_id,
        method=method,
        params=params or {},
        is_stream=is_stream,
        user_id=user_id,
        channel_context=_build_channel_context(routing, request_ext),
        service_id=tenant.get("service_id"),
        agent_id=tenant.get("agent_id"),
        workspace_key=tenant.get("workspace_key"),
    )
    return e2a_to_agent_request(env)


def _frame_event_name(frame: dict[str, Any]) -> str:
    """从 wire 帧推导 SSE ``event`` 名。

    优先 ``response_kind``；否则取 ``body.event_type``；再否则按终态兜底。
    """
    kind = frame.get("response_kind")
    if isinstance(kind, str) and kind:
        return kind
    body = frame.get("body")
    if isinstance(body, dict):
        evt = body.get("event_type") or body.get("type")
        if isinstance(evt, str) and evt:
            return evt
    if frame.get("status") == E2A_RESPONSE_STATUS_FAILED:
        return "chat.error"
    if frame.get("is_final"):
        return "chat.final"
    return "message"


def frame_to_http_envelope(
    frame: dict[str, Any] | None, request_id: str
) -> tuple[dict[str, Any], int]:
    """wire 帧 → (HTTP 响应体, HTTP 状态码)。"""
    if frame is None:
        # handler 未产出任何帧：视为已受理但无内容返回。
        return {"request_id": request_id, "ok": True, "data": None, "metadata": {}}, 200

    body = frame.get("body") if isinstance(frame.get("body"), dict) else {}
    metadata = frame.get("metadata") if isinstance(frame.get("metadata"), dict) else {}
    failed = frame.get("status") == E2A_RESPONSE_STATUS_FAILED

    if failed:
        code = str(body.get("code") or "INTERNAL_ERROR")
        message = str(body.get("error") or body.get("message") or "request failed")
        details = {k: v for k, v in body.items() if k not in {"code", "error", "message"}}
        # 业务层用 payload 里的 ``code`` 细化状态码。
        #
        # ``ok=False`` 的响应经 ``encode_agent_response_for_wire`` 编码后，顶层
        # ``code``/``message`` 恒为 ``E2A.AGENT_ERROR`` / ``Agent error``，业务
        # payload 整个落进 ``details``。这两个通用值不带语义，只能回落 500 ——
        # 而「查不到席位」「会话不存在」这类客户端侧结果被报成 500，正是
        # :data:`GENERIC_ERROR_CODES` 注释里说的「误报为服务故障、导致对接方
        # 盲目重试」。
        #
        # 因此：顶层码是通用码时，认 ``details.code``，且**只认**
        # :data:`ERROR_CODE_STATUS` 里的已知码 —— 业务 payload 里恰好叫 code 的
        # 其它值（如渠道码、模型码）不会误改状态码。
        if code in GENERIC_ERROR_CODES:
            inner = body.get("details")
            hinted = inner.get("code") if isinstance(inner, dict) else None
            if isinstance(hinted, str) and hinted in ERROR_CODE_STATUS:
                code = hinted
        payload = {
            "request_id": frame.get("request_id") or request_id,
            "ok": False,
            "error": {"code": code, "message": message, "details": details},
        }
        return payload, resolve_error_status(code, message)

    payload = {
        "request_id": frame.get("request_id") or request_id,
        "ok": True,
        "data": body,
        "metadata": metadata,
    }
    return payload, 200


class AgentHTTPServer:
    """HTTP/SSE 服务端；持有 ``AgentWebSocketServer`` 实例以复用其 handler。"""

    def __init__(
        self,
        ws_server: Any,
        host: str = DEFAULT_HTTP_HOST,
        port: int = DEFAULT_HTTP_PORT,
    ) -> None:
        self._ws_server = ws_server
        self._host = host
        self._port = port
        self._server: Any = None
        self._serve_task: asyncio.Task | None = None

    @property
    def port(self) -> int:
        """实际监听的端口。

        不是构造时传入的那个 —— :meth:`start` 在端口被占时会按 +1000 步进向上扫描
        并落到别的端口上（与实例端口组一致）。调用方（如就绪横幅）要打印的是**实际**
        监听的端口，因此读这个属性，而不是外部去碰内部字段。
        """
        return self._port

    # 与WS共享入口
    def _make_ctx(self, sink: Any, request: Any) -> Any:
        """构造与 WS 侧同形的 ``RequestContext``"""
        from jiuwenswarm.server.context import AgentServerServices, RequestContext

        return RequestContext(
            request=request,
            sink=sink,
            connection_id=HTTP_CONNECTION_ID,
            services=AgentServerServices(self._ws_server),
        )

    async def dispatch_raw_envelope(self, sink: Any, raw: str) -> None:
        """把**原始 JSON 字节**按与 WS 完全相同的规则解析后交给汇合点。

        仅供 ``/e2a`` 原始信封透传使用 —— 那里的载荷是客户端直接给的任意 JSON，
        需要完整的解析与解析错误处理。结构化入口请用 :meth:`_dispatch_request`。
        解析规则在 ``server/wire_parse.py``
        """
        from jiuwenswarm.server.wire_parse import parse_inbound

        result = parse_inbound(raw)
        if not result.ok:
            await self._make_ctx(sink, None).sink.send_wire(result.error_wire)
            return
        await self._dispatch_request(sink, result.request)

    async def _dispatch_request(self, sink: Any, request: Any) -> None:
        """分发构造造好的``AgentRequest``，发给pipeline"""
        from jiuwenswarm.server.pipeline import dispatch_parsed_request

        ctx = self._make_ctx(sink, request)
        await dispatch_parsed_request(ctx, request)

    async def invoke_unary(
        self,
        method: str,
        params: dict[str, Any],
        *,
        request_id: str,
        session_id: str | None = None,
        channel_id: str = "web",
        user_id: str | None = None,
        routing: dict[str, str] | None = None,
        tenant_ids: dict[str, str] | None = None,
        request_ext: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], int]:
        """非流式调用，返回 (响应体, 状态码)。"""
        if _is_enterprise_skill_forbidden(method):
            return (
                {
                    "request_id": request_id,
                    "ok": False,
                    "error": {"code": "FORBIDDEN", "message": "企业版配置由管理面统一下发"},
                },
                403,
            )
        agent_request = build_agent_request(
            method=method,
            params=params,
            request_id=request_id,
            session_id=session_id,
            channel_id=channel_id,
            user_id=user_id,
            is_stream=False,
            routing=routing,
            tenant_ids=tenant_ids,
            request_ext=request_ext,
        )
        sink = UnaryHTTPSink()
        try:
            await self._dispatch_request(sink, agent_request)
        except Exception as exc:  # noqa: BLE001 - 转成 500 而非中断服务
            logger.exception("[AgentHTTPServer] handler 异常 method=%s", method)
            return (
                {
                    "request_id": request_id,
                    "ok": False,
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": str(exc),
                        "details": {"method": method},
                    },
                },
                500,
            )
        return frame_to_http_envelope(sink.last_frame, request_id)

    async def iter_stream(
        self,
        method: str,
        params: dict[str, Any],
        *,
        request_id: str,
        session_id: str | None = None,
        channel_id: str = "web",
        user_id: str | None = None,
        routing: dict[str, str] | None = None,
        tenant_ids: dict[str, str] | None = None,
        request_ext: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """流式调用（结构化入口），产出 sse_starlette 所需的事件 dict。"""
        agent_request = build_agent_request(
            method=method,
            params=params,
            request_id=request_id,
            session_id=session_id,
            channel_id=channel_id,
            user_id=user_id,
            is_stream=True,
            routing=routing,
            tenant_ids=tenant_ids,
            request_ext=request_ext,
        )
        sink = SSESink()
        async for event in self._pump_sse(
            sink,
            lambda: self._dispatch_request(sink, agent_request),
            request_id=request_id,
            label=f"method={method}",
        ):
            yield event

    async def iter_raw_envelope(
        self, raw: str, *, request_id: str
    ) -> AsyncIterator[dict[str, Any]]:
        """流式调用（``/e2a`` 原始信封入口）。

        与 :meth:`invoke_unary` / :meth:`iter_stream` 的区别只在解析段：这里走
        ``wire_parse.parse_inbound``，把客户端给的信封**原样**解析，而不是由我们
        用 ``method`` + ``params`` 重新组装。

        存在的理由：``/e2a`` 的非流式分支用 ``UnaryHTTPSink``，而 ``UnaryHTTPSink``
        只记录 ``send_unary`` / ``send_wire`` 的帧 —— 信封里 ``is_stream=true`` 时
        业务层全程走 ``send_chunk``，那些帧进不了 ``last_frame``，于是响应体退化成
        ``{"ok": true, "data": null}``：HTTP 200、内容全丢、且没有任何报错。
        补上本方法后，原始信封的两种形态都能原样透传，与 WS 对齐。
        """
        sink = SSESink()
        async for event in self._pump_sse(
            sink,
            lambda: self.dispatch_raw_envelope(sink, raw),
            request_id=request_id,
            label="e2a",
        ):
            yield event

    async def _pump_sse(
        self,
        sink: SSESink,
        run: Callable[[], Awaitable[None]],
        *,
        request_id: str,
        label: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """把「一个把结果写进 ``SSESink`` 的协程」变成 SSE 事件流。

        两个流式入口（结构化 / 原始信封）只有 ``run`` 不同，其余的任务编排、
        异常兜底、客户端断开时的取消与清理完全一致 —— 这些恰恰是最容易写歪的部分，
        因此收在这里一处维护，避免两份拷贝各自漂移。
        """
        async def _runner() -> None:
            try:
                await run()
            except Exception:  # noqa: BLE001 - 转成终态错误帧，不让流无声中断
                logger.exception("[AgentHTTPServer] 流式 handler 异常 %s", label)
                # 用 offer 而非 queue.put：这里已经在收尾路径上，消费者可能已经走了，
                # 阻塞式 put 会把清理流程一起挂死（与 SSESink.finish 同一道理）。
                await sink.offer(
                    {
                        "request_id": request_id,
                        "status": E2A_RESPONSE_STATUS_FAILED,
                        "is_final": True,
                        "body": {"code": "INTERNAL_ERROR", "error": "stream handler failed"},
                    }
                )
            finally:
                await sink.finish()

        task = asyncio.create_task(_runner())
        try:
            while True:
                item = await sink.queue.get()
                if item is STREAM_DONE:
                    break
                yield {
                    "id": item.get("request_id") or request_id,
                    "event": _frame_event_name(item),
                    "data": json.dumps(item, ensure_ascii=False),
                }
        finally:
            # 客户端断开时取消 handler，避免任务悬挂。
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    # 正常取消路径：上面刚 cancel 过，这里必然走到。
                    pass
                except Exception:  # noqa: BLE001 - 清理阶段不外溢，但要留痕
                    logger.debug(
                        "[AgentHTTPServer] 清理流式 handler task 时异常 %s",
                        label,
                        exc_info=True,
                    )

    def _find_bindable_port(self) -> int | None:
        """端口被占时向上扫描，语义与实例端口组一致（+1000 步进）"""
        for offset in range(PORT_SCAN_RANGE):
            candidate = self._port + offset * PORT_SCAN_STEP
            if candidate > 65535:
                break
            if is_port_available(self._host, candidate):
                return candidate
        return None

    def build_app(self) -> Any:
        from jiuwenswarm.server.agent_http_routes import build_fastapi_app

        return build_fastapi_app(self)

    async def start(self) -> bool:
        """启动 HTTP 服务，返回是否成功。

        **绝不能因 HTTP 起不来而影响 WebSocket 主链路**，这里做了三层防护：

        1. 启动前预检端口占用，占用则直接放弃（不抛异常）。
        2. uvicorn 绑定失败时内部会调 ``sys.exit(1)``，在 task 里会变成
           ``SystemExit`` 逃逸出来把整个 AgentServer 拖垮 —— 用 guard 协程捕获。
        3. 等待确认 ``server.started``，避免"日志说启动了其实没起来"。
        """
        import uvicorn

        bindable = self._find_bindable_port()
        if bindable is None:
            logger.error(
                "[AgentHTTPServer] 端口 %s:%s 起连续 %d 个候选均被占用，HTTP 入口未启动"
                "（WebSocket 不受影响）。请清理占用进程，或设 AGENT_HTTP_PORT / config.yaml http_server.port",
                self._host,
                self._port,
                PORT_SCAN_RANGE,
            )
            return False
        if bindable != self._port:
            logger.warning(
                "[AgentHTTPServer] 端口 %s 被占用，自动切换到 %s（与实例端口组同为 +1000 步进）",
                self._port,
                bindable,
            )
            self._port = bindable

        app = self.build_app()
        config = uvicorn.Config(
            app,
            host=self._host,
            port=self._port,
            log_level="info",
            access_log=False,
            lifespan="on",
        )
        self._server = uvicorn.Server(config)

        async def _serve_guarded() -> None:
            try:
                await self._server.serve()
            except (asyncio.CancelledError, KeyboardInterrupt):
                # Ctrl+C 必须正常传播，否则会挡住 AgentServer 的关闭流程。
                raise
            except SystemExit as exc:  # uvicorn 绑定失败走这里
                logger.error("[AgentHTTPServer] uvicorn 退出(code=%s)，HTTP 入口不可用", exc.code)
            except BaseException:  # noqa: BLE001 - 其余一切仍不外溢
                logger.exception("[AgentHTTPServer] 服务异常终止，HTTP 入口不可用")

        # 交给后台任务，避免阻塞 AgentServer 主流程。
        self._serve_task = asyncio.create_task(_serve_guarded())

        # 最多等 5 秒确认端口真的起来了。
        for _ in range(50):
            if getattr(self._server, "started", False) or self._serve_task.done():
                break
            await asyncio.sleep(0.1)

        if not getattr(self._server, "started", False):
            logger.error("[AgentHTTPServer] 启动未就绪，HTTP 入口不可用（WebSocket 不受影响）")
            # 必须主动收掉 _serve_task：调用方拿到 False 会把 http_server 置 None，
            # 关闭分支（app_agentserver 的 `if http_server is not None`）随之跳过 stop()。
            # 而超时分支的前提正是「还没起来也没失败」—— uvicorn 完全可能在 5 秒窗口之后
            # 才 bind 成功，那就留下一个无人持有、无法停止、却在完整对外服务的监听器。
            # 这里在**启动关键路径**上，不能用默认的 10 秒超时拖住 WebSocket。
            await self.stop(timeout=SHUTDOWN_TIMEOUT_ON_START_FAILURE)
            return False

        logger.info(
            "[AgentHTTPServer] 已启动: http://%s:%s%s", self._host, self._port, API_PREFIX
        )
        return True

    async def stop(self, *, timeout: float = SHUTDOWN_TIMEOUT) -> None:
        """停止 HTTP 服务。

        Args:
            timeout: 等待 uvicorn 收尾的秒数，超时则强制 cancel。
                默认走正常关闭的宽限值；``start`` 失败时的清理在启动关键路径上，
                会传一个更短的值，避免拖慢 WebSocket 主链路。
        """
        if self._server is not None:
            self._server.should_exit = True
        if self._serve_task is not None:
            try:
                await asyncio.wait_for(self._serve_task, timeout=timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._serve_task.cancel()
            except Exception:  # noqa: BLE001
                logger.warning("[AgentHTTPServer] 停止时异常", exc_info=True)
        logger.info("[AgentHTTPServer] 已停止")


def is_valid_req_method(method: str) -> bool:
    try:
        ReqMethod(method)
    except ValueError:
        return False
    return True
