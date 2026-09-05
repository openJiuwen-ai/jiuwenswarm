# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""沙箱配置 RPC: E2A / AgentRequest 入口, 返回 AgentResponse.

与 :mod:`jiuwenclaw.agentserver.permissions.config_rpc` 同形态. 对外暴露 8 个
``sandbox.*`` WS 方法 (经 ``agent_ws_server._handle_agent_request_body`` 派发):

  - ``sandbox.enabled.get/set``        沙箱开关 (config.yaml::sandbox.enabled, 基础配置)
  - ``sandbox.startup_mode.get/set``   沙箱启动方式 (config.yaml::sandbox.startup_mode)
  - ``sandbox.files.get/set``          文件白/黑名单 (运行时副本 user_overrides.files)
  - ``sandbox.network.get/set``        网络配置 (运行时副本 user_overrides.network)

set 成功后异步触发生效 (``_apply_sandbox_change``):
  - enabled: 关闭时清理 Runner.resource_mgr 里残留的沙箱 sysop (防 SkillTurbo 兜底命中),
    不动 box-server (只影响新会话 _create_sys_operation 选 LOCAL/SANDBOX).
  - startup_mode: 热重载 agent; internal→external 停掉自拉起的 box-server;
    external→internal 下次 bootstrap 拉起.
  - files: set_sandbox_files_config 已 render 副本; 显式销毁活沙箱 (新 ACL 只作用于新沙箱),
    不重启 box-server (ACL 在沙箱创建时读).
  - network: set_sandbox_network_config 已 render 副本; 重启 box-server (重跑 lifespan 重建
    EgressFilter); 活沙箱由重启的 shutdown_all_sandboxes 副作用自动清.
  - jbx-sandbox 用户从不重建 (安装期产物, ensure_windows_setup 幂等).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod

logger = logging.getLogger(__name__)

# 主事件循环句柄: agent-server 启动时由 register_main_loop() 注入.
# _trigger_apply 是同步 RPC handler (可能在工作线程调用), 必须用
# run_coroutine_threadsafe 把协程投递回主 loop, 而不能用已 deprecated 的
# asyncio.get_event_loop().create_task() (Python 3.10+ 在无 running loop 的线程里
# 会新建/复用错误 loop, 导致协程在错误的 loop 上调度).
_main_loop: asyncio.AbstractEventLoop | None = None


def register_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    """供 agent-server 主循环线程在启动后注入其 running loop 句柄."""
    global _main_loop
    _main_loop = loop

_SANDBOX_CFG_METHODS: frozenset[ReqMethod] = frozenset(
    {
        ReqMethod.SANDBOX_ENABLED_GET,
        ReqMethod.SANDBOX_ENABLED_SET,
        ReqMethod.SANDBOX_STARTUP_MODE_GET,
        ReqMethod.SANDBOX_STARTUP_MODE_SET,
        ReqMethod.SANDBOX_FILES_GET,
        ReqMethod.SANDBOX_FILES_SET,
        ReqMethod.SANDBOX_NETWORK_GET,
        ReqMethod.SANDBOX_NETWORK_SET,
    }
)


def get_sandbox_config_req_methods() -> frozenset[ReqMethod]:
    """返回 sandbox 配置 req_method 集合, 供 agent_ws_server 分组派发."""
    return _SANDBOX_CFG_METHODS


def _ok(request: AgentRequest, payload: dict[str, Any] | None) -> AgentResponse:
    return AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=True,
        payload=payload or {},
        metadata=request.metadata,
    )


def _err(
    request: AgentRequest, message: str, *, code: str = "BAD_REQUEST"
) -> AgentResponse:
    return AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=False,
        payload={"error": message, "code": code},
        metadata=request.metadata,
    )


