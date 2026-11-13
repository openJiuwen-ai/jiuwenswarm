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
     stock provider), then resolves the current turn's ``X-User-Token`` by
     per-request binding — 实际 HTTP 请求由建连时启动的后台任务发送，直接
     读 ContextVar 拿到的是 connect 时刻快照，全局 holder 也无法表达
     "这个请求是谁发的"；发送侧经会话写流代理（``_TurnTokenTaggingStream``，
     在 ``ClientSession.__init__`` 消费 authenticate 留下的归属标记安装——
     先于首次 send，直持**本连接的 provider**）按消息形态登记：请求按
     JSON-RPC id 精确登记、通知按连接 FIFO 配对（无令牌通知入 "" 哨兵）、
     响应不登记；登记表内聚在 provider 实例上——多会话各自建连互不可见
     （并发 session 交替不串身份）；
     provider 侧按请求体形态消费：请求精确消费（未登记回退静态快照，
     绝不沿用他人令牌）、通知出队、响应 / GET 事件流 / SSE 重连 / DELETE
     取 last-known（令牌换成 B 后重连即带 B）。Then it fetches a fresh
     actor token **before the first ``yield``** and injects the dynamic
     agent-workload header ``X-Actor-Token: Bearer <token>`` alongside
     ``Authorization`` (§2.1 dual matrix: user authorization vs
     agent-workload authorization). A token-fetch failure does **not**
     abort the request: the dynamic header is sent empty and the call
     proceeds (the gateway decides). Concurrency is merged with
     single-flight (in-flight fetches are shared; strictly one fetch per
     request).

  D. SDK lifecycle seams: the write-stream proxy implements ``__aenter__`` /
     ``__aexit__`` because ``BaseSession._receive_loop`` holds the write
     stream with ``async with`` (special methods bypass ``__getattr__``).
     ``SseClient`` and ``StreamableHttpClient`` run ``connect`` / ``reconnect``
     on a long-lived owner task whose ContextVars are frozen at creation, so
     both classes' ``_submit`` captures the caller's token and binds it to
     **that command** (set/reset inside the owner task) — otherwise a
     reconnect issued during a later turn (self-healing / TTL refresh) would
     handshake with the first connect's stale token. A connect-window override
     (``_CONNECT_USER_TOKEN``) then makes the ``initialize`` handshake,
     ``notifications/initialized`` and the SSE ``GET`` stream carry the
     current caller's token; concurrent reconnects each keep their own
     submitter's token instead of overwriting a shared instance value.
     The write-stream proxy also owns the connection-level provider
     lifecycle: when the session closes (including reconnect teardown) it
     detaches and ``aclose``s that connection's provider/token HTTP client,
     so providers do not accumulate until the whole MCP server is unmarked.

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
``enterprise.config.config_provider._apply_enterprise_policy``) stores the template's instance
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
import weakref
from collections import deque
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
_REGISTRY_LOCK = threading.Lock()
# 强引用集：asyncio 对 task 只持弱引用，无人引用的后台任务可能被 GC 掉导致
# aclose 永不执行——仿 mcp_config._retire_tasks 的自持模式。
_BACKGROUND_TASKS: set[asyncio.Task] = set()
# 实例级凭据（client_id / client_secret，由企业配置合并从 Agent 模板
# data.custom_config 提取）——所有被标记网关共用；apply_dispatched_credentials
# 整份替换，None = 未下发/清除。
_DISPATCHED_CREDENTIALS: dict[str, str] | None = None
_DISPATCHED_LOCK = threading.Lock()

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

# ---------------------------------------------------------------------------
# 条目级 ssl_verify：与 ``dynamic_token_url`` 同处下发（``mcp_entry`` 透传键，
# 随企业合并整份出现在 ``mcp.servers[]`` 条目上，零解析）、**绑定生效**——
# 仅动态 token 网关（条目携带 ``dynamic_token_url``）显式配置
# ``"ssl_verify": False`` 时，**取动态 token的 HTTP 请求**跳过证书校验
# （自签名 Keycloak 等）；其余一律默认校验。绑定在唯一落点收敛：
# ``build_mcp_server_config`` 只在两者同时存在时把 ``ssl_verify: False`` 放进
# ``McpServerConfig.params``，注册时随 ``mark_dynamic_token_server`` 登记到
# server_id 注册表，取 token 的客户端按 server_id 现读。
# ---------------------------------------------------------------------------
#: 条目里 ssl_verify 的键名（``mcp_entry`` 透传键）。
SSL_VERIFY_ENTRY_KEY = "ssl_verify"

