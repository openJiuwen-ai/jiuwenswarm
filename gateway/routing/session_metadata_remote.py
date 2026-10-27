# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""session_metadata 域的 WS 远端 provider（server_store_bridge 的拆仓替代件）。

背景：``gateway/server_store_bridge.py`` 通过 import server 侧模块完成
``common.external_providers`` 的本地注册；gateway 独立成仓后该桥整体删除，
session_metadata 域改由本模块的远端实现承接。远端调用复用既有
Gateway ↔ AgentServer WebSocket 通道上的 ``SESSION_GET_METADATA`` E2A
method（server 侧 ``gateway_adapter/session_adapter.py`` 已有只读处理器），
不新增任何 HTTP 服务面——内部文件传输（下载拦截 / E2A 分块上传）已先一步
验证了 WS 承载旁路请求的形态。

语义对齐本地实现 ``server.runtime.session.session_metadata``：

- ``cache_bust``：server 侧处理器固定直读盘（跨进程拿最新），远端读天然
  每次最新，本参数被接受但不再有行为差异；
- ``enable_writeback``：本地实现的推断写盘副作用发生在 server 侧读路径
  自身（默认开启），远端读同样触发（在 AgentServer 进程内），语义透传；
- 缺失会话/不可达/超时一律返回空 dict（消费方"读不到就当没有"的既有
  语义），错误细节进日志。

bridge 退役后本实现是 session_metadata 域在 gateway 进程的唯一注册
形态（``app_gateway`` 启动装配）。
"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.common.external_providers import register
from jiuwenswarm.common.schema.message import ReqMethod

from gateway.routing.e2a_proxy import fetch_agent_unary

logger = logging.getLogger(__name__)

#: 会话元数据远端读取的 unary 超时（秒）。刻意低于 video_duplex 的
#: AGENT_QUERY_TIMEOUT（20s）：元数据是小载荷单条读，且消费方含
#: CHAT_CANCEL 等时延敏感控制路径——超时即按"读不到"降级，宁可短等待
#: 也不要把控制报文阻塞在元数据查询后面。
SESSION_METADATA_QUERY_TIMEOUT_SECONDS = 5.0

#: 内部调用通道标识；刻意避开 ``web``，不触发 server 侧排队会话消息的附加逻辑。
_SESSION_METADATA_CHANNEL_ID = "gateway"

_agent_client: Any = None


def bind_agent_client(client: Any) -> None:
    """绑定 Gateway → AgentServer 客户端（app_gateway 启动装配时调用）。"""
    global _agent_client
    _agent_client = client


class SessionMetadataRemoteUnavailable(RuntimeError):
    """远端读传输失败（不可达/超时/路由拒绝）——区别于"会话不存在"。

    控制路径消费方必须区分这两种失败：读失败时不应把"未知"当成"读为
    空"下结论（例如定向取消会被误放大成会话级取消）。仅 server 侧明确
    返回"未找到"（NOT_FOUND）时才返回空 dict。
    """


class SessionMetadataRemoteProvider:
    """session_metadata 域的 WS 远端实现（异步为主入口）。

    本地实现是同步函数；跨进程 WS 往返天然异步，故远端实现的真正入口是
    ``get_session_metadata_async``。同步入口保留协议签名但降级返回空
    dict 并告警（远端模式下没有本地桥可回落，未迁移的同步消费方——如
    ``trajectory_http`` 的 metadata_loader 注入契约——以"读不到元数据"
    优雅降级而非报错），其迁移随拆仓批次单独处理。

    失败语义：传输失败（不可达/超时/路由拒绝）抛
    ``SessionMetadataRemoteUnavailable``；仅 server 侧判定会话不存在
    （NOT_FOUND）返回空 dict——与本地实现"缺失会话返回空 dict"的语义
    对齐。本域的写方法（update_session_model_selection 等）在
    AgentServer 进程执行，gateway 远端模式不承载，经 ``__getattr__``
    得到带域信息的显式错误。
    """

    @staticmethod
    def __getattr__(name: str) -> Any:
        raise RuntimeError(
            f"session_metadata remote provider does not carry '{name}': "
            "the method runs in the AgentServer process "
            "(registered there via provider_setup); the gateway remote "
            "mode only serves the read entries"
        )

    @staticmethod
    def get_session_metadata(
        session_id: str,
        cache_bust: bool = False,
        *,
        enable_writeback: bool = True,
    ) -> dict[str, Any]:
        """同步入口降级：无法同步等待 WS 往返，返回空 dict 并告警。"""
        logger.warning(
            "[session-metadata-remote] sync entry degraded in remote mode; "
            "returning empty metadata: session_id=%s（migrate to "
            "get_session_metadata_async）",
            session_id,
        )
        return {}

    @staticmethod
    async def get_session_metadata_async(
        session_id: str,
        cache_bust: bool = False,
        *,
        enable_writeback: bool = True,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """经 WS 读取目标会话元数据。

        ``user_id``：AgentOS/ADX 多用户路由部署的必需身份（路由层对空
        user_id 直接拒绝，不传则每次读都会失败）；单机/单用户路由可省。
        有 user 上下文的调用方（TUI/Web 面板、消息处理）应传入。
        传输失败（不可达/超时/路由拒绝）抛
        ``SessionMetadataRemoteUnavailable``；会话不存在（NOT_FOUND）
        返回空 dict，对齐本地实现语义。
        """
        ok, payload = await fetch_agent_unary(
            agent_client=_agent_client,
            req_method=ReqMethod.SESSION_GET_METADATA,
            params={
                "session_id": session_id,
                # 只读语义随调用方声明透传（server 侧读路径据此关闭推断写盘），
                # 与本地适配器的参数透传行为一致
                "enable_writeback": bool(enable_writeback),
            },
            session_id=session_id,
            user_id=user_id or None,
            channel_id=_SESSION_METADATA_CHANNEL_ID,
            label="session.metadata",
            timeout_seconds=SESSION_METADATA_QUERY_TIMEOUT_SECONDS,
        )
        if not ok:
            code = (payload or {}).get("code")
            if code == "NOT_FOUND":
                return {}
            logger.warning(
                "[session-metadata-remote] get failed: session_id=%s error=%s code=%s",
                session_id,
                (payload or {}).get("error"),
                code,
            )
            raise SessionMetadataRemoteUnavailable(
                f"session metadata read failed: session_id={session_id} code={code}"
            )
        if not isinstance(payload, dict):
            return {}
        return payload


def install() -> None:
    """把 WS 远端实现注册进 ``common.external_providers`` 的 session_metadata 域。"""
    register("session_metadata", SessionMetadataRemoteProvider())
    logger.info(
        "[session-metadata-remote] registered session_metadata provider (WS)"
    )