def _teardown_registered_sandbox_sysops() -> int:
    """关闭沙箱开关时, 清理 Runner.resource_mgr 里残留的沙箱 sysop.

    问题: 第一轮会话创建的沙箱 sysop 注册到全局 Runner.resource_mgr 后, 即使下轮
    _create_sys_operation 读 enabled=False 选了 local, SkillTurbo 等经
    Runner.resource_mgr.get_sys_operation() 无参兜底拿 sysop 的路径仍会命中上一轮残留的
    沙箱 sysop → 关了开关照样进沙箱. 关闭开关时主动遍历已注册 sysop, 移除沙箱类型
    (isolation_key_template 非空) 的, local 类型保留.

    返回移除的沙箱 sysop 数.
    """
    try:
        from openjiuwen.core.runner import Runner
    except Exception as exc:  # noqa: BLE001
        logger.debug("[sandbox] teardown: openjiuwen 未就绪, 跳过 (%s)", exc)
        return 0
    rm = getattr(Runner, "resource_mgr", None)
    if rm is None:
        return 0
    try:
        registered = rm.get_sys_operation()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[sandbox] teardown: get_sys_operation 失败 (%s)", exc)
        return 0
    # get_sys_operation(None) 可能返回单实例/列表/None.
    if registered is None:
        return 0
    if not isinstance(registered, list):
        registered = [registered]
    removed = 0
    for sysop in registered:
        if sysop is None:
            continue
        try:
            iso_key = getattr(sysop, "isolation_key_template", None)
        except Exception:  # noqa: BLE001
            iso_key = None
        if not iso_key:
            # local sysop (isolation_key_template 为 None), 不动.
            continue
        op_id = getattr(sysop, "id", None)
        try:
            rm.remove_sys_operation(op_id)
            removed += 1
            logger.info(
                "[sandbox] teardown: 移除残留沙箱 sysop id=%s isolation_key=%s",
                op_id, iso_key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[sandbox] teardown: 移除沙箱 sysop id=%s 失败: %s", op_id, exc
            )
    return removed


