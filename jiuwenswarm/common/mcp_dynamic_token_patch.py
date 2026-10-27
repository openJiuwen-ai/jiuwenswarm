# coding: utf-8
"""Inject a per-request dynamic token into openjiuwen MCP HTTP client auth flows.

Why this exists: 企业版 MCP 网关采用 Keycloak Client Credentials 模式做
Agent-to-Tool 双重授权。**每个网关条目自带 ``dynamic_token_url``**（取动态
token 的端点，存在 = 该网关需要动态 token），而 ``client_id``/``client_secret``
是 **AgentServer 实例层级**的凭据（由上游 Gateway 下发，所有需要动态 token 的
网关共用）。本补丁让被标记网关的 MCP HTTP 请求在**每次出网前**实时取 token
并注入 ``X-Actor-Token`` 头；**取不到 token 时不阻断请求**：照常发起调用，
动态头置空（值 = 空字符串），由网关侧决定放行或 401。

Mechanism (SDK v0.1.16, AgentServer 主进程 site-packages 版):

  A. Replace the ``AuthStrategyRegistry`` HEADER_AND_QUERY strategy. SDK
     clients (``StreamableHttpClient.connect`` / ``SseClient._do_connect``)
     trigger ``TOOL_AUTH`` on **every** connect — including the reconnects
     forced by ``mcp_call_timeout_patch`` — and take the first success
     ``ToolAuthResult.auth_data["auth_provider"]``. ``unified_auth_handler``
     resolves the strategy from ``AuthStrategyRegistry._strategies`` (one slot
     per ``AuthType``) on every trigger, so registering a replacement strategy
     takes effect immediately with no ordering hazard. The original
     ``HeaderQueryAuthStrategy`` is kept as the fallback for un-marked
     servers.

  B. Scope via a process-level **server_id registry (refcount + per-server
     token_url)**, NOT a ContextVar: reconnects run inside
     ``call_tool``/``list_tools`` (outside the registration stack), so a
     ContextVar set around registration is invisible exactly when it matters.
     ``_register_mcp_server`` marks ``server_id`` together with that entry's
     ``dynamic_token_url`` (``mark_dynamic_token_server``) and
     ``_unregister_mcp_server`` unmarks it (``unmark_dynamic_token_server``);
     the strategy resolves the registry by ``auth_config.tool_id`` — reconnect
     / TTL refresh / SSE owner-task reconnects all resolve dynamically.
     Gateways whose entries carry no ``dynamic_token_url`` (request-scoped
     connectors etc.) keep the static behavior.

  C. ``DynamicTokenProvider`` is an ``httpx.Auth``: ``async_auth_flow`` lays
     down the static template headers/query params (same semantics as the
     stock provider), then resolves the current turn's ``X-User-Token`` from
     the (server_id, JSON-RPC id) registry — 实际 HTTP 请求由建连时启动的
     后台任务发送，直接读 ContextVar 拿到的是 connect 时刻快照；发送侧经
     会话写流代理（``_TurnTokenTaggingStream``，在 ``ClientSession.__init__``
     消费 authenticate 留下的归属标记安装——先于首次 send，``initialize``
     握手即带用户令牌；connect 成功后另有兜底安装）按请求 id 登记当轮
     token，provider 侧按请求体 id 精确消费：带 id 的请求只认本次登记值
     （缺失回退静态快照，不沿用上一轮用户令牌）；GET 事件流 / 通知回退
     最近一次登记值。Then it fetches a fresh actor token **before the first
     ``yield``** and injects the dynamic agent-workload header
     ``X-Actor-Token: Bearer <token>`` alongside ``Authorization`` (§2.1 dual
     matrix: user authorization vs agent-workload authorization). A
     token-fetch failure does **not** abort the request: the dynamic header
     is sent empty and the call proceeds (the gateway decides). Concurrency
     is merged with single-flight (in-flight fetches are shared; strictly one
     fetch per request).

Configuration (two levels, both delivered by the upstream Manager):

- **Per-gateway entry** (``mcp.servers`` entry / template row): the optional
  ``dynamic_token_url`` field is THAT gateway's token endpoint — its presence
  marks the gateway as needing dynamic tokens (absent/empty = static headers).
  Each gateway may point at a different token endpoint;
- **Instance level** (``client_id`` / ``client_secret``): one pair per
  AgentServer instance (``client_id`` IS the instance identity in Keycloak),
  shared by all marked gateways. Manager 挂在 Agent 模板行
  ``data.custom_config`` 对象数组里下发；企业配置合并时由
  ``apply_dispatched_credentials`` 提取并整份替换（不经过 config 快照，
  合并未携带合法凭据对即清除）。

``apply_dispatched_credentials`` (wired at the enterprise config merge,
``config_provider._apply_enterprise_policy``) stores the template's instance
credentials wholesale; a merge without the pair clears them. Everything else
is a code-level contract (the
``TOKEN_*`` constants below): POST form ``grant_type=client_credentials`` + the
instance credentials as form fields, response field ``access_token`` (short
plain-text bodies accepted verbatim), injected header ``Authorization: Bearer
<token>``, 5s fetch timeout. Credential hot-update takes effect on the next
fetch without re-registration.
"""  # noqa: E501
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, AsyncGenerator