# ---------------------------------------------------------------------------
# SDK / 会话私有槽位与幂等标记名（G.CLS.11：类外不得以 ``obj._xxx`` 字面量
# 访问受保护成员——monkeypatch 接缝一律经常量 + setattr/getattr 访问）。
# ---------------------------------------------------------------------------
#: 会话写流槽位（``ClientSession`` 实例属性，登记代理替换目标）。
_SESSION_WRITE_STREAM_ATTR = "_write_stream"
#: 幂等标记：会话已装登记代理 / SDK 方法已包装（挂在实例与方法上）。
_TURN_TOKEN_TAGGED_ATTR = "_jw_turn_token_tagged"
#: 幂等标记：``SseClient._submit`` 已包上按命令捕获令牌的作用域。
_TOKEN_SCOPED_ATTR = "_jw_token_scoped"
#: ``SseClient`` / ``StreamableHttpClient`` 命令提交私有方法（owner task 入口）。
_SDK_SUBMIT_ATTR = "_submit"
#: ``SseClient`` 建连私有方法（connect/reconnect 实际执行体）。
_SDK_SSE_DO_CONNECT_ATTR = "_do_connect"


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

    由企业配置合并管线调用（``enterprise.config.config_provider._apply_enterprise_policy``）。
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


def mark_dynamic_token_server(
    server_id: str, token_url: str, *, ssl_verify: bool = True
) -> bool:
    """把进程级注册的网关标记为动态 token 作用域，并登记 token 端点与 ssl_verify。

    ``token_url`` 为空（条目未携带 dynamic_token_url）时是 no-op（返回 False，
    该网关保持静态行为）。``ssl_verify=False`` 表示取该网关动态 token 时跳过
    证书校验（条目级配置，见 ``SSL_VERIFY_ENTRY_KEY``）。注册成功路径上 mark
    必须与 unregister 路径上 unmark 配对，见 ``interface_deep._register_mcp_server``。
    """
    token_url = str(token_url or "").strip()
    sid = str(server_id or "").strip()
    if not token_url or not sid:
        return False
    with _REGISTRY_LOCK:
        entry = _DYNAMIC_SERVERS.setdefault(
            sid, {"refcount": 0, "token_url": token_url}
        )
        entry["refcount"] += 1
        entry["token_url"] = token_url
        entry[SSL_VERIFY_ENTRY_KEY] = bool(ssl_verify)
    return True


def unmark_dynamic_token_server(server_id: str) -> None:
    sid = str(server_id or "").strip()
    if not sid:
        return
    evicted: list[DynamicTokenProvider] = []
    with _REGISTRY_LOCK:
        entry = _DYNAMIC_SERVERS.get(sid)
        if entry is None:
            return
        if entry["refcount"] <= 1:
            _DYNAMIC_SERVERS.pop(sid, None)
            _LATEST_PROVIDERS.pop(sid, None)
            evicted = [p for p in _ACTIVE_PROVIDERS if p.server_id == sid]
            for provider in evicted:
                _ACTIVE_PROVIDERS.discard(provider)
        else:
            entry["refcount"] -= 1
    for provider in evicted:
        _spawn_quiet_aclose(provider)


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


def _registry_ssl_verify(server_id: str) -> bool:
    """查某网关登记的 ssl_verify（未标记 / 未配置 → True）。"""
    sid = str(server_id or "").strip()
    if not sid:
        return True
    with _REGISTRY_LOCK:
        entry = _DYNAMIC_SERVERS.get(sid)
    if not entry:
        return True
    return entry.get(SSL_VERIFY_ENTRY_KEY) is not False


# ---------------------------------------------------------------------------
# 逐轮用户令牌：凭据绑定到具体请求（写流代理按调用者上下文登记）
# ---------------------------------------------------------------------------
#
# 为什么不能从全局 holder 推断（评审 ①②③）：实际 HTTP 请求由建连时启动的
# 后台任务（post_writer / SSE 读流）发送，provider 在那个上下文里无法知道
# "这个请求是谁发的"——holder 只能给出"最近在途的某个值"，无令牌的并发
# 请求会带上别人的在途值、通知与 SSE 重连拿不到归属。
#
# 正确通道：``BaseSession.send_request`` 内 ``await self._write_stream.send``
# 在**调用者上下文**执行——把会话写流挂上登记代理，在 send 点按消息形态
# 登记：
#   - 出站**请求**（method+id，如 tools/call / initialize）→ 按
#     (server_id, JSON-RPC id) 精确登记；provider 解析请求体 id 消费，
#     未登记的请求（本轮没带令牌的后台轮次 / cron）回退静态快照，
#     绝不沿用他人令牌；
#   - 出站**通知**（method 无 id，如 notifications/initialized）→ 无 id
#     可精确归属，按网关 FIFO 队列配对：send 侧逐条入队（调用者上下文
#     令牌，无令牌则入 "" 哨兵），通知 POST 逐条出队——写流与 POST 同为
#     FIFO，配对精确；"" 哨兵防止无令牌通知误配他人登记值；
#   - 出站**响应**（id 无 method，回服务器 ping 等）→ 不登记（响应的 id
#     属服务器请求 id 空间，与客户端请求撞号，消费会误配）。
# 每次**非空**登记同步更新 last-known；GET 事件流 / SSE 重连 / DELETE 等
# 无法按消息归属的请求取 last-known（最近一次已知令牌，令牌换成 B 后
# 重连即带 B），无已知值回退静态快照。

