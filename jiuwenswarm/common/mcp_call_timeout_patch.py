# coding: utf-8
"""Inject a per-call timeout + dead-session recovery into openjiuwen MCP HTTP clients.

Why this exists: ``MCPTool.invoke`` calls ``call_tool`` without a ``timeout``,
and the stock ``StreamableHttpClient.call_tool`` / ``list_tools`` await
``self._session.call_tool(...)`` / ``self._session.list_tools()`` with no
deadline. When a remote tool is slow or the server dies mid-session, the call
hangs on the MCP SDK's ``sse_read_timeout`` (default 300s).

Additionally, ToolMgr keeps one long-lived MCP client per server for the whole
AgentServer process. Chat sessions A/B share that client. So after a tool
timeout in chat A, a brand-new chat B still uses the same half-dead client and
fails with empty ``execute invoke failed, error=''`` (field sds-037840350c2c)
until we invalidate + reconnect. When the remote Streamable HTTP service
restarts, the protocol session id is stale and calls fail with
``Session terminated`` / HTTP 404 — again including brand-new chat sessions —
until AS rebuilds the MCP connection.

SSE / Streamable HTTP timeout teardown often raises ``asyncio.CancelledError``
(cancel-scope cross-task noise) *after* the real timeout. ``CancelledError`` is
not an ``Exception`` subclass, so ``MCPTool.invoke`` cannot turn it into a tool
error — the agent loop treats it as “user cancel”, skips ``chat.tool_result``,
and replies “工具调用被用户取消”. This patch converts non-outer
``CancelledError`` into ``TimeoutError`` / transport ``RuntimeError``.

This patch is applied once at process startup (from
``JiuWenSwarmDeepAdapter.__init__``):

  A. Wrap ``call_tool`` / ``list_tools`` on the HTTP transports with
     ``asyncio.wait_for``. On timeout we tear down the dead session and
     **force-invalidate** local state even if ``disconnect()`` raises
     (e.g. ``Attempted to exit cancel scope in a different task`` or
     ``CancelledError``). The next call auto-reconnects on a fresh
     ``AsyncExitStack`` so the same chat session can keep using other tools
     (TC_MCP_CALL_014).

     On retryable dead-session errors (``Session terminated``, connection
     closed, cancel-scope teardown noise, …) we invalidate, reconnect once,
     and retry the same call so both the original chat and a newly created
     chat recover after MCP service restart (without requiring AS restart).

     同 MCP client 的调用经 ``_client_io_lock`` **串行**是刻意取舍（多会话
     共享进程级客户端时避免并发踩坏半死连接）；重试语义是
     **at-least-once**（超时/死会话后可能已在对端执行过一次）。

     NB: remote（sse / streamable-http）**必须**用 ``asyncio.wait_for``，
     **不能**用 ``anyio.fail_after``。transport 自带后台 task + anyio
     cancel scope；外层再套 ``fail_after`` 会在跨 task 退出时抛
     ``Attempted to exit a cancel scope...``。现场表现：超时后重连成功、
     MCP 侧 ``call completed``，但第 3 轮结果仍被该异常盖成失败
     （TC_MCP_CALL_014）。与 ``mcp_config._run_mcp_worker`` 对 remote
     的超时策略一致。

     同时把解析后的超时以 ``timeout + 5`` 写入 client 方法的 ``timeout``
     kwarg（内层天花板），外层 ``wait_for`` 仍用精确超时，避免 SseClient
     默认 60s 提前掐断，并保证 pooled worker 的外层截止先触发。
  B. Monkeypatch ``ToolMgr._create_client`` so ``config.params["timeout_s"]``
     (i.e. ``/mcp add ... --timeout_s N``) is stamped onto the client instance
     as ``_jws_call_timeout`` and honored by (A). Falls back to
     ``DEFAULT_CALL_TIMEOUT`` when unset.
  C. Wrap the MCP SDK factory functions (``mcp.client.sse.sse_client`` /
     ``mcp.client.streamable_http.streamablehttp_client``) so connections
     created for a client with a stamped ``_jws_call_timeout`` get an
     ``sse_read_timeout`` of at least that value, instead of the SDK's 300s
     default — long tool calls (SSE result stream / held streamable-http
     response) would otherwise be dropped mid-call after 300 idle seconds.
  D. AbilityManager 外层 ``anyio.fail_after`` → 无 cancel scope 的截止时间
     + ``tool.invoke`` 用 ``asyncio.wait_for``（不改 agent-core 源码，等价于
     把 AM 超时从 fail_after 换成 wait_for，修复 TC_MCP_CALL_014 第 3 轮）。
  E. ``disconnect`` / ``_do_disconnect`` 在 finally 中强制清会话，避免 aclose
     失败留下半死连接。

Both transforms are idempotent: a module-level ``_PATCHED`` guard makes the
whole function a no-op on repeat calls.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
from contextlib import AsyncExitStack
from typing import Any, Optional

from openjiuwen.core.common.logging import logger

_PATCHED = False
# (cls, name) pairs already wrapped — idempotency guard independent of
# _PATCHED, so we don't stamp attributes onto function objects (which would
# need a mypy ``[attr-defined]`` type-ignore).
_wrapped_methods: set[tuple[type, str]] = set()
# (module, name) SDK factory functions already wrapped, and (cls, name)
# client connect methods already wrapped — same idempotency purpose.
_wrapped_factories: set[tuple[object, str]] = set()
_wrapped_connects: set[tuple[type, str]] = set()
#: Set while a client with a stamped ``_jws_call_timeout`` is establishing its
#: transport, so the SDK factory wrappers can lift ``sse_read_timeout`` to the
#: per-connector timeout (see ``_patch_sdk_read_timeouts``).
_pending_sdk_read_timeout: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "_jws_pending_sdk_read_timeout", default=None
)
#: Fallback per-call timeout for process-level ToolMgr MCP clients when
#: ``--timeout_s`` is not supplied. Request-scoped MCP workers use their
#: separate 300s default in ``mcp_config._MCP_CALL_TOOL_TIMEOUT_S`` because
#: connector discovery/startup may legitimately take longer than this
#: process-level fail-fast fallback.
DEFAULT_CALL_TIMEOUT = 30.0

# AbilityManager 外层曾用 anyio.fail_after 包 tool.invoke；MCP 重连若落在该
# cancel scope 内，退出时会抛 cancel scope（TC_MCP_CALL_014 第 3 轮）。
# 补丁把 fail_after 换成「无 cancel scope + contextvar 截止时间」，再由
# tool.invoke 包装层用 asyncio.wait_for 真正限时——效果等同改 agent-core。
_am_call_timeout: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar(
    "jws_ability_manager_call_timeout", default=None
)

# MCP 对端重启 / 半死连接时 SDK 常见报错（对齐 browser-move 补丁口径）。
_RETRYABLE_DEAD_SESSION_MARKERS: tuple[str, ...] = (
    "session terminated",
    "closedresourceerror",
    "brokenresourceerror",
    "endofstream",
    "stream closed",
    "connection closed",
    "remoteprotocolerror",
    "readerror",
    "writeerror",
    "not connected",
    "broken pipe",
    "server error 404",
    "404 not found",
    # anyio cancel-scope 跨 task 清理噪声（超时/断连后偶发，应重连而非当业务失败）
    "cancel scope",
    "attempted to exit a cancel scope",
)

__all__ = [
    "DEFAULT_CALL_TIMEOUT",
    "apply_mcp_call_timeout_patch",
    "force_invalidate_mcp_client",
    "is_retryable_mcp_dead_session_error",
]


def _fire_and_forget_aclose_exit_stack(stack: Any) -> None:
    """后台 best-effort ``aclose`` 旧 AsyncExitStack，避免泄漏；不阻塞调用方。"""
    if not isinstance(stack, AsyncExitStack):
        return

    async def _aclose() -> None:
        try:
            await stack.aclose()
        except BaseException as exc:
            # 含 CancelledError：后台清理失败不得污染当前调用方
            logger.debug(
                "[mcp-timeout] background aclose of old AsyncExitStack failed: %r",
                exc,
            )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug(
            "[mcp-timeout] no running loop; skip background aclose of old AsyncExitStack"
        )
        return

    task = loop.create_task(_aclose())

    def _drain_task_result(done: asyncio.Task) -> None:
        try:
            done.result()
        except BaseException:
            pass

    task.add_done_callback(_drain_task_result)


def force_invalidate_mcp_client(client: Any) -> None:
    """丢弃半死会话状态，换上新的 AsyncExitStack，供后续 ``connect()`` 重连。

    SDK ``StreamableHttpClient.disconnect`` 在 aclose 失败时往往不清 ``_session``，
    且成功 aclose 后旧 stack 也不能再 enter。超时 / Session terminated 路径必须
    无条件调用本函数。

    替换 ``_exit_stack`` 前会取出旧 stack，用 running loop 的
    ``create_task`` fire-and-forget 去 ``aclose()``（后台 best-effort；异常仅
    debug 记录。无 running loop 时 debug 后放弃 aclose）。本函数保持同步，
    不改成 async。

    同时打上 ``_jws_needs_reconnect``：即便残留对象仍像「已连接」，下次调用也强制
    重连。聊天会话 A/B 共用同一客户端，否则新建会话也会继承半死连接。
    """
    for attr in ("_session", "_client", "_read", "_write"):
        if hasattr(client, attr):
            setattr(client, attr, None)
    if hasattr(client, "_is_disconnected"):
        setattr(client, "_is_disconnected", True)
    old_stack = None
    if hasattr(client, "_exit_stack"):
        old_stack = getattr(client, "_exit_stack", None)
        setattr(client, "_exit_stack", AsyncExitStack())
    if hasattr(client, "_auth_provider"):
        setattr(client, "_auth_provider", None)
    try:
        setattr(client, "_jws_needs_reconnect", True)
    except Exception as exc:
        # __slots__/代理对象可能拒绝动态属性；标记失败不阻断作废流程
        logger.debug(
            "[mcp-timeout] failed to set _jws_needs_reconnect on %s: %r",
            type(client).__name__,
            exc,
        )
    if old_stack is not None:
        _fire_and_forget_aclose_exit_stack(old_stack)


def is_retryable_mcp_dead_session_error(error: BaseException) -> bool:
    """对端重启或传输层半死后，值得作废本地会话并重连重试一次的错误。"""
    name = error.__class__.__name__.lower()
    text = str(error).lower().strip()
    # 现场半死 streamable-http 常抛空消息；仅对传输类空异常重试，避开业务 ValueError。
    if not text and name in (
        "exception",
        "runtimeerror",
        "oserror",
        "connectionerror",
        "mcperror",
        "error",
    ):
        return True
    return any(marker in name or marker in text for marker in _RETRYABLE_DEAD_SESSION_MARKERS)


def _is_outer_cancellation() -> bool:
    """True only for real interrupt / WS-drop cancels (not transport noise).

    不 import ``mcp_config``：该模块副作用重，超时热路径只需要 ``Task.cancelling()``。
    """
    current = asyncio.current_task()
    return bool(current is not None and current.cancelling())


def _client_io_lock(client: Any) -> asyncio.Lock:
    """进程级共享 MCP client 的重连/调用串行锁。

    串行是刻意取舍：多聊天会话共用同一客户端时，避免并发重连/调用踩坏半死连接。
    超时与死会话路径的重试是 at-least-once（对端可能已执行过一次）。
    """
    lock = getattr(client, "_jws_io_lock", None)
    if isinstance(lock, asyncio.Lock):
        return lock
    lock = asyncio.Lock()
    try:
        setattr(client, "_jws_io_lock", lock)
    except Exception as exc:
        # 挂不上锁时仍返回本次新建的 lock，仅本调用串行；下次再试 setattr
        logger.debug(
            "[mcp-timeout] failed to attach _jws_io_lock on %s: %r",
            type(client).__name__,
            exc,
        )
    return lock


async def _abandon_session(client: Any, *, context: str) -> None:
    """超时后直接作废本地会话，不在当前 task 上调用 disconnect()/aclose。

    wait_for 超时在子 task 取消调用；原 AsyncExitStack 里的 anyio cancel scope
    若在父 task 上 aclose，会抛 cancel scope 并污染当前 task 的 scope 栈，
    导致下一轮重连后的成功结果在 AbilityManager 外层退出时仍被盖掉
    （TC_MCP_CALL_014 第 3 轮）。

    ``force_invalidate_mcp_client`` 会把旧 stack 丢到后台 task best-effort
    ``aclose()``，本协程本身不 await 清理。
    """
    force_invalidate_mcp_client(client)
    logger.warning(
        "[mcp-timeout] abandoned MCP session without aclose (%s): %s",
        context,
        getattr(client, "_server_path", "?"),
    )


def _wrap_disconnect_always_invalidate(cls: type, method_name: str) -> None:
    """disconnect / _do_disconnect：aclose 失败也清掉半死会话（对齐 agent-core 修复）。"""
    if (cls, method_name) in _wrapped_methods:
        return
    if not hasattr(cls, method_name):
        return
    _wrapped_methods.add((cls, method_name))
    orig = getattr(cls, method_name)

    async def wrapped(self, *args, **kwargs):
        try:
            return await orig(self, *args, **kwargs)
        finally:
            force_invalidate_mcp_client(self)

    setattr(cls, method_name, wrapped)


def _wrap_invoke_with_am_timeout(cls: type, method_name: str = "invoke") -> None:
    """AbilityManager 路径下用 wait_for 限时，避免外层 fail_after 的 cancel scope。

    用 ``_jws_orig_<method>`` 记住原始方法；测试反复 ``_PATCHED=False; clear;
    apply`` 时先还原再包一层，避免多层 ``wait_for`` 嵌套。

    必须只看 ``cls.__dict__``：``hasattr``/``getattr`` 会沿 MRO 找到父类已
    stash 的 ``_jws_orig_invoke``，把子类自己的 ``invoke`` 覆盖成父类空实现
    （返回 None）——CI 里 LocalFunction / RequestScoped 全系挂掉的根因。
    """
    if method_name not in cls.__dict__:
        return
    orig_attr = f"_jws_orig_{method_name}"
    # 再次包装前先还原，避免嵌套（仅本类自己 stash 的才算）
    existing_orig = cls.__dict__.get(orig_attr)
    if existing_orig is not None:
        orig = existing_orig
    else:
        orig = cls.__dict__[method_name]
        try:
            setattr(cls, orig_attr, orig)
        except Exception as exc:
            logger.debug(
                "[mcp-timeout] failed to stash %s on %s: %r",
                orig_attr,
                cls.__name__,
                exc,
            )

    _wrapped_methods.add((cls, f"am_timeout:{method_name}"))

    async def wrapped(self, *args, **kwargs):
        timeout = _am_call_timeout.get()
        if timeout is None:
            return await orig(self, *args, **kwargs)
        return await asyncio.wait_for(orig(self, *args, **kwargs), timeout=timeout)

    setattr(cls, method_name, wrapped)


def _install_tool_init_subclass_am_timeout_hook(tool_cls: type) -> None:
    """Tool 子类若在自身 ``__dict__`` 定义了 ``invoke``，自动套 AM wait_for 包装。

    必须赋 ``classmethod``：类体里写的 ``__init_subclass__`` 会被 type 自动包成
    classmethod，但事后 ``Tool.__init_subclass__ = 普通函数`` 不会。未包成
    classmethod 时，创建子类会因缺少 ``cls`` 直接 TypeError（!6630 回退根因之一）。
    """
    if getattr(tool_cls, "_jws_am_timeout_init_subclass_hooked", False):
        return
    prev = tool_cls.__dict__.get("__init_subclass__")

    @classmethod
    def __init_subclass__(cls, **kwargs):  # noqa: N807 — 匹配 Python 钩子名
        if prev is not None:
            # ``__dict__`` 里可能是 classmethod 描述符，也可能是裸函数
            prev_fn = prev.__func__ if isinstance(prev, classmethod) else prev
            prev_fn(cls, **kwargs)
        else:
            super(tool_cls, cls).__init_subclass__(**kwargs)
        if "invoke" in cls.__dict__:
            _wrap_invoke_with_am_timeout(cls)

    try:
        tool_cls.__init_subclass__ = __init_subclass__  # type: ignore[method-assign]
        setattr(tool_cls, "_jws_am_timeout_init_subclass_hooked", True)
    except Exception as exc:
        logger.warning(
            "[mcp-timeout] Tool.__init_subclass__ AM timeout hook skipped: %r",
            exc,
        )


def _patch_ability_manager_fail_after() -> None:
    """只替换 AbilityManager 模块命名空间里的 ``anyio``，不改全局 ``anyio.fail_after``。

    注意：``am_mod.anyio.fail_after = ...`` 会改到共享的 anyio 模块属性，
    ProgressiveToolRail 等其它 ``import anyio`` 也会中招（丢 cancel_called）。
    正确做法：把 ``am_mod.anyio`` 换成代理对象，仅 AbilityManager 内
    ``anyio.fail_after`` 走无 cancel-scope 桥接。
    """
    import anyio as real_anyio
    import openjiuwen.core.single_agent.ability_manager as am_mod

    @contextlib.contextmanager
    def _fail_after_without_cancel_scope(delay, *args, **kwargs):
        del args, kwargs
        # anyio.fail_after(None) 原语义：不设截止
        if delay is None:
            yield
            return
        try:
            timeout = float(delay)
        except (TypeError, ValueError):
            yield
            return
        if timeout <= 0:
            yield
            return
        token = _am_call_timeout.set(timeout)
        try:
            yield
        finally:
            _am_call_timeout.reset(token)

    class _AbilityManagerAnyioProxy:
        """AbilityManager 专用 anyio 视图：fail_after 桥接，其余原样转发。"""

        @staticmethod
        def fail_after(*args, **kwargs):
            return _fail_after_without_cancel_scope(*args, **kwargs)

        @staticmethod
        def __getattr__(name: str) -> Any:
            return getattr(real_anyio, name)

    # 只改 ability_manager.anyio 绑定，绝不写 real_anyio.fail_after
    am_mod.anyio = _AbilityManagerAnyioProxy()  # type: ignore[assignment]

    # invoke 包装：在 AM 截止时间内用 wait_for（覆盖 MCP / 普通 Tool / 请求级工具）
    from openjiuwen.core.foundation.tool.base import Tool
    from openjiuwen.core.foundation.tool.mcp.base import MCPTool

    _wrap_invoke_with_am_timeout(Tool)
    _wrap_invoke_with_am_timeout(MCPTool)
    try:
        from openjiuwen.core.foundation.tool.function.function import LocalFunction

        _wrap_invoke_with_am_timeout(LocalFunction)
    except Exception as exc:  # noqa: BLE001 — 可选依赖，缺了就跳过
        logger.warning("[mcp-timeout] LocalFunction invoke wrap skipped: %r", exc)
    try:
        from openjiuwen.core.foundation.tool.service_api.restful_api import RestfulApi

        _wrap_invoke_with_am_timeout(RestfulApi)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[mcp-timeout] RestfulApi invoke wrap skipped: %r", exc)
    try:
        from jiuwenswarm.common.mcp_config import RequestScopedOfficeClawMcpTool

        _wrap_invoke_with_am_timeout(RequestScopedOfficeClawMcpTool)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[mcp-timeout] RequestScoped invoke wrap skipped: %r", exc)

    # 后续新 Tool 子类若自带 invoke，自动包装，避免再漏
    _install_tool_init_subclass_am_timeout_hook(Tool)

    logger.info(
        "[mcp-timeout] AbilityManager fail_after → wait_for bridge applied "
        "(module-local anyio proxy; global anyio untouched)"
    )


def _stamp_is_valid(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _patch_sdk_read_timeouts() -> None:
    """Lift the MCP SDK transports' ``sse_read_timeout`` to the connector timeout.

    The SDK builds one httpx client per connection with
    ``httpx.Timeout(timeout, read=sse_read_timeout)`` where ``sse_read_timeout``
    defaults to 300s. A long tool call exceeds it silently: on SSE the result
    is delivered on the event stream, on streamable-http the POST response is
    held until the tool finishes — either way an idle read of more than 300s
    drops the connection mid-call. openjiuwen's clients don't expose the
    parameter, so wrap the module-level factory functions and raise
    ``sse_read_timeout`` to the client's stamped ``_jws_call_timeout`` when one
    is present. The stamp is surfaced to the factories through a ContextVar set
    by wrapping the clients' connect paths (SseClient builds its transport
    inside ``_do_connect`` on the owner task; StreamableHttpClient inside
    ``connect``), which run in the same task that invokes the factory.
    """
    import mcp.client.sse as mcp_sse_module
    import mcp.client.streamable_http as mcp_streamable_http_module
    from openjiuwen.core.foundation.tool.mcp.client.sse_client import SseClient
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )

    for module, factory_name in (
        (mcp_sse_module, "sse_client"),
        (mcp_streamable_http_module, "streamablehttp_client"),
    ):
        if (module, factory_name) in _wrapped_factories:
            continue
        _wrapped_factories.add((module, factory_name))
        orig_factory = getattr(module, factory_name)

        def factory_with_read_timeout(*args: Any, orig=orig_factory, name=factory_name, **kwargs: Any):
            hint = _pending_sdk_read_timeout.get()
            if _stamp_is_valid(hint):
                current = kwargs.get("sse_read_timeout")
                if not _stamp_is_valid(current) or float(current) < float(hint):
                    kwargs["sse_read_timeout"] = float(hint)
                    logger.info(
                        "[mcp-timeout] raised %s sse_read_timeout to %.1fs for this connection",
                        name,
                        float(hint),
                    )
            return orig(*args, **kwargs)

        setattr(module, factory_name, factory_with_read_timeout)

    connect_targets: list[tuple[type, str]] = [
        (StreamableHttpClient, "connect"),
    ]
    # 真实 SseClient 在 owner-task 的 _do_connect 里建 transport；单测假类可能只有 connect。
    if hasattr(SseClient, "_do_connect"):
        connect_targets.append((SseClient, "_do_connect"))
    elif hasattr(SseClient, "connect"):
        connect_targets.append((SseClient, "connect"))
    else:
        logger.debug("[mcp-timeout] SseClient has neither _do_connect nor connect; skip stamp")

    for cls, method_name in connect_targets:
        if (cls, method_name) in _wrapped_connects:
            continue
        if not hasattr(cls, method_name):
            logger.debug(
                "[mcp-timeout] skip wrapping %s.%s (attribute missing)",
                getattr(cls, "__name__", cls),
                method_name,
            )
            continue
        _wrapped_connects.add((cls, method_name))
        orig_method = getattr(cls, method_name)

        async def method_with_read_timeout(self, *args: Any, orig=orig_method, **kwargs: Any):
            hint = getattr(self, "_jws_call_timeout", None)
            token = (
                _pending_sdk_read_timeout.set(float(hint)) if _stamp_is_valid(hint) else None
            )
            try:
                return await orig(self, *args, **kwargs)
            finally:
                if token is not None:
                    _pending_sdk_read_timeout.reset(token)

        setattr(cls, method_name, method_with_read_timeout)


def _patch_sse_disconnect_guard() -> None:
    """Keep ``SseClient.disconnect`` best-effort cleanup.

    openjiuwen's ``_do_disconnect`` wraps the session/transport ``__aexit__``
    calls in ``asyncio.wait_for``, which executes them in a new asyncio Task.
    The contexts were entered on the SSE owner task, so exiting them from
    wait_for's task trips anyio's cancel-scope invariant; the broken teardown
    then surfaces as a ``CancelledError`` raised from ``_submit``'s future —
    a BaseException that sails past the ``except Exception`` handlers around
    discovery cleanup (``_list_remote_mcp_connector_tools``) and the pooled
    worker's exit-stack callback, aborting SSE connector registration outright.
    Disconnect is cleanup: swallow the future-cancel flavor, log it, and
    report failure. A genuine cancellation of the running task (``cancelling``
    count > 0) still propagates.
    """
    import asyncio as _asyncio

    from openjiuwen.core.foundation.tool.mcp.client.sse_client import SseClient

    if (SseClient, "disconnect-guard") in _wrapped_connects:
        return
    _wrapped_connects.add((SseClient, "disconnect-guard"))
    orig_disconnect = getattr(SseClient, "disconnect")

    async def disconnect_guarded(self, *args: Any, orig=orig_disconnect, **kwargs: Any):
        try:
            return await orig(self, *args, **kwargs)
        except _asyncio.CancelledError:
            task = _asyncio.current_task()
            cancelling = getattr(task, "cancelling", None)
            if callable(cancelling) and cancelling() > 0:
                raise
            logger.warning(
                "[mcp-timeout] SseClient.disconnect aborted by transport teardown "
                "(pre-existing anyio cancel-scope conflict); treated as failed cleanup",
            )
            return False

    setattr(SseClient, "disconnect", disconnect_guarded)


def apply_mcp_call_timeout_patch(default_timeout: float = DEFAULT_CALL_TIMEOUT) -> None:
    """Apply the per-call MCP timeout patch. Idempotent per process."""
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )
    from openjiuwen.core.foundation.tool.mcp.client.sse_client import SseClient
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr

    def _resolve_timeout(client: Any, explicit: Any) -> float:
        # Explicit kwarg wins (currently never passed by invoke, but keeps the
        # signature honest); then the per-instance value stamped by (B); then
        # the module default.
        if isinstance(explicit, (int, float)) and explicit > 0:
            return float(explicit)
        stamped = getattr(client, "_jws_call_timeout", None)
        if isinstance(stamped, (int, float)) and stamped > 0:
            return float(stamped)
        return default_timeout

    async def _connect_fresh(client: Any, *, reason: str) -> None:
        """作废旧协议会话后重新 connect（进程级客户端，跨聊天会话共享）。"""
        # 超时后勿 aclose：跨 task cancel scope 会污染当前 task（见 _abandon_session）。
        await _abandon_session(client, context=f"before reconnect ({reason})")
        connect = getattr(client, "connect", None)
        if not callable(connect):
            raise RuntimeError("MCP client has no connect() for session recovery")
        ok = await connect()
        if not ok:
            raise RuntimeError(
                f"MCP client reconnect failed ({reason}): "
                f"{getattr(client, '_server_path', '?')}"
            )
        try:
            setattr(client, "_jws_needs_reconnect", False)
        except Exception as exc:
            logger.debug(
                "[mcp-timeout] failed to clear _jws_needs_reconnect on %s after "
                "reconnect (%s): %r",
                type(client).__name__,
                reason,
                exc,
            )
        logger.warning(
            "[mcp-timeout] MCP client reconnected after %s: %s",
            reason,
            getattr(client, "_server_path", "?"),
        )

    async def _ensure_connected(client: Any) -> None:
        """上一轮超时 / 作废后：强制重连（新旧聊天会话共用同一客户端）。"""
        needs_reconnect = bool(getattr(client, "_jws_needs_reconnect", False))
        session = getattr(client, "_session", None)
        disconnected = bool(getattr(client, "_is_disconnected", False))
        if not needs_reconnect and session is not None and not disconnected:
            return
        await _connect_fresh(
            client,
            reason="forced-reconnect" if needs_reconnect else "missing-session",
        )

    async def _teardown_after_timeout(
        client: Any, *, cls_name: str, method: str, timeout: float
    ) -> None:
        # 跳过 disconnect/aclose，直接作废（TC_MCP_CALL_014）。
        await _abandon_session(
            client,
            context=f"{cls_name}.{method} after {timeout:g}s timeout",
        )
        logger.warning(
            "[mcp-timeout] %s.%s session invalidated after %gs timeout "
            "(next call will reconnect): %s",
            cls_name,
            method,
            timeout,
            getattr(client, "_server_path", "?"),
        )

    def _timeout_error(cls_name: str, method: str, timeout: float) -> TimeoutError:
        # 空 TimeoutError 会导致 SSE error=''；给出可识别文案。
        return TimeoutError(
            f"MCP {cls_name}.{method} timed out after {timeout:g}s"
        )

    def _transport_cancel_error(cls_name: str, method: str) -> RuntimeError:
        return RuntimeError(
            f"MCP {cls_name}.{method} interrupted by transport/timeout "
            "(not user cancel)"
        )

    def _wrap_with_timeout(cls: type, name: str) -> None:
        if (cls, name) in _wrapped_methods:
            return
        _wrapped_methods.add((cls, name))
        orig = getattr(cls, name)

        async def wrapped(self, *args, **kwargs):
            timeout = _resolve_timeout(self, kwargs.get("timeout"))
            # 内层（如 SseClient 自带 wait_for）用 timeout+5 作天花板，避免仍按
            # 默认 60s 提前掐断；外层 wait_for 用精确 timeout，与 pooled worker
            # 的外层截止对齐，保证先触发外层 TimeoutError。
            kwargs.setdefault("timeout", timeout + 5.0)

            async def _invoke_once():
                # remote HTTP transport：用 asyncio.wait_for，不用 anyio.fail_after。
                # fail_after 会在本 task 套 cancel scope，与 streamable-http/SSE
                # transport 后台 task 的 scope 冲突，表现为超时后重连成功、MCP
                # 已 call completed，但返回时仍抛 Attempted to exit a cancel scope
                # （TC_MCP_CALL_014 第 3 轮）。与 mcp_config remote worker 一致。
                return await asyncio.wait_for(
                    orig(self, *args, **kwargs),
                    timeout=timeout,
                )

            async def _handle_timeout() -> None:
                logger.warning(
                    "[mcp-timeout] %s.%s timed out after %gs, disconnecting client: %s",
                    cls.__name__,
                    name,
                    timeout,
                    getattr(self, "_server_path", "?"),
                )
                await _teardown_after_timeout(
                    self, cls_name=cls.__name__, method=name, timeout=timeout
                )

            async def _handle_spurious_cancel(*, phase: str) -> None:
                """Transport / cancel-scope CancelledError → 可捕获的工具错误。"""
                logger.warning(
                    "[mcp-timeout] %s.%s got CancelledError without outer cancel "
                    "(%s); treating as transport/timeout error: %s",
                    cls.__name__,
                    name,
                    phase,
                    getattr(self, "_server_path", "?"),
                )
                await _teardown_after_timeout(
                    self, cls_name=cls.__name__, method=name, timeout=timeout
                )

            async def _reconnect_and_invoke_once(*, reason: str):
                """作废重连后立刻再 invoke 一次（spurious-cancel / dead-session 共用）。"""
                await _connect_fresh(self, reason=reason)
                return await _invoke_once()

            # 进程级共享 client：串行化重连与调用，避免多聊天会话并发踩坏会话。
            async with _client_io_lock(self):
                # 同会话续聊：上一轮超时后自动恢复连接，避免永久 execute invoke failed。
                await _ensure_connected(self)
                try:
                    return await _invoke_once()
                except TimeoutError:
                    try:
                        await _handle_timeout()
                    except asyncio.CancelledError:
                        # teardown 内偶发 CancelledError 不应冒泡成「用户取消」
                        if _is_outer_cancellation():
                            raise
                        force_invalidate_mcp_client(self)
                        logger.warning(
                            "[mcp-timeout] %s.%s teardown raised CancelledError "
                            "without outer cancel; still reporting timeout: %s",
                            cls.__name__,
                            name,
                            getattr(self, "_server_path", "?"),
                        )
                    raise _timeout_error(cls.__name__, name, timeout) from None
                except asyncio.CancelledError:
                    # wait_for / SSE disconnect 可能直接冒出 CancelledError；
                    # MCPTool.invoke 捕不到 → 误判用户取消且无 tool_result。
                    if _is_outer_cancellation():
                        raise
                    logger.warning(
                        "[mcp-timeout] %s.%s got CancelledError without outer cancel "
                        "(invoke); reconnecting once: %s",
                        cls.__name__,
                        name,
                        getattr(self, "_server_path", "?"),
                    )
                    # 连接中断类：作废后重连并重试一次（对齐 SSE mock 重启场景）
                    try:
                        return await _reconnect_and_invoke_once(reason="spurious-cancel")
                    except TimeoutError:
                        try:
                            await _handle_timeout()
                        except asyncio.CancelledError:
                            if _is_outer_cancellation():
                                raise
                            force_invalidate_mcp_client(self)
                        raise _timeout_error(cls.__name__, name, timeout) from None
                    except asyncio.CancelledError:
                        if _is_outer_cancellation():
                            raise
                        force_invalidate_mcp_client(self)
                        raise _transport_cancel_error(cls.__name__, name) from None
                    except Exception as retry_exc:
                        force_invalidate_mcp_client(self)
                        logger.warning(
                            "[mcp-timeout] %s.%s retry after spurious cancel failed: %r",
                            cls.__name__,
                            name,
                            retry_exc,
                        )
                        raise _transport_cancel_error(cls.__name__, name) from retry_exc
                except Exception as exc:
                    # MCP 服务重启后协议会话失效：进程级客户端仍持有旧 session id，
                    # 新旧聊天会话都会 Session terminated / 404。作废后重连并重试一次。
                    # 亦覆盖 cancel-scope 清理噪声（超时后首轮偶发）。
                    if not is_retryable_mcp_dead_session_error(exc):
                        raise
                    logger.warning(
                        "[mcp-timeout] %s.%s hit dead session (%s), reconnecting once: %s",
                        cls.__name__,
                        name,
                        exc,
                        getattr(self, "_server_path", "?"),
                    )
                    try:
                        await _connect_fresh(self, reason="dead-session")
                    except asyncio.CancelledError:
                        if _is_outer_cancellation():
                            raise
                        await _handle_spurious_cancel(phase="reconnect")
                        raise _transport_cancel_error(cls.__name__, name) from None
                    except Exception as reconnect_exc:
                        logger.warning(
                            "[mcp-timeout] %s.%s reconnect after dead session failed: %r",
                            cls.__name__,
                            name,
                            reconnect_exc,
                        )
                        raise exc from reconnect_exc
                    try:
                        return await _invoke_once()
                    except TimeoutError:
                        await _handle_timeout()
                        raise _timeout_error(cls.__name__, name, timeout) from None
                    except asyncio.CancelledError:
                        if _is_outer_cancellation():
                            raise
                        await _handle_spurious_cancel(phase="retry")
                        raise _transport_cancel_error(cls.__name__, name) from None

        setattr(cls, name, wrapped)

    # (A) HTTP long-poll transports — same failure mode when the server dies.
    for cls in (StreamableHttpClient, SseClient):
        _wrap_with_timeout(cls, "call_tool")
        _wrap_with_timeout(cls, "list_tools")
        # aclose 失败也必须清会话（原 SDK 只在 success 路径清理）
        _wrap_disconnect_always_invalidate(cls, "disconnect")
    # SseClient 真正清连接在 owner-task 的 _do_disconnect
    _wrap_disconnect_always_invalidate(SseClient, "_do_disconnect")

    _patch_sdk_read_timeouts()
    _patch_sse_disconnect_guard()

    # (B) Thread config.params["timeout_s"] (--timeout_s) onto each client so
    # (A) can pick it up. Browser-move 仅劫持自身 MCP server，不再全局替换
    # _create_client，因此企业远程 MCP 仍会走到本 stamp。
    _orig_create_client = getattr(ToolMgr, "_create_client")

    def _create_client_with_timeout(config):
        client = _orig_create_client(config)
        timeout_s = (getattr(config, "params", None) or {}).get("timeout_s")
        if isinstance(timeout_s, (int, float)) and timeout_s > 0:
            try:
                # setattr (not attribute assignment): McpClient has no
                # _jws_call_timeout field, so direct assignment is an mypy
                # [attr-defined] error. setattr keeps it dynamic & lint-clean.
                setattr(client, "_jws_call_timeout", float(timeout_s))
            except Exception as exc:
                # Don't silently drop the user's --timeout_s. If this client
                # can't hold the attribute (e.g. __slots__ without the field,
                # or a proxy object), surface it so ops notices the silent
                # fallback to DEFAULT_CALL_TIMEOUT instead of debugging blind.
                logger.warning(
                    "[mcp-timeout] failed to apply timeout_s=%.1fs to MCP client "
                    "%s: %r — falling back to default timeout",
                    float(timeout_s),
                    getattr(config, "server_name", "?"),
                    exc,
                )
        return client

    # setattr (not direct assignment): rebinding a staticmethod on the class is
    # an mypy [assignment] error; setattr keeps the monkeypatch lint-clean
    # (no type: ignore needed).
    setattr(ToolMgr, "_create_client", staticmethod(_create_client_with_timeout))

    # (C) AbilityManager 外层 fail_after → wait_for 桥（不改 agent-core）
    try:
        _patch_ability_manager_fail_after()
    except Exception as exc:  # noqa: BLE001 — 单测假模块环境可能无 AbilityManager
        logger.warning(
            "[mcp-timeout] AbilityManager fail_after bridge skipped: %r",
            exc,
        )

    logger.info(
        "[mcp-timeout] patch applied (default_timeout=%.1fs, "
        "covered=StreamableHttpClient,SseClient,AbilityManager,sdk_read_timeouts)",
        default_timeout,
    )
