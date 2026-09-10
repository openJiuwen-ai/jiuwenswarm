# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""jiuwenclaw 进程关停时主动回收 jiuwenbox 远端沙箱。

jiuwenbox 服务端**没有** idle TTL (参见
``openjiuwen.extensions.sys_operation.sandbox.providers.jiuwenbox._JiuwenBoxClient.delete_sandbox``
的源码注释 — "sandbox registry 视为 ephemeral across restarts"), 所以 jiuwenclaw
进程退出后, openjiuwen jiuwenbox provider 进程内缓存的 ``sandbox_id`` 会随
Python 进程一起消失, 但 jiuwenbox 那一头对应的 ``bwrap`` 守护进程仍然活着,
长期累积会让 jiuwenbox 服务端的活跃 sandbox 数线性增长直到 jiuwenbox 本身重启
才会自然 GC。

本模块只暴露一个同步入口 :func:`shutdown_jiuwenbox_sandboxes`, 由
``app_agentserver.py`` 在 ``finally`` 段通过 ``asyncio.to_thread`` 调用, 做的事:

  1. 从 :func:`jiuwenclaw.config.get_sandbox_endpoint` 读 ``base_url`` (即
     ``config.yaml::sandbox.url``)。
  2. 调
     :func:`openjiuwen.extensions.sys_operation.sandbox.providers.jiuwenbox.clear_jiuwenbox_shared_sandbox`
     把**本进程**已知的 ``sandbox_id`` 列表一次性 ``pop`` 出来 (其内部走类锁,
     线程安全)。
  3. 对每个 id 用一个独立、短超时的 ``_JiuwenBoxClient`` 同步发
     ``DELETE /api/v1/sandboxes/{id}``; 单个失败 (网络断、jiuwenbox 已先退出、
     远端已不认这个 id 等) 只打 ``warning``, 继续清下一个。
  4. 永远不向上抛异常, 不阻塞 jiuwenclaw 自身的关停流程。

设计原则是**"软清"**: 只删本 Python 进程缓存里的 sandbox, 不去
``GET /api/v1/sandboxes`` 扫整张表删全部 — 这样多 jiuwenclaw 进程共用同一台
jiuwenbox 时, 不会出现"A 关停顺手把 B 的活跃 sandbox 也回收掉"的串台。

不在本模块兜底处理 ``SIGKILL`` / OOM / kernel panic 这类强杀场景: 那种情况
Python 不会跑任何 finalizer, 唯一根治办法是让 jiuwenbox 服务端实现 idle TTL,
属于"动 jiuwenbox 代码"的范围, 不在本次范围里。
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# 单条 ``DELETE /api/v1/sandboxes/{id}`` 的超时。
# - 过大: jiuwenbox 卡死时整个 jiuwenclaw 关停会被拖很久 (sandbox 数 ×
#   timeout 是最坏情况)。
# - 过小: 慢盘 / 跨容器场景下正常 DELETE 也会被中途切断, 沙箱泄漏。
# 5s 是经验值: 本机 jiuwenbox DELETE 实测在 200ms 量级, 跨 docker 在 1s 量级;
# 5s 给一倍以上余量。需要调可以再开 env 入口, 暂时常量更稳。
_PER_DELETE_TIMEOUT_SECONDS: float = 5.0