async def _apply_sandbox_change(kind: str) -> None:
    """set 后的生效动作 (异步, 不阻塞 RPC 响应).

    kind ∈ {'enabled', 'startup_mode', 'files', 'network'}.

    关键认知: box-server 的 SandboxManager 在启动时一次性加载 root policy 到
    ``self.policy`` (PolicyReader.load_policy, sandbox_manager.py:228), 之后**不重读文件**.
    所以无论是文件 ACL (沙箱创建时经 _resolve_effective_policy(None)→deep-copy root) 还是
    网络 egress (lifespan 启动时建 EgressFilter), 用户改运行时副本后, **必须重启 box-server**
    才能让新副本被重新加载 + 应用到新沙箱. jbx-sandbox 用户不重建 (ensure_windows_setup 幂等),
    活沙箱由重启的 shutdown_all_sandboxes 副作用自动清.
    """
    try:
        if kind == "enabled":
            # enabled 只影响 _create_sys_operation 选 LOCAL/SANDBOX (读 config.yaml,
            # update_sandbox_runtime 已 clear_config_cache, 下次读即新值), 不动 box-server.
            # 但关闭时须清理两层残留, 否则 LOCAL sysop 仍会命中旧 sandbox_id 照样进沙箱:
            #   1. Runner.resource_mgr 里残留的沙箱 sysop (_teardown_registered_sandbox_sysops)
            #   2. jiuwenbox provider 的 _shared_sandbox_ids 缓存 + 对应的 box-server 沙箱进程
            #      (shutdown_jiuwenbox_sandboxes 清缓存并 DELETE 远端沙箱)
            # 只清第 1 层不够: provider 缓存的 sandbox_id 会被后续 LOCAL sysop 的 bash exec
            # 复用 (provider _get_sandbox_id 命中缓存), 命令仍发到旧沙箱.
            from jiuwenswarm.common.config import get_sandbox_runtime
            if not bool(get_sandbox_runtime().get("enabled")):
                removed = _teardown_registered_sandbox_sysops()
                # 清 provider 缓存 + 删残留沙箱进程 (同步 HTTP 调用, 放 worker 线程不阻塞 event loop).
                from jiuwenswarm.server.sandbox_lifecycle import shutdown_jiuwenbox_sandboxes
                released = await asyncio.to_thread(shutdown_jiuwenbox_sandboxes)
                logger.info(
                    "[sandbox] enabled 变更为关闭, 已移除 %d 个残留沙箱 sysop, 释放 %d 个残留沙箱进程, 下轮 _create_sys_operation 读新值生效",
                    removed, released,
                )
            else:
                logger.info("[sandbox] enabled 变更为开启, 下轮 _create_sys_operation 读新值生效")
            return
        if kind == "startup_mode":
            # 模式切换: internal→external 停掉自拉起的 box-server; external→internal 下次拉起.
            from jiuwenswarm.common.config import get_sandbox_startup_mode
            from jiuwenswarm.server.sandbox.jiuwenbox_runner import JiuwenBoxRunner

            runner = JiuwenBoxRunner.instance()
            mode = get_sandbox_startup_mode()
            if mode == "external" and runner.owns_process:  # noqa: SLF001 - JiuwenBoxRunner 内部状态访问
                logger.info(
                    "[sandbox] startup_mode=external, 停掉 agent-server 拉起的 box-server"
                )
                await runner.stop()
            # internal 时下次 _bootstrap_internal_jiuwenbox 拉起; 这里不主动拉.
            return
        if kind in ("files", "network"):
            # 文件 ACL + 网络 egress 都需重启 box-server 重载 root policy 副本.
            # 重启的 lifespan shutdown 调 shutdown_all_sandboxes 自动清活沙箱 (新配置只作
            # 用于新沙箱); jbx-sandbox 用户不重建 (ensure_windows_setup 幂等).
            from jiuwenswarm.server.sandbox.jiuwenbox_runner import JiuwenBoxRunner

            runner = JiuwenBoxRunner.instance()
            if not runner.owns_process or runner.process is None:  # noqa: SLF001 - JiuwenBoxRunner 内部状态访问
                logger.info(
                    "[sandbox] %s 变更但 box-server 非 agent-server 拉起 (external), 跳过重启",
                    kind,
                )
                return
            logger.info(
                "[sandbox] %s 变更, 重启 box-server 重载运行时 policy 副本", kind,
            )
            await runner.ensure_running(
                host=runner.host,
                port=runner.port,
                startup_mode="internal",
                policy_path=runner.spawned_policy_path,
                timeout=120.0,
            )
            return
        logger.warning("[sandbox] unknown apply kind: %s", kind)
    except Exception:  # noqa: BLE001
        logger.exception("[sandbox] _apply_sandbox_change(%s) failed", kind)


def _trigger_apply(kind: str) -> None:
    """在主事件循环里异步触发生效 (不阻塞 RPC 响应).

    本函数是同步 RPC handler 的调用路径, 可能在工作线程执行; 用
    run_coroutine_threadsafe 把协程投递到 agent-server 主 loop (由
    register_main_loop 注入), 而非已 deprecated 的 asyncio.get_event_loop().
    """
    loop = _main_loop
    if loop is None or loop.is_closed():
        # 无可用主 loop (未注入/已关闭, 测试环境常见): 同步降级 (files/network 的
        # IO 可能阻塞, 但至少把 enabled/startup_mode 的日志打出来). 正常路径不会走到.
        logger.warning(
            "[sandbox] 无可用主事件循环, 跳过异步生效 (kind=%s)", kind
        )
        return
    try:
        asyncio.run_coroutine_threadsafe(_apply_sandbox_change(kind), loop)
    except RuntimeError:
        logger.warning(
            "[sandbox] 投递异步生效失败 (kind=%s)", kind
        )