#: 存活 provider 集合（每次 connect 的 TOOL_AUTH 新建一个，绑到该连接的
#: 会话写流代理上）。登记表全部内聚在 provider 实例上——**凭据按连接绑定**：
#: 同一网关被多个会话各自建连时，各连接的请求 id 空间 / 通知 FIFO /
#: last-known 互不可见，同 id 撞号、通知错配、GET 重连串身份从根上消除。
_ACTIVE_PROVIDERS: set["DynamicTokenProvider"] = set()
#: server_id -> 最近一次 TOOL_AUTH 创建的 provider（connect 成功后的兜底
#: 安装接缝用；主接缝经归属标记直持 provider 引用，无歧义）。
_LATEST_PROVIDERS: dict[str, "DynamicTokenProvider"] = {}
#: 单连接待消费登记上限（在途消息数量级），超出丢弃最旧——连接死亡导致
#: 永不消费的登记不无限累积。
_MAX_PENDING_TURN_TOKENS = 64
_MAX_PENDING_NOTIF_TOKENS = 64

#: 会话归属标记：TOOL_AUTH（authenticate）与 ``ClientSession`` 构造发生在
#: **同一任务**（connect 流程内，中间的 await 不换上下文）——authenticate
#: 对动态 token 网关写下**本次连接的 provider 实例**，会话构造点一次性
#: 消费（读后即清），把登记代理装到 ``_write_stream`` 上并直持该
#: provider。必须在首次 send 之前：initialize 握手也要求双 token（网关
#: 在握手缺用户令牌时直接拒绝）。
_PENDING_TAG_SERVER: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "jw_mcp_pending_tag_server", default=None
)

#: 连接期用户令牌覆盖（**仅在 connect/reconnect 期间非 UNSET**）。
#:
#: 为什么需要它：``SseClient`` 的 connect/reconnect 跑在**长期 owner task**
#: 里（ConnectQueue），owner task 的 ContextVar 是**创建时快照**——重连时
#: 在该任务里读请求级 ext 得到的是首次建连那一刻的值（甚至为空），
#: initialize / notifications/initialized / GET /sse 握手会带错用户身份。
#: ``SseClient.connect/reconnect`` 在**调用者上下文**把当轮令牌记到实例上，
#: ``_do_connect`` 包装在 owner task 里把它写入本 ContextVar（set/reset），
#: 使握手期所有消息（含 SSE 的 GET 流）取到当次调用者的令牌；连接结束即
#: 复位，后续普通请求仍按请求级登记/当前 ext 取值。
_UNSET: Any = object()
_CONNECT_USER_TOKEN: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "jw_mcp_connect_user_token", default=_UNSET
)


def _effective_send_authorization() -> str:
    """发送侧取本轮用户令牌：连接期（owner task 内）优先用调用者捕获值。

    ``SseClient`` 的 owner task 上下文在创建时冻结，连接期直接读 ext 会拿到
    首次建连的旧值；``_CONNECT_USER_TOKEN`` 在 connect/reconnect 期间由
    ``_do_connect`` 包装写入 **owner task 自己的上下文**，覆盖该旧值。
    连接结束后复位为 UNSET，普通请求回到请求级 ext 语义。
    """
    override = _CONNECT_USER_TOKEN.get()
    if override is not _UNSET:
        return str(override or "")
    return _current_user_authorization()


def _weak_session_ref(session: Any) -> Any:
    """会话弱引用；不可弱引用的对象（测试替身等）退化为 None。"""
    if session is None:
        return None
    try:
        return weakref.ref(session)
    except TypeError:
        return None