import httpx
# jiuwenswarm logging 体系（→ full.log），与 mcp_tool_list_refresh_patch 一致；
# 不用 openjiuwen.core.common.logging（其输出走 openjiuwen 自己的 sink/server.log）。
logger = logging.getLogger(__name__)
# 轻量 dataclass 模块（无 Runner 依赖链），可顶层 import；
# auth_callback（含 Runner/callback framework）刻意延迟到 apply() 内。
from openjiuwen.core.foundation.tool.auth.auth import ToolAuthResult

_PATCHED = False
# apply() 时保存的原 HeaderQueryAuthStrategy 引用（fallback / 测试恢复用）。
_ORIGINAL_STRATEGY: Any = None
# server_id -> {"refcount": int, "token_url": str}。多个 DeepAdapter（团队共享
# DeepAgentSpec 并行启动）会对同一 server_id 并发 mark；某一方注册失败 unmark
# 不得影响另一方——计数归零才真正摘除。token_url 为该网关自己的取 token 端点
# （条目级下发，随 mark 登记）。
_DYNAMIC_SERVERS: dict[str, dict[str, Any]] = {}
# server_id -> provider（含专用 token httpx client），注册表命中时复用，
# 归零逐出，避免每次 connect 重建 TLS 连接。
_PROVIDERS: dict[str, "DynamicTokenProvider"] = {}
_REGISTRY_LOCK = threading.Lock()
# 强引用集：asyncio 对 task 只持弱引用，无人引用的后台任务可能被 GC 掉导致
# aclose 永不执行——仿 mcp_config._retire_tasks 的自持模式。
_BACKGROUND_TASKS: set[asyncio.Task] = set()
# 实例级凭据（client_id / client_secret，由企业配置合并从 Agent 模板
# data.custom_config 提取）——所有被标记网关共用；apply_dispatched_credentials
# 整份替换，None = 未下发/清除。
_DISPATCHED_CREDENTIALS: dict[str, str] | None = None
_DISPATCHED_LOCK = threading.Lock()
# 逐轮用户令牌登记表（见下方"逐轮用户令牌登记表"一节：发送侧按 JSON-RPC id
# 登记，provider 按 id 精确消费）。
#: server_id -> {jsonrpc_id: [Authorization 值, ...]}。同 id 多值（两个连接
#: 复用同 provider 且请求 id 撞号）按 FIFO 消费；单连接场景每 id 恰一条。
_TURN_TOKENS: dict[str, dict[Any, list[str]]] = {}
#: server_id -> 最近一次登记的用户 Authorization 值：GET 事件流 / 通知等
#: 无 JSON-RPC id 的请求无法精确归属，回退到它（取不到再回退静态快照）。
_LAST_TURN_TOKEN: dict[str, str] = {}
#: 单网关待消费登记上限（在途请求数量级），超出丢弃最旧——连接死亡导致
#: 永不消费的登记不无限累积。
_MAX_PENDING_TURN_TOKENS = 64

#: 会话归属标记：TOOL_AUTH（authenticate）与 ``ClientSession`` 构造发生在
#: **同一任务**（connect 流程内，中间的 await 不换上下文）——authenticate
#: 对动态 token 网关写下 server_id，会话构造点一次性消费（读后即清），
#: 把逐轮 token 登记代理装到 ``_write_stream`` 上。必须在首次 send 之前：
#: initialize 握手也要求双 token（网关在握手缺用户令牌时直接拒绝）。
_PENDING_TAG_SERVER: contextvars.ContextVar[str] = contextvars.ContextVar(
    "jw_mcp_pending_tag_server", default=""
)

#: 纯文本回退时接受的最大 body 长度（超过按解析失败处理，防止把大页面当 token）。
_MAX_PLAIN_TEXT_TOKEN_LEN = 4096
#: 取 token 失败后的负缓存窗口（秒）：窗口内直接空头降级，不再打 token 服务
#: （防故障期每请求 5s 超时等待 + warning 刷屏）。窗口结束自动重试。
_NEGATIVE_CACHE_TTL_S = 30.0

# ---------------------------------------------------------------------------
# 代码级契约常量（keycloak_api_integration.md §2.2）——url/凭据之外不可配置。
# ---------------------------------------------------------------------------
#: 响应中 token 的字段名。
TOKEN_RESPONSE_FIELD = "access_token"
#: 注入 MCP 请求的动态头名（§2.1 Agent-to-Tool 双重安全矩阵中的智能体负载
#: 授权头；用户授权走逐轮 ``X-User-Token`` → ``Authorization`` 覆盖 + 模板
#: 静态头兜底，见 ``_current_user_authorization``）。
TOKEN_HEADER_NAME = "X-Actor-Token"
#: 动态头值前缀。
TOKEN_HEADER_PREFIX = "Bearer "
#: 单次取 token 超时（秒）。须显著小于 mcp_call_timeout_patch 的 30s 调用超时。
TOKEN_TIMEOUT_S = 5.0