def shutdown_jiuwenbox_sandboxes() -> int:
    """关停时回收本进程缓存里的所有 jiuwenbox sandbox。

    本函数是**同步**入口, 内部 HTTP 调用走 ``httpx.Client``, 调用方应在 worker
    线程里跑 (例如 ``await asyncio.to_thread(shutdown_jiuwenbox_sandboxes)``)
    以免阻塞 event loop 的关停。

    Returns:
        实际 ``DELETE`` 成功 (或 ``404`` 幂等成功) 的 sandbox 个数; 任何环节
        出错都只记 ``warning``, 已经成功删的部分如实返回, 不抛异常。
    """
    try:
        # 局部 import: 避免 jiuwenclaw 启动时就拉 openjiuwen 沙箱 provider 链路
        # (沙箱可能并未启用)。这条 import 只在关停时发生一次, 不影响冷启动。
        from openjiuwen.extensions.sys_operation.sandbox.providers.jiuwenbox import (
            _JiuwenBoxClient,
            clear_jiuwenbox_shared_sandbox,
        )
        from jiuwenswarm.common.config import get_sandbox_endpoint
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[sandbox_lifecycle] dependency import failed, skip cleanup: %s",
            exc,
        )
        return 0

    try:
        endpoint = get_sandbox_endpoint()
        base_url = str(endpoint.get("url") or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[sandbox_lifecycle] read sandbox endpoint failed: %s", exc,
        )
        return 0

    if not base_url:
        # sandbox.url 没配, 也就根本不会有 provider 创建过任何远端 sandbox,
        # 缓存必为空。直接早退, 跳过 DELETE 路径。
        logger.debug(
            "[sandbox_lifecycle] sandbox.url is empty, nothing to clean",
        )
        return 0

    try:
        sandbox_ids = list(clear_jiuwenbox_shared_sandbox(base_url))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[sandbox_lifecycle] clear shared cache failed (base_url=%s): %s",
            base_url, exc,
        )
        return 0

    if not sandbox_ids:
        logger.debug(
            "[sandbox_lifecycle] no cached sandboxes for %s", base_url,
        )
        return 0

    try:
        # 共用一个 client 比每个 sandbox 新建一个更轻量 (复用底层 httpx 连接池),
        # 同时所有 DELETE 共享同一个超时配置。
        client = _JiuwenBoxClient(
            base_url,
            timeout_seconds=_PER_DELETE_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[sandbox_lifecycle] build _JiuwenBoxClient failed (base_url=%s): %s",
            base_url, exc,
        )
        return 0

    released = 0
    try:
        for sandbox_id in sandbox_ids:
            if not sandbox_id:
                continue
            try:
                client.delete_sandbox(sandbox_id)
                released += 1
                logger.info(
                    "[sandbox_lifecycle] DELETE jiuwenbox sandbox ok: %s",
                    sandbox_id,
                )
            except Exception as exc:  # noqa: BLE001
                # 404 已被 ``_JiuwenBoxClient.delete_sandbox`` 内部吞为幂等成功;
                # 进到这里的多半是网络断 / 5xx / 超时。jiuwenbox 重启时这些 id
                # 自然消失, 不会无限累积。
                logger.warning(
                    "[sandbox_lifecycle] DELETE jiuwenbox sandbox %s failed: %s",
                    sandbox_id, exc,
                )
    finally:
        client.close()

    logger.info(
        "[sandbox_lifecycle] released %d/%d jiuwenbox sandbox(es) for %s",
        released, len(sandbox_ids), base_url,
    )
    return released


async def recreate_all_sandboxes() -> int:
    """销毁本进程已知的所有 jiuwenbox 沙箱, 让下次 exec 按需 lazy 建新沙箱.

    用途: ``sandbox.files.set`` 后, 旧沙箱 ACL 已施加无法热改, 必须销毁重建才能让
    新文件白/黑名单生效 (新沙箱创建时读运行时副本的新 ACL). 网络 (egress) 配置变更
    靠重启 box-server 自动清沙箱, 不走这里 (见 sandbox_config_rpc._apply_sandbox_change).

    实现复用 :func:`shutdown_jiuwenbox_sandboxes` (弹缓存 id + DELETE), 走
    ``asyncio.to_thread`` 避免阻塞 event loop. 失败只 warning, 不抛 (best-effort).

    Returns: 实际回收的沙箱数.
    """
    try:
        return await asyncio.to_thread(shutdown_jiuwenbox_sandboxes)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[sandbox_lifecycle] recreate_all_sandboxes failed: %s", exc,
        )
        return 0


__all__ = ["shutdown_jiuwenbox_sandboxes", "recreate_all_sandboxes"]