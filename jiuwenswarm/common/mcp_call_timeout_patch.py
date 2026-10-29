# coding: utf-8
"""Jiuwenswarm 的远程 MCP 超时、生命周期与死会话恢复补丁。

调用保留精确 wait_for 截止和 SDK timeout+5/read timeout 传递；连接、断开及
重连必须在同一个 owner 上执行。池化 client 使用现有 worker，ToolMgr HTTP
client 使用专用任务，SSE 使用 SDK owner。超时先 await 清理再重置，禁止把旧
AsyncExitStack 移到后台任务关闭。断开失败保留不可复用标记并报错，外部取消原样传播。
死会话最多重连重试一次（at-least-once）。ToolMgr timeout 印章、AbilityManager
模块内截止桥和工具 invoke 包装仍在唯一启动入口幂等安装。
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
# 清理等待与取消收尾分别有界，避免失活 owner 无回执时无限屏蔽调用方取消。
_MCP_CLEANUP_TIMEOUT_S = 10.0
_MCP_CANCEL_DRAIN_TIMEOUT_S = 1.0

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


class _McpInternalCancellationError(RuntimeError):
    """单次 I/O 内部取消且清理成功，可走既有的一次重连重试。"""


def force_invalidate_mcp_client(client: Any) -> None:
    """只标记连接不可复用；同步函数不能安全关闭或丢弃仍有资源的上下文。"""
    setattr(client, "_jws_needs_reconnect", True)


def _reset_closed_client(client: Any) -> None:
    """仅在所属任务完成 disconnect 后重置，不再安排后台 aclose。

    SSE 自行退出 session/transport，旧 stack 的回调不能再执行一次。
    """
    for attr in ("_session", "_client", "_read", "_write", "_auth_provider"):
        if hasattr(client, attr):
            setattr(client, attr, None)
    if hasattr(client, "_is_disconnected"):
        setattr(client, "_is_disconnected", True)
    if hasattr(client, "_exit_stack"):
        setattr(client, "_exit_stack", AsyncExitStack())
    force_invalidate_mcp_client(client)


async def disconnect_remote_mcp_client(
    client: Any, exc_type: Any = None, exc: BaseException | None = None, tb: Any = None,
) -> None:
    """在 owner 上有界清理；可用作 AsyncExitStack 的 exit，保留外部取消。"""
    if (getattr(client, "_is_disconnected", False)
            and getattr(client, "_session", None) is None
            and getattr(client, "_client", None) is None):
        # SSE connect 失败虽已清理资源，public disconnect 仍需停止它的 owner。
        owner = getattr(client, "_owner_task", None)
        if owner is None or owner.done():
            return
    try:
        owner = getattr(client, "_owner_task", None)
        if owner is not None and owner.done() and (
            getattr(client, "_session", None) is not None
            or getattr(client, "_client", None) is not None
        ):
            raise RuntimeError("SSE previous owner cleanup incomplete; replace client")
        async with asyncio.timeout(10.0):
            clean = await client.disconnect(timeout=10.0)
        if clean is False:
            raise RuntimeError("remote MCP disconnect returned false; cleanup incomplete")
    except BaseException as cleanup_error:
        force_invalidate_mcp_client(client)
        logger.warning("[mcp-timeout] remote MCP cleanup failed", exc_info=True)
        if isinstance(exc, asyncio.CancelledError) and isinstance(cleanup_error, Exception):
            return  # 已报告清理失败；不能用普通异常替代调用方的取消
        raise


async def _finish_mcp_task_after_cancel(
    task: asyncio.Task, *, deadline: float | None = None,
) -> Any:
    """在预算内保护 owner 清理，超时后取消并有界收尾；调用处继续传播原取消。

    asyncio.wait 不把等待者的取消转发给目标；AnyIO shield 避免外层已取消
    scope 反复唤醒。预算耗尽后才主动取消目标，不跨任务关闭任何资源。
    """
    import anyio

    loop = asyncio.get_running_loop()
    if deadline is None:
        deadline = loop.time() + _MCP_CLEANUP_TIMEOUT_S

    async def wait_until(end: float) -> None:
        while not task.done():
            remaining = end - loop.time()
            if remaining <= 0:
                return
            try:
                await asyncio.wait({task}, timeout=remaining)
            except asyncio.CancelledError:
                continue  # 后续取消只影响等待者，不能重置清理截止时间。

    def consume_result(finished: asyncio.Task) -> None:
        # 超时已报告；仍不响应取消的任务结束时取走异常，资源仍归原 owner。
        with contextlib.suppress(BaseException):
            finished.result()

    with anyio.CancelScope(shield=True):
        await wait_until(deadline)
        if task.done():
            return task.result()
        task.cancel()
        await wait_until(loop.time() + _MCP_CANCEL_DRAIN_TIMEOUT_S)
        if task.done():
            consume_result(task)
        else:
            task.add_done_callback(consume_result)
        raise TimeoutError("MCP cleanup deadline exceeded; cleanup incomplete")


def manage_sse_owner_cleanup(client: Any) -> None:
    """沿用 SDK 的 SSE owner，在建连被取消时让它自己清理再退出。

    SDK 的 connect 等待队列 future，取消等待者不会自动中断正在 initialize 的
    owner。只停止等待会把 disconnect 排到长时间挂起的 initialize 后面。
    """
    if getattr(client, "_jws_sse_cleanup_managed", False):
        return
    owner_loop = getattr(client, "_owner_loop", None)
    do_disconnect = getattr(client, "_do_disconnect", None)
    if not callable(owner_loop) or not callable(do_disconnect):
        return
    connect = client.connect
    disconnect = client.disconnect

    async def owner_with_cleanup():
        try:
            await owner_loop()
        finally:
            if getattr(client, "_session", None) is not None or getattr(client, "_client", None) is not None:
                async with asyncio.timeout(10.0):
                    clean = await do_disconnect(timeout=10.0)
                if clean is False:
                    force_invalidate_mcp_client(client)
                    raise RuntimeError("SSE owner disconnect returned false; cleanup incomplete")
                _reset_closed_client(client)

    async def connect_with_cleanup(*args, **kwargs):
        owner = getattr(client, "_owner_task", None)
        if owner is not None and owner.done() and (
            getattr(client, "_session", None) is not None
            or getattr(client, "_client", None) is not None
        ):
            raise RuntimeError("SSE previous owner cleanup incomplete; replace client")
        try:
            return await connect(*args, **kwargs)
        except asyncio.CancelledError:
            owner = getattr(client, "_owner_task", None)
            if owner is not None and not owner.done():
                # SDK 清理可能消费这次 cancel；退出标记仍须让其命令循环结束。
                setattr(client, "_stopping", True)
                if not owner.cancelling():
                    owner.cancel()
                try:
                    await _finish_mcp_task_after_cancel(owner)
                except asyncio.CancelledError:
                    pass  # owner 取消已被等待；调用方取消仍在下方传播
                except Exception:
                    force_invalidate_mcp_client(client)
                    logger.warning("[mcp-timeout] SSE owner cleanup failed", exc_info=True)
            raise

    async def disconnect_with_cleanup(*args, **kwargs):
        # public API 只向 SDK owner 投命令；实际 __aexit__ 始终留在原 owner。
        # 保住整个 public disconnect，才能执行命令完成后的停止 owner / 释放锁步骤。
        deadline = asyncio.get_running_loop().time() + _MCP_CLEANUP_TIMEOUT_S
        closing = asyncio.create_task(
            disconnect(*args, **kwargs), name="mcp-sse-disconnect"
        )
        try:
            return await asyncio.shield(closing)
        except asyncio.CancelledError as exc:
            current = asyncio.current_task()
            if current is None or not current.cancelling():
                # shield 的内部任务取消也会抛 CancelledError，不能伪装成用户取消。
                force_invalidate_mcp_client(client)
                raise RuntimeError("SSE disconnect cancelled internally; cleanup incomplete") from exc
            try:
                await _finish_mcp_task_after_cancel(closing, deadline=deadline)
            except asyncio.CancelledError:
                force_invalidate_mcp_client(client)
            except Exception:
                force_invalidate_mcp_client(client)
                logger.warning("[mcp-timeout] SSE disconnect cleanup failed", exc_info=True)
            raise

    setattr(client, "_owner_loop", owner_with_cleanup)
    setattr(client, "connect", connect_with_cleanup)
    setattr(client, "disconnect", disconnect_with_cleanup)
    setattr(client, "_jws_sse_cleanup_managed", True)


class _StreamableHttpOwner:
    """仅供没有池化 worker 的 ToolMgr HTTP client 使用的生命周期任务。

    池化连接已有 _run_mcp_worker，SSE 已有 SDK owner，均不叠加此任务。
    """

    def __init__(self, client: Any) -> None:
        self.client = client
        self.queue: asyncio.Queue = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.active: asyncio.Future | None = None

    async def submit(self, method: Any, name: str, args: tuple, kwargs: dict) -> Any:
        if asyncio.current_task() is self.task:
            return await method(*args, **kwargs)
        if self.task is not None and self.task.done() and (
            getattr(self.client, "_session", None) is not None
            or getattr(self.client, "_client", None) is not None
        ):
            raise RuntimeError("MCP HTTP previous owner cleanup incomplete; replace client")
        # 已断开的 client 不必创建 owner。
        if name == "disconnect" and (self.task is None or self.task.done()):
            if getattr(self.client, "_is_disconnected", False):
                return True
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run(), name="mcp-streamable-http-owner")
        task = self.task
        future = asyncio.get_running_loop().create_future()
        self.queue.put_nowait((method, name, args, kwargs, future))
        try:
            result = await future
            if name == "disconnect" or (name == "connect" and not result):
                await task
            return result
        except asyncio.CancelledError:
            # 排队者取消不能中断另一个聊天正在执行的命令。
            if self.active is not None and self.active is not future:
                raise
            if not task.cancelling():
                task.cancel()
            try:
                await _finish_mcp_task_after_cancel(task)
            except asyncio.CancelledError:
                pass  # owner 的取消已取出；调用方取消在下方继续传播
            except Exception:
                force_invalidate_mcp_client(self.client)
                logger.warning("[mcp-timeout] HTTP owner cleanup failed", exc_info=True)
            raise
        except Exception:
            if name in ("connect", "disconnect"):
                deadline = asyncio.get_running_loop().time() + _MCP_CLEANUP_TIMEOUT_S
                try:
                    # 操作已失败，owner 正在 finally 清理；等待者取消不能再打断它。
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    caller_cancelled = current is not None and current.cancelling() > 0
                    try:
                        await _finish_mcp_task_after_cancel(task, deadline=deadline)
                    except asyncio.CancelledError:
                        force_invalidate_mcp_client(self.client)
                        logger.warning("[mcp-timeout] HTTP owner cancelled during cleanup")
                    except Exception:
                        force_invalidate_mcp_client(self.client)
                        logger.warning("[mcp-timeout] HTTP owner cleanup failed", exc_info=True)
                    if caller_cancelled:
                        raise
                except Exception:
                    force_invalidate_mcp_client(self.client)
                    logger.warning("[mcp-timeout] HTTP owner cleanup failed", exc_info=True)
            raise

    async def run(self) -> None:
        failure: BaseException = RuntimeError("MCP HTTP owner stopped")
        disconnect_attempted = False
        try:
            while True:
                method, name, args, kwargs, future = await self.queue.get()
                if future.cancelled():
                    continue
                self.active = future
                disconnect_attempted = name == "disconnect"
                try:
                    if name in ("connect", "disconnect"):
                        timeout = kwargs.get("timeout", -1)
                        default = 60.0 if name == "connect" else 10.0
                        async with asyncio.timeout(timeout if timeout > 0 else default):
                            result = await method(*args, **kwargs)
                    else:
                        result = await method(*args, **kwargs)
                except BaseException as exc:
                    failure = exc
                    if not future.done():
                        future.set_exception(exc)
                    if not isinstance(exc, Exception) or name in ("connect", "disconnect"):
                        raise
                else:
                    if not future.done():
                        future.set_result(result)
                    if name == "disconnect" or (name == "connect" and not result):
                        break
                finally:
                    self.active = None
        except Exception as exc:
            failure = exc
            logger.warning("[mcp-timeout] HTTP owner stopped: %s", exc)
        finally:
            try:
                if not disconnect_attempted:
                    await disconnect_remote_mcp_client(self.client)
            finally:
                while not self.queue.empty():
                    *_, pending = self.queue.get_nowait()
                    if not pending.done():
                        # 一个调用者取消不应伪装成其它调用者的用户取消。
                        pending.set_exception(
                            failure if isinstance(failure, Exception)
                            else RuntimeError("MCP HTTP owner stopped during another call")
                        )


def _install_streamable_http_owner(client: Any) -> None:
    owner = _StreamableHttpOwner(client)
    # 包括资源 API：它们也必须和重连/断开串行，不能观察半关闭 session。
    for name in ("connect", "disconnect", "call_tool", "list_tools", "get_tool_info",
                 "list_resources", "read_resource"):
        method = getattr(client, name, None)
        if not callable(method):
            continue

        async def routed(*args, _method=method, _name=name, **kwargs):
            return await owner.submit(_method, _name, args, kwargs)

        setattr(client, name, routed)
    setattr(client, "_jws_http_owner", owner)


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
    """由生命周期 owner 完成清理后才允许重连。"""
    await disconnect_remote_mcp_client(client)
    logger.debug("[mcp-timeout] closed MCP session (%s)", context)


def _wrap_disconnect_always_invalidate(cls: type, method_name: str) -> None:
    """清理失败只标记不可复用，成功才清状态，避免丢失待清理资源。"""
    if (cls, method_name) in _wrapped_methods or not hasattr(cls, method_name):
        return
    _wrapped_methods.add((cls, method_name))
    orig = getattr(cls, method_name)

    async def wrapped(self, *args, **kwargs):
        force_invalidate_mcp_client(self)
        result = await orig(self, *args, **kwargs)
        if result is False:
            raise RuntimeError("remote MCP disconnect returned false; cleanup incomplete")
        _reset_closed_client(self)
        return result

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
                connected = await orig(self, *args, **kwargs)
                if connected:
                    setattr(self, "_jws_needs_reconnect", False)
                return connected
            finally:
                if token is not None:
                    _pending_sdk_read_timeout.reset(token)

        setattr(cls, method_name, method_with_read_timeout)


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
        # 调用和重连均由生命周期 owner 执行，先关旧连接再建新连接。
        await _abandon_session(client, context=f"before reconnect ({reason})")
        connect = getattr(client, "connect", None)
        if not callable(connect):
            raise RuntimeError("MCP client has no connect() for session recovery")
        try:
            async with asyncio.timeout(60.0):
                ok = await connect()
        except BaseException:
            try:
                await disconnect_remote_mcp_client(client)
            except Exception:
                logger.warning("[mcp-timeout] reconnect cleanup failed", exc_info=True)
            raise
        if not ok:
            await disconnect_remote_mcp_client(client)
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
        # 不丢弃仍活着的 AsyncExitStack，由 owner 同步完成清理。
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
                # 这里只执行单次 I/O；会持有上下文的重连/清理在外层 owner 执行。
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

            async def _invoke_with_cleanup():
                try:
                    return await _invoke_once()
                except TimeoutError:
                    try:
                        await _handle_timeout()
                    except Exception as cleanup_error:
                        raise _timeout_error(cls.__name__, name, timeout) from cleanup_error
                    raise _timeout_error(cls.__name__, name, timeout) from None
                except asyncio.CancelledError as exc:
                    # I/O 自身取消不等于调用者取消；清理仍留在生命周期 owner。
                    current = asyncio.current_task()
                    outer_cancel = current is not None and current.cancelling() > 0
                    try:
                        await _abandon_session(self, context="call cancelled")
                    except asyncio.CancelledError as cleanup_error:
                        if outer_cancel or (current is not None and current.cancelling()):
                            raise
                        raise RuntimeError(
                            "MCP internal cancellation cleanup failed"
                        ) from cleanup_error
                    except Exception as cleanup_error:
                        if not outer_cancel and not (current is not None and current.cancelling()):
                            # 清理失败不能进入重试，否则会在未关闭的上下文上重连。
                            raise RuntimeError(
                                "MCP internal cancellation cleanup failed"
                            ) from cleanup_error
                        logger.warning("[mcp-timeout] cleanup after cancellation failed", exc_info=True)
                    if not outer_cancel and not (current is not None and current.cancelling()):
                        raise _McpInternalCancellationError(
                            f"MCP {cls.__name__}.{name} cancelled internally"
                        ) from exc
                    raise

            async with _client_io_lock(self):
                await _ensure_connected(self)
                try:
                    return await _invoke_with_cleanup()
                except (TimeoutError, asyncio.CancelledError):
                    raise
                except Exception as exc:
                    if not (
                        isinstance(exc, _McpInternalCancellationError)
                        or is_retryable_mcp_dead_session_error(exc)
                    ):
                        raise
                    # 保留死会话的一次 at-least-once 重试；清理失败不能当成已断开。
                    await _connect_fresh(self, reason="dead-session")
                    try:
                        return await _invoke_with_cleanup()
                    except Exception:
                        force_invalidate_mcp_client(self)
                        raise

        setattr(cls, name, wrapped)

    # (A) HTTP long-poll transports — same failure mode when the server dies.
    for cls in (StreamableHttpClient, SseClient):
        _wrap_with_timeout(cls, "call_tool")
        _wrap_with_timeout(cls, "list_tools")
        # 只有成功关闭才能重置状态，失败保留不可复用标记。
        _wrap_disconnect_always_invalidate(cls, "disconnect")

    _patch_sdk_read_timeouts()

    # (B) Thread config.params["timeout_s"] (--timeout_s) onto each client so
    # (A) can pick it up. Browser-move 仅劫持自身 MCP server，不再全局替换
    # _create_client，因此企业远程 MCP 仍会走到本 stamp。
    _orig_create_client = getattr(ToolMgr, "_create_client")

    def _create_client_with_timeout(config):
        client = _orig_create_client(config)
        if isinstance(client, StreamableHttpClient):
            _install_streamable_http_owner(client)
        elif isinstance(client, SseClient):
            manage_sse_owner_cleanup(client)
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