#: 每轮用户令牌的请求级 ext 字段名：网关企业模式把浏览器请求的同名头放进
#: ``metadata['ext']``（web_http_dispatch._build_http_ext），随 E2A 到
#: AgentServer，入口 ``lift_from_metadata`` 抬升到 ContextVar（agent_ws_server
#: dispatch_parsed_request），此处按请求现读。
USER_TOKEN_EXT_KEY = "X-User-Token"
#: 用户令牌值长度上限（httpx 头约束 + ext JSON 4KiB 载体上限之内）。
_MAX_USER_TOKEN_LEN = 4096


class McpDynamicTokenUnavailableError(RuntimeError):
    """Token 服务不可达 / 响应不含 token。消息自包含（URL + 原因）。

    该异常会经 mcp SDK 的 anyio task group 冒出，常被包进 ``ExceptionGroup``，
    repr 可能是它唯一露出的地方——因此 message 必须可独立定位问题。
    """

    def __init__(self, token_url: str, reason: str) -> None:
        super().__init__(
            f"dynamic token fetch failed: url={token_url}: {reason}"
        )
        self.token_url = token_url
        self.reason = reason


@dataclass(frozen=True)
class InstanceCredentials:
    """AgentServer 实例层级的凭据（上游 Gateway 下发，所有标记网关共用）。

    其余契约（方法 / 字段名 / 注入头 / 超时 / 网关各自的 token 端点）不进入
    本结构：网关级 token 端点在 server_id 注册表里，其余为 ``TOKEN_*`` 常量。
    """

    client_id: str
    client_secret: str


# ---------------------------------------------------------------------------
# 下发通道：实例级凭据（apply_dispatched_credentials 写，消费点现读）
# ---------------------------------------------------------------------------


def extract_dispatched_credentials(agent_template_row: Any) -> dict[str, str] | None:
    """从 Agent 模板行 ``data.custom_config`` 提取首个合法凭据对。

    Manager 下发的实例凭据挂在模板行 ``data.custom_config`` 对象数组里（元素
    形如 ``{"client_id": ..., "client_secret": ...}``）；逐条跳过非 dict /
    缺字段 / 空值条目，整行未配置或无合法条目返回 ``None``。取值去除首尾空白。
    """
    if not isinstance(agent_template_row, dict):
        return None
    data = agent_template_row.get("data")
    if not isinstance(data, dict):
        return None
    entries = data.get("custom_config")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        client_id = str(entry.get("client_id") or "").strip()
        client_secret = str(entry.get("client_secret") or "").strip()
        if client_id and client_secret:
            return {"client_id": client_id, "client_secret": client_secret}
    return None


def apply_dispatched_credentials(enterprise: Any) -> None:
    """企业配置到达时，提取其中的实例凭据并整份替换（未携带 → 清除）。

    由企业配置合并管线调用（``config_provider._apply_enterprise_policy``）。
    ``enterprise`` 为鸭子类型的企业配置对象（带 ``agent_template`` /
    ``resource_id`` / ``ref_template_id`` 属性；None = 配置未加载 → 清除）。
    ``custom_config`` 存在但提取不到合法凭据对视为模板配置错误，打
    ``warning``（只带模板定位信息，不带凭据值）。
    """
    global _DISPATCHED_CREDENTIALS
    row = getattr(enterprise, "agent_template", None) if enterprise is not None else None
    credentials = extract_dispatched_credentials(row)
    if credentials is None:
        data = row.get("data") if isinstance(row, dict) else None
        if isinstance(data, dict) and "custom_config" in data:
            logger.warning(
                "[mcp-dynamic-token] agent_template.data.custom_config has no valid "
                "client_id/client_secret pair: resource_id=%s ref_template_id=%s",
                getattr(enterprise, "resource_id", None),
                getattr(enterprise, "ref_template_id", None),
            )
        with _DISPATCHED_LOCK:
            _DISPATCHED_CREDENTIALS = None
        return
    with _DISPATCHED_LOCK:
        _DISPATCHED_CREDENTIALS = dict(credentials)