class _TurnTokenTaggingStream:
    """会话写流代理：在**调用者上下文**把本轮用户令牌绑到出站消息上。

    ``ClientSession`` 的所有出站消息经 ``self._write_stream.send`` 发出
    （mcp ``BaseSession.send_request`` / ``send_notification`` 内 ``await``），
    该调用发生在发起方的任务里——与 HTTP 实际发送任务（建连时的
    post_writer）不同。代理在 ``send`` 里按消息形态登记：请求（method+id）
    入 id 登记表、通知（method 无 id）入 FIFO 队列、响应（id 无 method）
    跳过。登记失败只打 debug，绝不阻断消息发送。

    代理直持**本连接的 provider 实例**（归属标记下发）——登记表内聚在
    provider 上，多会话各自建连时互不可见（同 id 撞号 / 通知错配 /
    GET 重连串身份从根上消除）。代理在 ``ClientSession.__init__`` 消费
    归属标记时替换 ``session._write_stream``（实例属性，先于首次 send
    ——initialize 握手即在登记范围内）。会话的接收循环随后以代理进入
    ``async with``（关闭走代理并委托内层流）。
    """

    def __init__(
        self,
        inner: Any,
        provider: "DynamicTokenProvider",
        session: Any = None,
    ) -> None:
        self._inner = inner
        self._provider = provider
        # 会话弱引用：会话关闭时据此确认 provider 仍归本会话所有，避免误关
        # 已被其它会话接管的 provider。
        self._session_ref = _weak_session_ref(session)

    @property
    def _server_id(self) -> str:
        return self._provider.server_id

    async def send(self, item: Any) -> None:
        try:
            root = getattr(getattr(item, "message", None), "root", None)
            method = getattr(root, "method", None)
            if method is not None:  # 请求 / 通知；响应（无 method）不登记
                user_token = _effective_send_authorization()
                request_id = getattr(root, "id", None)
                if request_id is None:
                    # 通知（无 id）：FIFO 配对，无令牌入 "" 哨兵占位
                    self._provider.push_notification_token(user_token)
                elif isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
                    # 非法 id：两侧都无法精确归属——不进 id 表也不进通知
                    # FIFO（防污染配对），其 POST 走 last-known。
                    logger.debug(
                        "[mcp-dynamic-token] skip tagging, invalid request id: "
                        "server_id=%s id=%r",
                        self._provider.server_id,
                        request_id,
                    )
                elif user_token:
                    self._provider.record_request_token(request_id, user_token)
        except Exception:  # noqa: BLE001 - 登记失败不阻断消息发送
            logger.debug(
                "[mcp-dynamic-token] turn token tagging failed: server_id=%s",
                self._provider.server_id,
                exc_info=True,
            )
        await self._inner.send(item)

    async def __aenter__(self) -> "_TurnTokenTaggingStream":
        # ``BaseSession._receive_loop`` 以 ``async with (read_stream, write_stream)``
        # 持有写流；上下文管理器协议按**类型**查找特殊方法，不经过
        # ``__getattr__``——不显式实现会让真实会话在启动接收循环时
        # AttributeError/TypeError，接收任务退出、SSE 读流关闭
        # （initialize 超时）。这里委托内层流的进入/退出。
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        try:
            return await self._inner.__aexit__(exc_type, exc, tb)
        finally:
            await self._release_provider()

    async def aclose(self) -> None:
        # 某些 SDK 路径直接 aclose 写流而不走 async with；两条路径都回收。
        try:
            await self._inner.aclose()
        finally:
            await self._release_provider()

    async def _release_provider(self) -> None:
        provider = self._provider
        session = self._session_ref() if self._session_ref is not None else None
        if provider is None or not provider.release_session(session):
            return
        await _release_active_provider(provider)

    def __getattr__(self, name: str) -> Any:
        # 仅初始化期可能触发的私有槽位直接抛错，避免递归；其余全委托内层
        # 流（send_nowait / statistics 等）。
        if name in ("_inner", "_provider", "_session_ref"):
            raise AttributeError(name)
        return getattr(self._inner, name)


def _install_turn_token_tagging(session: Any, provider: Any) -> None:
    """把会话写流换成登记代理（幂等）；结构不符时跳过（回退静态头语义）。"""
    if session is None or provider is None:
        return
    if getattr(session, _TURN_TOKEN_TAGGED_ATTR, False):
        return
    inner = getattr(session, _SESSION_WRITE_STREAM_ATTR, None)
    if inner is None:
        return
    # 归属绑定：provider 每次 connect 新建；主接缝消费归属标记时它尚未被
    # 任何会话持有。兜底安装若发现该 provider 已属于另一个存活会话，必须
    # 跳过——否则会把别人连接的凭据/生命周期挂到本会话上。
    if not provider.bind_session(session):
        logger.debug(
            "[mcp-dynamic-token] provider already owned by another session, "
            "skip tagging: server_id=%s",
            provider.server_id,
        )
        return
    setattr(
        session, _SESSION_WRITE_STREAM_ATTR, _TurnTokenTaggingStream(inner, provider, session)
    )
    setattr(session, _TURN_TOKEN_TAGGED_ATTR, True)
    logger.debug(
        "[mcp-dynamic-token] turn token tagging installed: server_id=%s",
        provider.server_id,
    )


def _consume_pending_tag_provider() -> Any:
    """一次性消费会话归属标记（读后即清，防陈旧标记误挂后续无关会话）。"""
    provider = _PENDING_TAG_SERVER.get()
    if provider is not None:
        _PENDING_TAG_SERVER.set(None)
    return provider