def dispatch_sandbox_config_request(request: AgentRequest) -> AgentResponse:
    """执行一条 sandbox 配置 RPC (与 dispatch_permissions_config_request 同形态).

    Returns: AgentResponse (ok + payload / error + code).
    """
    from jiuwenswarm.common.config import (
        get_sandbox_runtime,
        update_sandbox_runtime,
        get_sandbox_startup_mode,
        update_sandbox_startup_mode,
    )
    from jiuwenswarm.server.sandbox_policy_render import (
        get_sandbox_files_config,
        set_sandbox_files_config,
        get_sandbox_network_config,
        set_sandbox_network_config,
    )

    m = request.req_method
    params = request.params if isinstance(request.params, dict) else {}
    tag = m.value if m is not None else ""

    try:
        # ---- 接口1a/1b: 沙箱开关 (存 config.yaml, 基础配置) ----
        if m == ReqMethod.SANDBOX_ENABLED_GET:
            return _ok(
                request,
                {"enabled": bool(get_sandbox_runtime().get("enabled"))},
            )

        if m == ReqMethod.SANDBOX_ENABLED_SET:
            value = params.get("enabled")
            if not isinstance(value, bool):
                return _err(request, "enabled must be boolean")
            update_sandbox_runtime({"enabled": value})
            _trigger_apply("enabled")
            return _ok(request, {"enabled": value})

        # ---- 接口1c/1d: 沙箱启动方式 (存 config.yaml, 基础配置) ----
        if m == ReqMethod.SANDBOX_STARTUP_MODE_GET:
            return _ok(request, {"startup_mode": get_sandbox_startup_mode()})

        if m == ReqMethod.SANDBOX_STARTUP_MODE_SET:
            mode = params.get("startup_mode")
            if not isinstance(mode, str) or not mode.strip():
                return _err(request, "startup_mode is required")
            try:
                normalized = update_sandbox_startup_mode(mode)
            except ValueError as exc:
                return _err(request, str(exc))
            _trigger_apply("startup_mode")
            return _ok(request, {"startup_mode": normalized})

        # ---- 接口2: 文件安全 (读写运行时副本, 不碰 config.yaml) ----
        if m == ReqMethod.SANDBOX_FILES_GET:
            return _ok(request, {"files": get_sandbox_files_config()})

        if m == ReqMethod.SANDBOX_FILES_SET:
            allow = params.get("allow")
            deny = params.get("deny")
            if not isinstance(allow, list) or not isinstance(deny, list):
                return _err(request, "allow and deny must be lists")
            try:
                files = set_sandbox_files_config(allow, deny)
            except ValueError as exc:
                return _err(request, str(exc))
            _trigger_apply("files")
            return _ok(request, {"files": files})

        # ---- 接口3: 网络安全 (读写运行时副本) ----
        if m == ReqMethod.SANDBOX_NETWORK_GET:
            return _ok(request, {"network": get_sandbox_network_config()})

        if m == ReqMethod.SANDBOX_NETWORK_SET:
            disable_all = params.get("disable_all")
            allow_domains = params.get("allow_domains")
            deny_domains = params.get("deny_domains")
            if not isinstance(disable_all, bool):
                return _err(request, "disable_all must be boolean")
            if not isinstance(allow_domains, list) or not isinstance(deny_domains, list):
                return _err(request, "allow_domains and deny_domains must be lists")
            try:
                network = set_sandbox_network_config(
                    disable_all, allow_domains, deny_domains
                )
            except ValueError as exc:
                return _err(request, str(exc))
            _trigger_apply("network")
            return _ok(request, {"network": network})

    except ValueError as exc:
        return _err(request, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("[%s] %s", tag, exc)
        return _err(request, str(exc), code="INTERNAL_ERROR")

    return _err(request, "unknown sandbox req_method", code="BAD_REQUEST")


__all__ = [
    "dispatch_sandbox_config_request",
    "get_sandbox_config_req_methods",
]