def read_instance_credentials() -> InstanceCredentials | None:
    """读实例级凭据快照；未下发 / 缺 client_id / 缺 client_secret → None。"""
    with _DISPATCHED_LOCK:
        section = dict(_DISPATCHED_CREDENTIALS) if _DISPATCHED_CREDENTIALS else None
    if not section:
        return None
    client_id = str(section.get("client_id") or "").strip()
    client_secret = str(section.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        return None
    return InstanceCredentials(client_id=client_id, client_secret=client_secret)


# ---------------------------------------------------------------------------
# server_id 注册表（refcount + token_url）
# ---------------------------------------------------------------------------


def mark_dynamic_token_server(server_id: str, token_url: str) -> bool:
    """把进程级注册的网关标记为动态 token 作用域，并登记其 token 端点。

    ``token_url`` 为空（条目未携带 dynamic_token_url）时是 no-op（返回 False，
    该网关保持静态行为）。注册成功路径上 mark 必须与 unregister 路径上 unmark
    配对，见 ``interface_deep._register_mcp_server``。
    """
    token_url = str(token_url or "").strip()
    sid = str(server_id or "").strip()
    if not token_url or not sid:
        return False
    with _REGISTRY_LOCK:
        entry = _DYNAMIC_SERVERS.setdefault(sid, {"refcount": 0, "token_url": token_url})
        entry["refcount"] += 1
        entry["token_url"] = token_url
    return True


def unmark_dynamic_token_server(server_id: str) -> None:
    sid = str(server_id or "").strip()
    if not sid:
        return
    evicted: DynamicTokenProvider | None = None
    with _REGISTRY_LOCK:
        entry = _DYNAMIC_SERVERS.get(sid)
        if entry is None:
            return
        if entry["refcount"] <= 1:
            _DYNAMIC_SERVERS.pop(sid, None)
            evicted = _PROVIDERS.pop(sid, None)
            _TURN_TOKENS.pop(sid, None)
            _LAST_TURN_TOKEN.pop(sid, None)
        else:
            entry["refcount"] -= 1
    if evicted is not None:
        _spawn_quiet_aclose(evicted)


def is_dynamic_token_server(server_id: str) -> bool:
    return _registry_token_url(server_id) is not None


def _registry_token_url(server_id: str) -> str | None:
    """查某网关登记的 token 端点（未标记 → None）。"""
    sid = str(server_id or "").strip()
    if not sid:
        return None
    with _REGISTRY_LOCK:
        entry = _DYNAMIC_SERVERS.get(sid)
    return str(entry["token_url"]) if entry else None


# ---------------------------------------------------------------------------
# 逐轮用户令牌登记表（发送侧按 JSON-RPC id 登记，provider 按 id 精确消费）
# ---------------------------------------------------------------------------
#
# 为什么不直接读 ContextVar：实际 HTTP 请求由**建连时启动的后台任务**
# （mcp SDK streamable_http post_writer / SSE 读流）发送——httpx.Auth 在
# 那个任务的上下文里执行，读到的 ContextVar 是 connect 时刻的快照，逐轮
# 轮换的用户令牌会停在旧值（首轮 A、次轮 B 仍发 A；预热连接永远静态值）。
# 正确通道：``BaseSession.send_request`` 内 ``await self._write_stream.send``
# 在**调用者上下文**执行且消息带 JSON-RPC id —— 把会话写流挂上登记代理
# （``_TurnTokenTaggingStream``，connect 成功后安装），调用侧按
# (server_id, id) 登记本轮 token；provider 侧解析请求体 id 精确消费。


def _record_turn_token(server_id: str, request_id: Any, authorization: str) -> None:
    """发送侧登记：把当前调用者的用户令牌绑到 (server_id, JSON-RPC id)。"""
    sid = str(server_id or "").strip()
    if not sid or request_id is None:
        return
    with _REGISTRY_LOCK:
        bucket = _TURN_TOKENS.setdefault(sid, {})
        bucket.setdefault(request_id, []).append(authorization)
        while len(bucket) > _MAX_PENDING_TURN_TOKENS:
            bucket.pop(next(iter(bucket)))
        _LAST_TURN_TOKEN[sid] = authorization


def _consume_turn_token(server_id: str, request_id: Any) -> str:
    """provider 侧精确消费：返回并移除该 (server_id, id) 的登记（无 → 空串）。"""
    sid = str(server_id or "").strip()
    if not sid:
        return ""
    with _REGISTRY_LOCK:
        values = _TURN_TOKENS.get(sid, {}).get(request_id)
        if not values:
            return ""
        value = values.pop(0)
        if not values:
            _TURN_TOKENS[sid].pop(request_id, None)
        return value


def _last_turn_token(server_id: str) -> str:
    """该网关最近一次登记的用户令牌（无登记 → 空串，调用方回退静态头）。"""
    sid = str(server_id or "").strip()
    if not sid:
        return ""
    with _REGISTRY_LOCK:
        return _LAST_TURN_TOKEN.get(sid, "")


class _TurnTokenTaggingStream:
    """会话写流代理：在**调用者上下文**把本轮用户令牌绑到出站请求上。

    ``ClientSession`` 的所有出站消息经 ``self._write_stream.send`` 发出
    （mcp ``BaseSession.send_request`` 内 ``await``），该调用发生在工具
    调用者的任务里——与 HTTP 实际发送任务（建连时的 post_writer）不同。
    代理在 ``send`` 里识别出站 JSON-RPC **请求**（root 同时有 ``method``
    与 ``id``；响应有 id 无 method、通知有 method 无 id，均不登记），把
    当前请求级 ext 里的 ``X-User-Token`` 登记到 (server_id, id)；provider
    侧按请求体 id 精确消费。登记失败只打 debug，绝不阻断消息发送。

    代理在 connect 成功后替换 ``session._write_stream``（实例属性）。会话
    的接收循环在建连时已用原流进入 ``async with``（关闭走原流），替换后
    的属性读取（各 send 点）才走代理——原流生命周期不受影响。
    """

    def __init__(self, inner: Any, server_id: str) -> None:
        self._inner = inner
        self._server_id = str(server_id or "").strip()

    async def send(self, item: Any) -> None:
        try:
            root = getattr(getattr(item, "message", None), "root", None)
            request_id = getattr(root, "id", None)
            if (
                request_id is not None
                and not isinstance(request_id, bool)
                and getattr(root, "method", None) is not None
            ):
                user_token = _current_user_authorization()
                if user_token:
                    _record_turn_token(self._server_id, request_id, user_token)
        except Exception:  # noqa: BLE001 - 登记失败不阻断消息发送
            logger.debug(
                "[mcp-dynamic-token] turn token tagging failed: server_id=%s",
                self._server_id,
                exc_info=True,
            )
        await self._inner.send(item)

    def __getattr__(self, name: str) -> Any:
        # 仅初始化期可能触发的私有槽位直接抛错，避免递归；其余全委托内层
        # 流（aclose / send_nowait / statistics 等）。
        if name in ("_inner", "_server_id"):
            raise AttributeError(name)
        return getattr(self._inner, name)


def _install_turn_token_tagging(session: Any, server_id: str) -> None:
    """把会话写流换成登记代理（幂等）；结构不符时跳过（回退静态头语义）。"""
    if session is None or not str(server_id or "").strip():
        return
    if getattr(session, "_jw_turn_token_tagged", False):
        return
    inner = getattr(session, "_write_stream", None)
    if inner is None:
        return
    session._write_stream = _TurnTokenTaggingStream(inner, server_id)
    session._jw_turn_token_tagged = True
    logger.debug(
        "[mcp-dynamic-token] turn token tagging installed: server_id=%s",
        server_id,
    )


def _consume_pending_tag_server() -> str:
    """一次性消费会话归属标记（读后即清，防陈旧标记误挂后续无关会话）。"""
    server_id = _PENDING_TAG_SERVER.get()
    if server_id:
        _PENDING_TAG_SERVER.set("")
    return server_id


def _patch_sdk_connect_for_turn_token() -> None:
    """给 MCP HTTP 客户端装逐轮 token 登记代理（发送侧，两类接缝）。

    - ``ClientSession.__init__``（主接缝）：authenticate 在 TOOL_AUTH 里写下
      网关归属标记，会话构造点消费标记并安装代理——发生在**首次 send 之前**，
      ``initialize`` 握手就带上当轮用户令牌（网关握手校验双 token 时否则
      直接连接失败）；
    - connect 成功后的兜底安装：若 ``__init__`` 接缝因 SDK 结构变化未生效，
      至少保证首次调用起的请求被登记（握手退回静态头语义）。

    重连会重建 ``_session``——两条接缝都幂等（标记挂在类方法 / session 实例
    上）。call_timeout_patch 已包装 call_tool/list_tools，与此处包装的
    connect/_do_connect/__init__ 不相交。
    """
    from mcp.client.session import ClientSession
    from openjiuwen.core.foundation.tool.mcp.client.sse_client import SseClient
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )

    original_init = ClientSession.__init__
    if not getattr(original_init, "_jw_turn_token_tagged", False):

        def _tagged_init(
            self: Any, *args: Any, __orig: Any = original_init, **kwargs: Any
        ) -> None:
            __orig(self, *args, **kwargs)
            server_id = _consume_pending_tag_server()
            if server_id:
                _install_turn_token_tagging(self, server_id)

        _tagged_init._jw_turn_token_tagged = True  # type: ignore[attr-defined]
        ClientSession.__init__ = _tagged_init

    for cls, method_name in ((StreamableHttpClient, "connect"), (SseClient, "_do_connect")):
        original = getattr(cls, method_name, None)
        if original is None or getattr(original, "_jw_turn_token_tagged", False):
            continue

        async def _tagged(self: Any, *args: Any, __orig: Any = original, **kwargs: Any) -> Any:
            ok = await __orig(self, *args, **kwargs)
            if ok:
                _install_turn_token_tagging(
                    getattr(self, "_session", None),
                    str(getattr(self, "_server_id", "") or ""),
                )
            return ok

        _tagged._jw_turn_token_tagged = True  # type: ignore[attr-defined]
        setattr(cls, method_name, _tagged)