def _patch_sdk_session_tagging() -> None:
    """给 MCP 会话装逐轮用户令牌登记代理（发送侧，两类接缝，幂等）。

    - ``ClientSession.__init__``（主接缝）：authenticate 在 TOOL_AUTH 里写下
      网关归属标记，会话构造点消费标记并安装代理——发生在**首次 send 之前**，
      ``initialize`` 握手与 ``notifications/initialized`` 都带当轮用户令牌；
    - connect 成功后的兜底安装：若 ``__init__`` 接缝因 SDK 结构变化未生效，
      至少保证首次调用起的请求被登记（握手退回静态头语义）。

    连接期令牌覆盖（``_CONNECT_USER_TOKEN``）：Streamable HTTP 的 connect
    在调用者任务里执行，直接读 ext 即可；SSE 的 connect/reconnect 跑在长期
    owner task 中，其 ContextVar 是创建时快照——因此包装
    ``SseClient._submit``，在**调用者上下文**捕获当轮令牌并绑定到**本次
    命令**（在 owner task 执行该命令时 set/reset），``_do_connect`` 包装据此
    使用本次操作自己的凭据。这样并发重连时后加入的等待者不会覆盖先发起
    者的令牌，initialize / initialized / GET/sse 握手都带本次调用者的值
    （重连换人时不沿用首连的旧值，也不被并发等待者串改）。

    连接级回收：写流代理持有本会话的 provider，会话关闭（含重连的
    disconnect 阶段）时摘除并关闭该 provider 的 token HTTP 客户端，不再
    等到整个 MCP 服务 unmark 才清理。

    重连会重建 ``_session``——所有接缝都幂等（标记挂在类方法 / session 实例
    上）。call_timeout_patch 包装 call_tool/list_tools，与本接缝不相交。
    """
    from mcp.client.session import ClientSession
    from openjiuwen.core.foundation.tool.mcp.client.sse_client import SseClient
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )

    original_init = ClientSession.__init__
    if not getattr(original_init, _TURN_TOKEN_TAGGED_ATTR, False):

        def _tagged_init(
            self: Any, *args: Any, __orig: Any = original_init, **kwargs: Any
        ) -> None:
            __orig(self, *args, **kwargs)
            provider = _consume_pending_tag_provider()
            if provider is not None:
                _install_turn_token_tagging(self, provider)

        setattr(_tagged_init, _TURN_TOKEN_TAGGED_ATTR, True)
        ClientSession.__init__ = _tagged_init

    def _make_connect_wrapper(original: Any, resolve_token: Any) -> Any:
        async def _tagged(self: Any, *args: Any, **kwargs: Any) -> Any:
            pending = _CONNECT_USER_TOKEN.set(resolve_token(self))
            try:
                ok = await original(self, *args, **kwargs)
            finally:
                _CONNECT_USER_TOKEN.reset(pending)
            if ok:
                with _REGISTRY_LOCK:
                    provider = _LATEST_PROVIDERS.get(
                        str(getattr(self, "_server_id", "") or "")
                    )
                _install_turn_token_tagging(getattr(self, "_session", None), provider)
            else:
                # 建连失败且会话构造点没有消费归属标记（失败发生在
                # ClientSession 之前）时，provider 没有任何会话持有——这里
                # 主动回收，避免失败重试累积 provider 与 token HTTP 客户端。
                leftover = _consume_pending_tag_provider()
                if leftover is not None:
                    await _release_active_provider(leftover)
            return ok

        setattr(_tagged, _TURN_TOKEN_TAGGED_ATTR, True)
        return _tagged

    # Streamable HTTP connect 包装：连接成功后的兜底登记安装（_LATEST_
    # PROVIDERS → _install_turn_token_tagging）与建连失败时的遗留 provider
    # 回收。令牌按命令绑定由下方 _submit 包装完成；这里的连接期覆盖仅对
    # 未经 _submit 的直接调用兜底（当前 SDK 不会走到）。
    original_streamable = getattr(StreamableHttpClient, "connect", None)
    if original_streamable is not None and not getattr(
        original_streamable, _TURN_TOKEN_TAGGED_ATTR, False
    ):
        StreamableHttpClient.connect = _make_connect_wrapper(
            original_streamable, lambda _self: _current_user_authorization()
        )

    # Streamable HTTP / SSE：SDK 的 connect 与 reconnect 都经 ``_submit``
    # 提交到**长期 owner task** 执行——首个命令 ``create_task`` 时继承调用
    # 者上下文，首次 connect 恰好正确；owner task 存活期间（自愈重连 / TTL
    # 刷新，@with_reconnect 在用户轮次内触发且 owner task 不重建）命令跑在
    # 创建时冻结的上下文里，握手只会读到首次建连者的陈旧令牌。因此对两个
    # 客户端统一包装 ``_submit``：在**每次提交的调用者上下文**捕获当轮令牌
    # 并绑定到**本次命令**（owner task 执行该命令时 set/reset
    # ``_CONNECT_USER_TOKEN``），使每次 connect / reconnect 的握手
    # （initialize / notifications/initialized / GET 事件流）按当次发起者
    # 取值；并发重连各自绑定互不覆盖——不能把令牌写在客户端实例属性上，
    # 后来者会覆盖先发起者的值；reconnect 的去重等待者不提交命令，自然也
    # 不会覆盖先发起者。
    def _make_submit_scoped(original: Any) -> Any:
        async def _scoped_submit(
            self: Any, cmd: Any, __orig: Any = original
        ) -> Any:
            captured = _current_user_authorization()

            async def _run() -> Any:
                pending = _CONNECT_USER_TOKEN.set(captured)
                try:
                    return await cmd()
                finally:
                    _CONNECT_USER_TOKEN.reset(pending)

            return await __orig(self, _run)

        setattr(_scoped_submit, _TOKEN_SCOPED_ATTR, True)
        return _scoped_submit

    for client_cls in (StreamableHttpClient, SseClient):
        original_submit = getattr(client_cls, _SDK_SUBMIT_ATTR, None)
        if original_submit is not None and not getattr(
            original_submit, _TOKEN_SCOPED_ATTR, False
        ):
            setattr(
                client_cls, _SDK_SUBMIT_ATTR, _make_submit_scoped(original_submit)
            )

    original_sse_connect = getattr(SseClient, _SDK_SSE_DO_CONNECT_ATTR, None)
    if original_sse_connect is not None and not getattr(
        original_sse_connect, _TURN_TOKEN_TAGGED_ATTR, False
    ):
        def _resolve_sse_token(client: Any) -> str:
            override = _CONNECT_USER_TOKEN.get()
            # _submit 包装已按“本次命令”写入发起者令牌（并发重连互不覆盖）。
            # 未经 _submit 直接调用 _do_connect 时退回当前上下文。
            if override is not _UNSET:
                return str(override or "")
            return _current_user_authorization()

        setattr(
            SseClient,
            _SDK_SSE_DO_CONNECT_ATTR,
            _make_connect_wrapper(original_sse_connect, _resolve_sse_token),
        )


