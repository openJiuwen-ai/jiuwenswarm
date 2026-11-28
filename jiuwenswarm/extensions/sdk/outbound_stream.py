# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""出站事件流扩展接口。

上游集成方（AG-UI 前端、relay-claw 这类本机客户端、或统一事件语义的中间层）可以继承
:class:`OutboundStreamExtension`，在 :meth:`OutboundStreamExtension.map_frame` 里把
JiuwenSwarm 的 E2A 事件映射成自己的协议事件。核心只负责：

- 按 ``config.yaml`` 的 ``outbound_stream.channels`` 选择扩展；
- 在每个出站帧上调用 ``map_frame``；
- 按返回值发送（E2A 帧 / raw 帧 / 丢弃）；
- 扩展故障时按约定做降级，不弄坏正常对话。

接口契约见 ``jiuwenswarm/server/docs/features/F_01_outbound-event-stream-hook.md``。
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Any, Iterable

from jiuwenswarm.extensions.sdk.base import BaseExtension


@dataclass(frozen=True)
class StreamMapContext:
    """一次请求的只读上下文，扩展按需取用。"""

    #: 这条流从哪种传输出去：``ws`` / ``http-sse`` / ``http-unary``。
    transport: str
    #: 请求来自哪个渠道（``channel_id``），例如 ``officeclaw`` / ``web`` / ``acp``。
    channel_id: str
    #: 本轮请求唯一 id；扩展按它区分不同轮次。
    request_id: str
    #: 会话 id；同一会话多轮复用。
    session_id: str | None
    #: 连接 id；WS 用连接对象 id，HTTP 为常量。
    connection_id: str
    #: 原始 ``AgentRequest``（只读）。仅在需要读 ``params`` 时使用。
    request: Any = None

    @property
    def req_method(self) -> str:
        """请求方法串（如 ``chat.send``）；取不到时返回空串。"""
        method = getattr(self.request, "req_method", None)
        return str(getattr(method, "value", "") or "")

    @property
    def params(self) -> dict[str, Any]:
        """请求参数（只读）；非 dict 时返回空 dict。"""
        params = getattr(self.request, "params", None)
        return params if isinstance(params, dict) else {}


class OutboundStreamExtension(BaseExtension):
    """出站事件流扩展基类。

    子类至少实现 :meth:`map_frame`；``on_open`` / ``on_close`` / ``on_error`` 都是
    可选钩子，不覆盖时按默认语义处理。
    """

    @abstractmethod
    def map_frame(
        self,
        frame: dict[str, Any],
        ctx: StreamMapContext,
    ) -> Iterable[dict[str, Any]]:
        """处理一帧（同步、无 IO、按到达顺序调用）。

        入参 ``frame`` 是 E2A 响应 wire（与上游实际收到的形状一致），大致为::

            {
              "request_id": "req-1",
              "sequence": 3,
              "is_final": False,
              "response_kind": "e2a.chunk",
              "body": {"event_type": "chat.delta", "delta": "你好"},
            }

        返回值表达"这一帧怎么发"：

        - ``[]``：丢弃；
        - ``[frame]``：原样发送（返回同一对象时核心走原 sink 路径）；
        - ``[改过的 frame, ...]``：按返回值发送；
        - ``[{"type": "raw", "data": ...}]``：不装 E2A 信封，直接发送 ``data``
          （WS 发 JSON 文本，SSE 只发 ``data:`` 行）。``data`` 必须可 JSON 序列化；
          不可序列化按"返回非法"降级，不会把序列化异常漏给传输层。
        """
        raise NotImplementedError

    def on_open(self, ctx: StreamMapContext) -> None:
        """本次请求进入扩展模式后调用一次；抛错会让本次请求回落到原生出口。"""
        return None

    def on_error(
        self,
        ctx: StreamMapContext,
        error: BaseException,
    ) -> Iterable[dict[str, Any]]:
        """扩展模式已开始、但 ``map_frame`` 抛错时调用一次。

        此时前端已经收到过改造后的帧，不能再回落成 E2A（会混协议）；扩展可返回
        自己的终止帧（例如 ``RUN_ERROR``），返回空表示直接停流。
        """
        return []

    def on_close(self, ctx: StreamMapContext) -> None:
        """本次请求结束时调用一次（成功 / 取消 / 异常都会调用）。"""
        return None