async def _quiet_aclose(provider: "DynamicTokenProvider") -> None:
    try:
        await provider.aclose()
    except Exception as exc:  # noqa: BLE001 - 逐出/清理绝不影响主流程
        logger.debug("[mcp-dynamic-token] provider aclose failed: %s", exc)


def _spawn_quiet_aclose(provider: "DynamicTokenProvider") -> None:
    """同步上下文里调度 provider 的后台关闭（有 running loop 才调度）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug(
            "[mcp-dynamic-token] no running loop, leaving provider client to GC"
        )
        return
    task = loop.create_task(_quiet_aclose(provider))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


# ---------------------------------------------------------------------------
# token 获取
# ---------------------------------------------------------------------------


def _extract_token(body: str) -> str:
    text = (body or "").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        # 纯文本 token 服务回退：单行、无空白、纯 ASCII 的短 body 直接当 token。
        # 含空白/换行/非 ASCII（HTML 错误页、代理中间页）一律拒绝——注入请求头
        # 会在 httpx/h11 发送阶段抛 LocalProtocolError/UnicodeEncodeError，
        # 绕过"取不到不阻断"的降级语义。
        if (
            len(text) <= _MAX_PLAIN_TEXT_TOKEN_LEN
            and text.isascii()
            and not any(c.isspace() for c in text)
        ):
            return text
        return ""
    if isinstance(payload, dict):
        value = payload.get(TOKEN_RESPONSE_FIELD)
        # 仅接受字符串标量；dict/list 等结构化形态视为无效（str() 会把
        # 逗号/空白带进请求头）。
        if not isinstance(value, str):
            return ""
        return value.strip()
    return ""


def _token_form_fields(creds: InstanceCredentials) -> dict[str, str]:
    """Keycloak Client Credentials 的三个 form 入参（§2.2）。

    ``grant_type`` 是 OAuth 2.0 规范中 Client Credentials 模式的固定字面量。
    ``client_id`` / ``client_secret`` 是实例层级凭据（上游 Gateway 下发）。
    """
    return {
        "grant_type": "client_credentials",
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
    }


async def _request_token(
    client: Any, token_url: str, creds: InstanceCredentials
) -> str:
    try:
        # httpx 对 data=dict 自动按 application/x-www-form-urlencoded 编码（§2.2）。
        response = await client.post(token_url, data=_token_form_fields(creds))
    except Exception as exc:  # noqa: BLE001 - 归一化为专用异常（含超时/连接错误）
        raise McpDynamicTokenUnavailableError(
            token_url, f"{type(exc).__name__}: {exc}"
        ) from exc
    if response.status_code >= 400:
        raise McpDynamicTokenUnavailableError(token_url, f"http {response.status_code}")
    token = _extract_token(response.text)
    if not token:
        raise McpDynamicTokenUnavailableError(
            token_url,
            f"empty token (field={TOKEN_RESPONSE_FIELD!r}, "
            f"body_len={len((response.text or '').strip())})",
        )
    return token


async def fetch_dynamic_token(
    token_url: str,
    creds: InstanceCredentials,
    *,
    client: Any | None = None,
    timeout_s: float | None = None,
) -> str:
    """取一次 token。供 preflight 一次性使用；不传 client 则临时建（async with）。

    ``timeout_s`` 覆盖 ``TOKEN_TIMEOUT_S``（preflight 取 min(token 超时, 探活
    read_timeout)，避免 token 服务慢拖垮整个探活周期）。
    """
    effective_timeout = TOKEN_TIMEOUT_S if timeout_s is None else timeout_s
    if client is not None:
        return await _request_token(client, token_url, creds)
    async with httpx.AsyncClient(timeout=effective_timeout) as probe:
        return await _request_token(probe, token_url, creds)


# ---------------------------------------------------------------------------
# 用户授权（逐轮 X-User-Token → Authorization 覆盖）
# ---------------------------------------------------------------------------


def _current_user_authorization() -> str:
    """读当前对话请求上下文的 ``X-User-Token`` 完整值，用作 Authorization 覆盖。

    上游下发的条目 ``auth_headers`` 是**注册/connect 时刻的快照**（TOOL_AUTH
    只在 connect 触发、provider 按 server_id 跨重连复用），逐轮轮换的用户
    令牌走不了静态链路。因此与 actor token 同机制——每个 MCP 请求出网前
    现读请求级 ContextVar：MCP 工具调用发生在本轮对话栈内（call_tool 及其
    自愈重连都在 turn 栈里），能读到本轮真实值；turn 栈外（后台 TTL 刷新、
    预热重连、cron 轮次）读不到 → 返回空串，回退静态快照头。

    防御：剔首尾空白；含控制字符（含 CR/LF）或非可打印 ASCII 的值一律拒绝
    ——这类值注入请求头会在 httpx/h11 发送阶段抛 LocalProtocolError /
    UnicodeEncodeError，击穿"取不到不阻断"的降级语义。
    """
    try:
        from jiuwenswarm.common.request_ext import get_ext

        raw = get_ext().get(USER_TOKEN_EXT_KEY)
    except Exception:  # noqa: BLE001 - ext 读取失败不阻断请求
        return ""
    value = str(raw or "").strip()
    if not value or len(value) > _MAX_USER_TOKEN_LEN:
        return ""
    if any(not (0x20 <= ord(ch) <= 0x7E) for ch in value):
        return ""
    return value


# ---------------------------------------------------------------------------
# httpx.Auth Provider
# ---------------------------------------------------------------------------


def _request_jsonrpc_id(request: httpx.Request) -> Any:
    """从 MCP HTTP 请求提取 JSON-RPC id（POST 体顶层的 ``id``）。

    非 POST（SSE 事件流的 GET）、空体、非对象体、通知（无 id）、非法 id
    （bool / 结构化）一律返回 ``None``——调用方按"无法精确归属"处理。
    """
    if request.method.upper() != "POST":
        return None
    content = request.content
    if not content:
        return None
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    request_id = payload.get("id")
    if isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
        return None
    return request_id


class DynamicTokenProvider(httpx.Auth):
    """每请求实时获取 token 的 ``httpx.Auth``。

    ``async_auth_flow`` 顺序：
      1. 铺静态模板 headers（对齐 ``AuthHeaderAndQueryProvider`` 语义——
         条目下发的 Authorization 原样保留）；
      2. 合并 auth_query_params；
      3. 用户授权逐轮覆盖：按 (server_id, JSON-RPC id) 登记表取本轮
         ``X-User-Token`` 完整值覆盖静态 ``Authorization``（带 id 的请求
         只认本次登记值，缺失回退静态快照；无 id 的 GET/通知回退最近一次
         登记值）；
      4. 取 token——**失败不阻断请求**：打 warning 后动态头置空（空字符串），
         照常发起调用，由网关决定放行或 401；
      5. 追加注入智能体负载授权头 ``X-Actor-Token: Bearer <token>``（§2.1
         双重安全矩阵，与 ``Authorization`` 并存）；
      6. yield request。
    ``McpDynamicTokenUnavailableError`` 仍由 ``fetch_dynamic_token`` /
    ``_request_token`` 抛出（preflight 场景感知细节用），provider 内消化。
    """

    def __init__(
        self,
        auth_headers: dict[str, str] | None,
        auth_query_params: dict[str, str] | None,
        token_url: str,
        server_id: str,
    ) -> None:
        self.headers = dict(auth_headers or {})
        self.query_params = dict(auth_query_params or {})
        # 网关级 token 端点（注册表按 server_id 持有，fetch 时现查——热更命中）。
        self._token_url = str(token_url or "")
        self._server_id = str(server_id or "")
        self._client: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._inflight: asyncio.Task | None = None
        # 负缓存：取 token 失败后的窗口期（秒），窗口内直接空头降级不再打
        # token 服务（防故障期打爆 + warning 刷屏）。0 = 无窗口（立即重试）。
        self._negative_until = 0.0

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        if self.headers:
            for key, value in self.headers.items():
                request.headers[key] = value
        if self.query_params:
            request.url = request.url.copy_merge_params(self.query_params)
        # 用户授权（逐轮 X-User-Token）：**不在这里读 ContextVar**——本方法
        # 在建连时启动的后台任务里执行，读到的是 connect 时刻上下文（旧值）。
        # 正确取法是发送侧按 JSON-RPC id 登记的登记表（见
        # _TurnTokenTaggingStream）：
        #   - 带 id 的请求（工具调用等）：**只认发送侧为本次调用登记的值**。
        #     登记缺失（本轮没带用户令牌的后台轮次 / cron / 排队续跑）回退
        #     静态快照——绝不沿用上一轮用户的令牌（身份串用）；
        #   - 无 id 的请求（GET 事件流 / 通知）：无法按调用归属，回退该网关
        #     最近一次登记值；都没有保留静态快照头，不阻断请求。
        request_id = _request_jsonrpc_id(request)
        if request_id is not None:
            user_authorization = _consume_turn_token(self._server_id, request_id)
        else:
            user_authorization = _last_turn_token(self._server_id)
        if user_authorization:
            request.headers["Authorization"] = user_authorization
        try:
            token = await self._get_token()
        except McpDynamicTokenUnavailableError:
            # 取不到 token 不阻断请求：动态头置空（空字符串），照常发起调用，
            # 由网关决定放行或 401。CancelledError 等 BaseException 不在此消化。
            request.headers[TOKEN_HEADER_NAME] = ""
        else:
            request.headers[TOKEN_HEADER_NAME] = TOKEN_HEADER_PREFIX + token
        yield request

    async def _get_token(self) -> str:
        now = time.monotonic()
        if now < self._negative_until:
            # 负缓存窗口内：直接失败（空头降级），不再打 token 服务。
            raise McpDynamicTokenUnavailableError(
                self._token_url, "negative cache window (recent fetch failure)"
            )
        # single-flight：进行中的 fetch 直接被并发调用者复用（合并而非缓存，
        # 顺序请求仍每次实时取）。
        inflight = self._inflight
        if inflight is not None and not inflight.done():
            return await inflight
        task = asyncio.get_running_loop().create_task(self._fetch_token())
        self._inflight = task
        try:
            token = await task
        except Exception:
            # 失败进入负缓存：窗口内后续请求直接空头降级（不再打 token 服务）。
            self._negative_until = time.monotonic() + _NEGATIVE_CACHE_TTL_S
            raise
        finally:
            if self._inflight is task:
                self._inflight = None
        self._negative_until = 0.0
        return token

    async def _fetch_token(self) -> str:
        # 现读实例凭据（下发快照热更即时生效）；token 端点用创建时登记的
        # 网关级 url（注册表按 server_id 持有，重连/热更均命中）。
        creds = read_instance_credentials()
        if creds is None:
            # 凭据被清除（下发快照未携带段）→ 专用异常 → async_auth_flow
            # 置空头降级；不用 AttributeError 穿透。
            raise McpDynamicTokenUnavailableError(
                self._token_url, "instance credentials not available"
            )
        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is not loop:
            if self._client is not None:
                # loop 变更：丢弃旧 client（跨 loop await aclose 不可靠），交 GC。
                logger.debug(
                    "[mcp-dynamic-token] event loop changed, dropping old "
                    "token client: server_id=%s",
                    self._server_id,
                )
            self._client = httpx.AsyncClient(timeout=TOKEN_TIMEOUT_S)
            self._loop = loop
        return await _request_token(self._client, self._token_url, creds)

    async def aclose(self) -> None:
        client = self._client
        self._client = None
        self._loop = None
        if client is not None:
            try:
                await client.aclose()
            except Exception as exc:  # noqa: BLE001 - 清理不影响主流程
                logger.debug(
                    "[mcp-dynamic-token] token client aclose failed: "
                    "server_id=%s error=%s",
                    self._server_id,
                    exc,
                )


# ---------------------------------------------------------------------------
# TOOL_AUTH 策略替换
# ---------------------------------------------------------------------------


class DynamicTokenAuthStrategy:
    """Duck-typed 替换 ``HeaderQueryAuthStrategy``（SDK 只调 ``authenticate``）。

    判定按 ``auth_config.tool_id``（SDK connect 时固定传 server_id）查
    server_id 注册表——与调用栈无关，注册期 connect、call 期自愈重连、TTL
    刷新重连全部命中；未标记的 server（前端请求级连接器等）委托原策略，
    静态行为不变。
    """

    #: 由 ``apply_mcp_dynamic_token_patch`` 注入 ``AuthType.HEADER_AND_QUERY``
    #: （模块顶层不 import auth_callback，避免拉起 Runner 依赖链）。
    auth_type: Any = None

    async def authenticate(self, auth_config: Any, **kwargs: Any) -> ToolAuthResult:
        original = _ORIGINAL_STRATEGY
        if original is None:
            return ToolAuthResult(
                success=False,
                auth_data={},
                message="dynamic token patch not applied; no static fallback",
            )
        creds = read_instance_credentials()
        tool_id = str(getattr(auth_config, "tool_id", "") or "").strip()
        token_url = _registry_token_url(tool_id) if tool_id else None
        if creds is None or not token_url:
            # 静态网关：清掉可能残留的陈旧归属标记，防止同任务里先前一次
            # 失败 connect 的标记把无关会话错挂到别的网关名下。
            _PENDING_TAG_SERVER.set("")
            return await original().authenticate(auth_config, **kwargs)
        with _REGISTRY_LOCK:
            provider = _PROVIDERS.get(tool_id)
            if provider is None:
                provider = DynamicTokenProvider(
                    auth_config.config.get("auth_headers"),
                    auth_config.config.get("auth_query_params"),
                    token_url,
                    tool_id,
                )
                _PROVIDERS[tool_id] = provider
        # 归属标记：connect 流程内 authenticate 与 ClientSession 构造同任务，
        # 构造点消费标记装上登记代理（首次 initialize 握手即带用户令牌）。
        _PENDING_TAG_SERVER.set(tool_id)
        return ToolAuthResult(
            success=True,
            auth_data={"auth_provider": provider},
            message="dynamic token authentication configured",
        )


def apply_mcp_dynamic_token_patch() -> bool:
    """把 HEADER_AND_QUERY 鉴权策略替换为动态 token 感知版本。幂等。

    必须先主动 import ``auth_callback``：SDK client 是 connect 时才 lazy
    import 它（注册 unified_auth_handler + 原 HeaderQueryAuthStrategy），
    抢先 import 确保替换发生在原策略注册之后、首次 connect 之前。
    """
    global _PATCHED, _ORIGINAL_STRATEGY
    if _PATCHED:
        return True
    from openjiuwen.core.foundation.tool.auth import auth_callback as _auth_callback  # noqa: F401
    from openjiuwen.core.foundation.tool.auth.auth_callback import (
        AuthStrategyRegistry,
        AuthType,
        HeaderQueryAuthStrategy,
    )

    _ORIGINAL_STRATEGY = HeaderQueryAuthStrategy
    DynamicTokenAuthStrategy.auth_type = AuthType.HEADER_AND_QUERY
    AuthStrategyRegistry.register(DynamicTokenAuthStrategy)
    # 逐轮用户令牌：connect 成功后给会话写流挂登记代理（发送侧，见
    # _TurnTokenTaggingStream）；幂等，重复 apply 无副作用。
    _patch_sdk_connect_for_turn_token()
    _PATCHED = True
    return True


def _reset_dynamic_token_state_for_tests() -> None:
    """测试隔离：清空补丁态与注册表（不触碰 SDK registry 的最终槽位归属）。"""
    global _PATCHED, _ORIGINAL_STRATEGY, _DISPATCHED_CREDENTIALS
    with _REGISTRY_LOCK:
        providers = list(_PROVIDERS.values())
        _PROVIDERS.clear()
        _DYNAMIC_SERVERS.clear()
        _TURN_TOKENS.clear()
        _LAST_TURN_TOKEN.clear()
    with _DISPATCHED_LOCK:
        _DISPATCHED_CREDENTIALS = None
    _PATCHED = False
    _ORIGINAL_STRATEGY = None
    DynamicTokenAuthStrategy.auth_type = None
    for provider in providers:
        _spawn_quiet_aclose(provider)