async def _quiet_aclose(provider: "DynamicTokenProvider") -> None:
    try:
        await provider.aclose()
    except Exception as exc:  # noqa: BLE001 - 逐出/清理绝不影响主流程
        logger.debug("[mcp-dynamic-token] provider aclose failed: %s", exc)


def _detach_active_provider(provider: "DynamicTokenProvider") -> bool:
    """从存活集合摘除 provider；返回本次是否真正摘除（幂等）。"""
    with _REGISTRY_LOCK:
        if provider not in _ACTIVE_PROVIDERS:
            return False
        _ACTIVE_PROVIDERS.discard(provider)
        sid = provider.server_id
        if _LATEST_PROVIDERS.get(sid) is provider:
            _LATEST_PROVIDERS.pop(sid, None)
    return True


async def _release_active_provider(provider: "DynamicTokenProvider") -> None:
    """连接级回收：会话关闭即摘除并关闭该连接的 token HTTP 客户端。"""
    if provider is None:
        return
    if _detach_active_provider(provider):
        await _quiet_aclose(provider)


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
    ssl_verify: bool = True,
) -> str:
    """取一次 token。供 preflight 一次性使用；不传 client 则临时建（async with）。

    ``timeout_s`` 覆盖 ``TOKEN_TIMEOUT_S``（preflight 取 min(token 超时, 探活
    read_timeout)，避免 token 服务慢拖垮整个探活周期）。``ssl_verify=False``
    时临时客户端跳过证书校验（条目级 ssl_verify 配置）；传入 ``client`` 时
    由调用方自行控制。
    """
    effective_timeout = TOKEN_TIMEOUT_S if timeout_s is None else timeout_s
    if client is not None:
        return await _request_token(client, token_url, creds)
    async with httpx.AsyncClient(timeout=effective_timeout, verify=ssl_verify) as probe:
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

    防御：仅接受字符串值（数字/列表等经 str() 会带进结构化字符）；剔首尾
    空白；含控制字符（含 CR/LF）或非可打印 ASCII 的值一律拒绝——这类值
    注入请求头会在 httpx/h11 发送阶段抛 LocalProtocolError /
    UnicodeEncodeError，击穿"取不到不阻断"的降级语义。
    """
    try:
        from jiuwenswarm.common.request_ext import get_ext

        raw = get_ext().get(USER_TOKEN_EXT_KEY)
    except Exception:  # noqa: BLE001 - ext 读取失败不阻断请求
        return ""
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if not value or len(value) > _MAX_USER_TOKEN_LEN:
        return ""
    if any(not (0x20 <= ord(ch) <= 0x7E) for ch in value):
        return ""
    return value


# ---------------------------------------------------------------------------
# httpx.Auth Provider
# ---------------------------------------------------------------------------


def _jsonrpc_kind_and_id(request: httpx.Request) -> tuple[str, Any]:
    """按 POST 体形态分类出站消息：``request`` / ``notification`` / ``response``。

    - ``request``：带 ``method`` + 合法 ``id``（tools/call、initialize 等）
      → 可按 (server_id, id) 精确归属；
    - ``notification``：带 ``method`` 无 ``id``（notifications/initialized、
      cancelled 等）→ 按网关通知 FIFO 配对（非法 id 亦不入 FIFO，见
      ``unknown``）；
    - ``response``：无 ``method``（result/error，回服务器 ping 等）→ 其 id
      属服务器请求 id 空间，与客户端请求撞号，**不得**按 id 消费登记；
    - ``unknown``：非法 id、非 POST（GET 事件流 / DELETE 会话）、空体、
      非对象体 → 走 last-known，不参与精确/FIFO 归属。

    返回 ``(kind, request_id)``，非 request 时 id 为 None。
    """
    if request.method.upper() != "POST":
        return "unknown", None
    body = request.content
    if not body:
        return "unknown", None
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return "unknown", None
    if not isinstance(payload, dict):
        return "unknown", None
    if "method" not in payload:
        return "response", None
    request_id = payload.get("id")
    if request_id is None:
        return "notification", None
    if isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
        # 非法 id：与发送侧代理同口径——跳过精确/FIFO 归属，走 last-known
        return "unknown", None
    return "request", request_id


class DynamicTokenProvider(httpx.Auth):
    """每请求实时获取 token 的 ``httpx.Auth``。

    ``async_auth_flow`` 顺序：
      1. 铺静态模板 headers（对齐 ``AuthHeaderAndQueryProvider`` 语义——
         条目下发的 Authorization 原样保留）；
      2. 合并 auth_query_params；
      3. 用户授权逐轮覆盖：按发送侧登记取本轮 ``X-User-Token`` 完整值
         覆盖静态 ``Authorization``（与 X-Actor-Token 平级并列）——请求按
         (server_id, id) 精确消费；通知按 FIFO 配对；响应 / GET / 重连等
         取 last-known；本轮没带令牌 → 无值 → 保留静态快照；
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
        *,
        ssl_verify: bool = True,
    ) -> None:
        self.headers = dict(auth_headers or {})
        self.query_params = dict(auth_query_params or {})
        # 网关级 token 端点（注册表按 server_id 持有，fetch 时现读——热更命中）。
        self._token_url = str(token_url or "")
        self._server_id = str(server_id or "")
        # 条目级 ssl_verify（strategy 按 server_id 从注册表现读后传入）：
        # false 时取 token 的 httpx 客户端跳过证书校验（自签名网关）。
        self._ssl_verify = bool(ssl_verify)
        self._client: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._inflight: asyncio.Task | None = None
        # 负缓存：取 token 失败后的窗口期（秒），窗口内直接空头降级不再打
        # token 服务（防故障期打爆 + warning 刷屏）。0 = 无窗口（立即重试）。
        self._negative_until = 0.0
        # 逐轮用户令牌登记表（**按连接内聚**）：写流代理在调用者上下文登记，
        # 本方法在后台发送任务里消费。多会话各自建连时各持一个 provider，
        # 登记互不可见——同 id 撞号 / 通知错配 / GET 重连串身份从根上消除。
        #: jsonrpc_id -> 令牌 FIFO（同连接不会撞号；FIFO 容忍重试类重复消费）
        self._pending_turn_tokens: dict[Any, deque[str]] = {}
        #: 通知令牌 FIFO（含 "" 哨兵 = 发送方无令牌）
        self._pending_notif_tokens: deque[str] = deque()
        #: 最近一次非空登记值（GET / SSE 重连 / DELETE 等取它）
        self._last_turn_token: str = ""
        self._binding_lock = threading.Lock()
        #: 持有本 provider 的会话（弱引用）。provider 随连接创建，也随会话
        #: 关闭回收——避免只依赖 unmark 被动清理，长期存活进程里泄漏 token
        #: HTTP 客户端。
        self._owner_session_ref: weakref.ref | None = None

    # ------------------------------------------------------------------
    # 连接归属 / 生命周期（写流代理安装时绑定，会话关闭时回收）
    # ------------------------------------------------------------------

    def bind_session(self, session: Any) -> bool:
        """把 provider 绑定到会话；已被**其它存活会话**持有时返回 False。

        兜底安装接缝可能查到同一 server_id 的“最近 provider”，若它已属于
        另一个连接，绑定失败即跳过，绝不把别的连接的身份/生命周期挂过来。
        """
        with self._binding_lock:
            owner = (
                self._owner_session_ref()
                if self._owner_session_ref is not None
                else None
            )
            if owner is not None and owner is not session:
                return False
            # 不可弱引用对象（测试替身）无法做归属比对，按无主处理。
            self._owner_session_ref = _weak_session_ref(session)
            return True

    def release_session(self, session: Any) -> bool:
        """解除会话归属；返回本次调用是否有权回收（owner 为空/该会话/已死）。"""
        with self._binding_lock:
            owner = (
                self._owner_session_ref()
                if self._owner_session_ref is not None
                else None
            )
            if owner is not None and owner is not session:
                return False
            self._owner_session_ref = None
            return True

    # ------------------------------------------------------------------
    # 对外只读视图（G.CLS.11：模块级接缝经公开属性/方法访问，不触内部字段）
    # ------------------------------------------------------------------

    @property
    def server_id(self) -> str:
        """网关 server_id（unmark / 回收等模块级接缝使用）。"""
        return self._server_id

    # ------------------------------------------------------------------
    # 逐轮用户令牌登记表（按连接内聚；写流代理发送侧调用，本类出网消费）
    # ------------------------------------------------------------------

    def record_request_token(self, request_id: Any, authorization: str) -> None:
        """发送侧登记：把当前调用者的用户令牌绑到本连接的 JSON-RPC id 上。"""
        with self._binding_lock:
            queue = self._pending_turn_tokens.setdefault(request_id, deque())
            queue.append(authorization)
            # 上限作用于不同 id 的数量（连接死亡导致永不消费的登记不无限累积）
            while len(self._pending_turn_tokens) > _MAX_PENDING_TURN_TOKENS:
                self._pending_turn_tokens.pop(next(iter(self._pending_turn_tokens)))
            if authorization:
                self._last_turn_token = authorization

    def _consume_request_token(self, request_id: Any) -> str:
        """出网消费：返回并移除该 id 的登记（无 → 空串，调用方回退静态头）。"""
        with self._binding_lock:
            queue = self._pending_turn_tokens.get(request_id)
            if not queue:
                return ""
            value = queue.popleft()
            if not queue:
                self._pending_turn_tokens.pop(request_id, None)
            return value

    def push_notification_token(self, authorization: str) -> None:
        """发送侧入队：通知与其 POST 按 FIFO 配对；无令牌入 "" 哨兵占位。"""
        with self._binding_lock:
            self._pending_notif_tokens.append(authorization)
            while len(self._pending_notif_tokens) > _MAX_PENDING_NOTIF_TOKENS:
                self._pending_notif_tokens.popleft()

    def _pop_notification_token(self) -> str:
        """出网出队：该通知 POST 对应发送侧登记的令牌（"" = 无令牌）。"""
        with self._binding_lock:
            if not self._pending_notif_tokens:
                return ""
            return self._pending_notif_tokens.popleft()

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        if self.headers:
            for key, value in self.headers.items():
                request.headers[key] = value
        if self.query_params:
            request.url = request.url.copy_merge_params(self.query_params)
        # 用户授权（逐轮 X-User-Token，与 X-Actor-Token 平级）：本方法在
        # 建连时启动的后台任务里执行——在那里读请求级 ContextVar 拿到的是
        # connect 时刻快照（旧值），全局 holder 也无法表达"这个请求是谁
        # 发的"。取值按发送侧写流代理（_TurnTokenTaggingStream）登记的
        # 凭据绑定：请求按 (server_id, id) 精确消费；通知按 FIFO 配对
        # （无令牌通知以 "" 哨兵占位，不误配他人）；响应 / GET 事件流 /
        # SSE 重连 / DELETE 等无法按消息归属的取 last-known。都没有 →
        # 保留条目静态快照头，不阻断请求。
        kind, request_id = _jsonrpc_kind_and_id(request)
        if kind == "request":
            user_authorization = self._consume_request_token(request_id)
        elif kind == "notification":
            user_authorization = self._pop_notification_token()
        else:  # response / unknown（GET / DELETE / 无法解析）
            user_authorization = self._last_turn_token
            if not user_authorization:
                # 尚无任何已登记令牌（典型：SSE 建连的 GET /sse 先于首次
                # send）。连接期覆盖由 connect/reconnect 的调用者捕获——
                # 尤其 SSE owner task 冻结上下文时，只有这里能拿到当轮值。
                override = _CONNECT_USER_TOKEN.get()
                if override is not _UNSET:
                    user_authorization = str(override or "")
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
            self._client = httpx.AsyncClient(timeout=TOKEN_TIMEOUT_S, verify=self._ssl_verify)
            self._loop = loop
            logger.debug(
                "[mcp-dynamic-token] token client created: server_id=%s verify=%s",
                self._server_id,
                self._ssl_verify,
            )
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
            _PENDING_TAG_SERVER.set(None)
            return await original().authenticate(auth_config, **kwargs)
        # 每次连接（含自愈重连）新建 provider：登记表按连接内聚——同一网关
        # 被多个会话各自建连时，各连接的请求 id 空间 / 通知 FIFO / last-known
        # 互不可见（并发 session 交替不串身份）。旧 provider 随其连接消亡，
        # unmark 时统一逐出关闭。
        # 条目级 ssl_verify：注册时随 mark 登记在 server_id 注册表，这里按
        # tool_id 现读，取 token 的客户端据此决定是否跳过证书校验。
        ssl_verify = _registry_ssl_verify(tool_id)
        logger.debug(
            "[mcp-dynamic-token] provider created: server_id=%s ssl_verify=%s",
            tool_id,
            ssl_verify,
        )
        provider = DynamicTokenProvider(
            auth_config.config.get("auth_headers"),
            auth_config.config.get("auth_query_params"),
            token_url,
            tool_id,
            ssl_verify=ssl_verify,
        )
        with _REGISTRY_LOCK:
            _ACTIVE_PROVIDERS.add(provider)
            _LATEST_PROVIDERS[tool_id] = provider
        # 归属标记：connect 流程内 authenticate 与 ClientSession 构造同
        # 任务，构造点消费标记（直持本连接的 provider）装上登记代理
        # （initialize 握手与通知即带当轮用户令牌，见 _TurnTokenTaggingStream）。
        _PENDING_TAG_SERVER.set(provider)
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
    # 逐轮用户令牌：会话写流登记代理（ClientSession.__init__ 主接缝 +
    # connect 兜底，见 _TurnTokenTaggingStream）；幂等，重复 apply 无副作用。
    _patch_sdk_session_tagging()
    _PATCHED = True
    return True


def _reset_dynamic_token_state_for_tests() -> None:
    """测试隔离：清空补丁态与注册表（不触碰 SDK registry 的最终槽位归属）。"""
    global _PATCHED, _ORIGINAL_STRATEGY, _DISPATCHED_CREDENTIALS
    with _REGISTRY_LOCK:
        providers = list(_ACTIVE_PROVIDERS)
        _ACTIVE_PROVIDERS.clear()
        _LATEST_PROVIDERS.clear()
        _DYNAMIC_SERVERS.clear()
    with _DISPATCHED_LOCK:
        _DISPATCHED_CREDENTIALS = None
    _PATCHED = False
    _ORIGINAL_STRATEGY = None
    DynamicTokenAuthStrategy.auth_type = None
    for provider in providers:
        _spawn_quiet_aclose(provider)
