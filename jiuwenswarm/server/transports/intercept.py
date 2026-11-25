# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""出站事件流拦截层。

把用户扩展注册的映射函数套在真正的 ``ResponseSink`` 外面：每条出站帧先编码成 E2A wire
交给扩展，再按扩展的返回值发送。设计见
``jiuwenswarm/server/docs/features/F_01_outbound-event-stream-hook.md``。

三条不变量：

1. **默认零变化**：未命中配置时本模块不参与，``ctx.sink`` 保持原对象。
2. **失败不混协议**：还没有发出改造帧时允许回落原生；一旦发出过改造帧，后续失败只能
   终止本轮（``on_error`` 提供的帧或空），不能把 E2A 原帧混进扩展协议里。
3. **故障不影响用户**：扩展抛错只记日志，不断连接、不重试。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.e2a.wire_codec import (
    encode_agent_chunk_for_wire,
    encode_agent_response_for_wire,
    is_e2a_response_wire_dict,
)
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions.sdk.outbound_stream import (
    OutboundStreamExtension,
    StreamMapContext,
)
from jiuwenswarm.server.transports.sink import (
    ResponseSink,
    SSESink,
    UnaryHTTPSink,
)

logger = logging.getLogger(__name__)

#: 配置段名：``outbound_stream.channels: {<channel_id>: <extension_name>}``。
CONFIG_SECTION = "outbound_stream"

#: 只映射"流式 chat 请求"的出口；其它请求（session.create / history.get / 权限等）
#: 一律原样透传，避免扩展把一元响应当事件处理掉。
_STREAMING_CHAT_METHODS = frozenset(
    {
        ReqMethod.CHAT_SEND,
        ReqMethod.CHAT_RESUME,
        ReqMethod.CHAT_ANSWER,
    }
)


def resolve_channel_id(request: Any) -> str:
    """请求渠道的归一化口径（选路与 StreamMapContext 必须一致）。"""
    return str(getattr(request, "channel_id", "") or "web").strip() or "web"


def detect_transport(sink: Any) -> str:
    """根据 sink 类型判断这条流从哪种传输出去。"""
    if isinstance(sink, SSESink):
        return "http-sse"
    if isinstance(sink, UnaryHTTPSink):
        return "http-unary"
    return "ws"


def resolve_outbound_stream(request: Any) -> OutboundStreamExtension | None:
    """按配置 + 注册表决定本次请求要不要走扩展。

    返回 ``None`` 表示完全保持原样。任何一步不确定（没配置、没注册、配置读失败）
    都返回 ``None``，让请求走原生链路。
    """
    if request is None or not bool(getattr(request, "is_stream", False)):
        return None
    method = getattr(request, "req_method", None)
    if method not in _STREAMING_CHAT_METHODS:
        return None

    channel = resolve_channel_id(request)
    try:
        config = get_config()
    except Exception:  # noqa: BLE001 - 配置读失败不应影响请求
        logger.warning("[OutboundStream] get_config failed; fallback to native", exc_info=True)
        return None

    section = config.get(CONFIG_SECTION) if isinstance(config, dict) else None
    channels = section.get("channels") if isinstance(section, dict) else None
    name = str((channels or {}).get(channel) or "").strip()
    if not name:
        return None

    from jiuwenswarm.extensions.registry import ExtensionRegistry

    try:
        registry = ExtensionRegistry.get_instance()
    except RuntimeError:
        logger.warning(
            "[OutboundStream] channel %s maps to %r but ExtensionRegistry is not initialized",
            channel,
            name,
        )
        return None
    extension = registry.get_outbound_stream_extension(name)
    if extension is None:
        logger.warning(
            "[OutboundStream] channel %s maps to unknown extension %r; fallback to native",
            channel,
            name,
        )
        return None
    return extension


class InterceptingSink:
    """包在真实 sink 外面的一层：把 E2A wire 交给扩展，再按返回值发送。"""

    #: 原始帧返回类型（扩展自定义协议，不装 E2A 信封）。
    RAW_FRAME_TYPE = "raw"

    def __init__(
        self,
        inner: ResponseSink,
        extension: OutboundStreamExtension,
        ctx: StreamMapContext,
    ) -> None:
        self._inner = inner
        self._extension = extension
        self._ctx = ctx
        #: 扩展模式开始前就失败 → 后续全走原生。
        self._disabled = False
        #: 已经发出过改造帧、随后失败 → 后续全部丢弃，绝不混入原生帧。
        self._terminated = False
        #: 是否已经**成功发出**过"非原样"的帧（一旦为真就不能再回落原生）。
        #: 注意：扩展返回 []（丢帧）或发送失败都不算，客户端一个字节都没收到。
        self._engaged = False
        #: 是否见过可识别的 E2A 终止帧（仅用于收尾诊断）。
        self._terminal_seen = False
        #: 本轮是否产出过 raw 帧（raw 的终态由扩展负责，核心不做"缺终帧"告警）。
        self._raw_seen = False

    # ── ResponseSink 五方法 ──────────────────────────────────────

    async def send_unary(self, resp: Any, *, response_id: str | None = None) -> bool:
        if self._terminated:
            return False
        if self._disabled:
            return await self._inner.send_unary(resp, response_id=response_id)
        wire = encode_agent_response_for_wire(
            resp, response_id=response_id or getattr(resp, "request_id", "") or ""
        )
        frames = self._map(wire)
        if len(frames) == 1 and frames[0] is wire:
            return await self._inner.send_unary(resp, response_id=response_id)
        return await self._send_outputs(frames)

    async def send_chunk(
        self,
        chunk: Any,
        *,
        sequence: int,
        response_id: str | None = None,
    ) -> bool:
        if self._terminated:
            return False
        if self._disabled:
            return await self._inner.send_chunk(
                chunk, sequence=sequence, response_id=response_id
            )
        wire = encode_agent_chunk_for_wire(
            chunk,
            response_id=response_id or getattr(chunk, "request_id", "") or "",
            sequence=sequence,
        )
        frames = self._map(wire)
        if len(frames) == 1 and frames[0] is wire:
            return await self._inner.send_chunk(
                chunk, sequence=sequence, response_id=response_id
            )
        return await self._send_outputs(frames)

    async def send_wire(self, wire: dict[str, Any]) -> bool:
        if self._terminated:
            return False
        if self._disabled:
            return await self._inner.send_wire(wire)
        frames = self._map(wire)
        if len(frames) == 1 and frames[0] is wire:
            return await self._inner.send_wire(wire)
        return await self._send_outputs(frames)

    async def send_error(
        self,
        request_id: str,
        message: str,
        *,
        code: str = "INTERNAL_ERROR",
        channel_id: str = "",
    ) -> bool:
        # 还没进入扩展模式：错误帧原样发；已经进入：原生错误帧会混协议，丢弃。
        if self._terminated:
            return False
        if self._engaged:
            logger.warning(
                "[OutboundStream] native error frame suppressed in extension mode "
                "request_id=%s code=%s",
                request_id,
                code,
            )
            return False
        return await self._inner.send_error(
            request_id, message, code=code, channel_id=channel_id
        )

    async def send_raw(self, data: Any) -> bool:
        return await self._inner.send_raw(data)

    # ── 收尾 ───────────────────────────────────────────────────

    def close(self) -> None:
        """请求结束（含取消）时调用：通知扩展收尾，并做诊断。"""
        try:
            self._extension.on_close(self._ctx)
        except Exception:  # noqa: BLE001 - 收尾失败不改变请求结果
            logger.warning(
                "[OutboundStream] on_close failed channel=%s request_id=%s",
                self._ctx.channel_id,
                self._ctx.request_id,
                exc_info=True,
            )
        if self._engaged and not self._terminal_seen and not self._raw_seen:
            logger.warning(
                "[OutboundStream] extension produced no recognizable terminal frame "
                "channel=%s request_id=%s (check on_close / is_final mapping)",
                self._ctx.channel_id,
                self._ctx.request_id,
            )

    # ── 映射与发送 ─────────────────────────────────────────────

    def _map(self, wire: dict[str, Any]) -> list[dict[str, Any]]:
        """调用扩展；失败时按"是否已发出过改造帧"决定降级方式。

        `_engaged` 不在这里置位：只有 `_send_outputs` 真正把改造帧发出去之后才算
        进入扩展模式。扩展返回 `[]`（丢帧/缓冲）或发送失败时，客户端仍是零字节，
        后续失败允许回落原生。
        """
        try:
            produced = list(self._extension.map_frame(wire, self._ctx))
            frames = self._validate(produced)
        except Exception as exc:  # noqa: BLE001 - 扩展故障 fail-open
            return self._handle_failure(wire, exc)

        if any(frame.get("type") == self.RAW_FRAME_TYPE for frame in frames):
            self._raw_seen = True
        if any(self._is_terminal(frame) for frame in frames):
            self._terminal_seen = True
        return frames

    @classmethod
    def _validate(cls, frames: Iterable[Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for frame in frames:
            if not isinstance(frame, dict):
                raise TypeError(f"map_frame must return dict frames, got {type(frame).__name__}")
            if frame.get("type") == cls.RAW_FRAME_TYPE:
                cls._validate_raw(frame)
                out.append(frame)
                continue
            if not is_e2a_response_wire_dict(frame):
                raise ValueError(
                    "map_frame returned neither an E2A wire frame nor a raw frame"
                )
            out.append(frame)
        return out

    @classmethod
    def _validate_raw(cls, frame: dict[str, Any]) -> None:
        """raw 帧的 ``data`` 必须是可 JSON 序列化的值。

        序列化发生在 sink 里（``WSSink.send_raw`` 的 ``json.dumps`` / SSE 泵），
        处于 ``_map`` / ``_handle_failure`` 的异常保护之外：一旦抛错会沿 handler
        一路击穿，客户端既收不到错误帧也收不到终止帧。因此在 ``_validate`` 里提前
        探测，把「扩展违反 raw 契约」并入与其它非法返回同一条降级路径。
        """
        if "data" not in frame:
            raise ValueError('raw frame must carry a "data" field')
        try:
            json.dumps(frame["data"], ensure_ascii=False)
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise TypeError(f"raw frame data is not JSON-serializable: {exc}") from exc

    @classmethod
    def _is_terminal(cls, frame: dict[str, Any]) -> bool:
        if frame.get("type") == cls.RAW_FRAME_TYPE:
            # raw 是扩展自己的协议，核心无法判断其终态；由扩展在 on_close 里负责。
            return False
        return bool(frame.get("is_final"))

    def _handle_failure(
        self,
        wire: dict[str, Any],
        exc: BaseException,
    ) -> list[dict[str, Any]]:
        logger.warning(
            "[OutboundStream] map_frame failed channel=%s request_id=%s engaged=%s",
            self._ctx.channel_id,
            self._ctx.request_id,
            self._engaged,
            exc_info=exc,
        )
        if not self._engaged:
            # 还没向客户端发出过改造帧：回落原生，对客户端完全透明。
            self._disabled = True
            return [wire]

        # 已经进入扩展模式：不能再混入 E2A 原帧，交给扩展给终止帧后停流。
        try:
            frames = self._validate(self._extension.on_error(self._ctx, exc))
        except Exception:  # noqa: BLE001
            logger.exception(
                "[OutboundStream] on_error failed channel=%s request_id=%s",
                self._ctx.channel_id,
                self._ctx.request_id,
            )
            frames = []
        if any(frame.get("type") == self.RAW_FRAME_TYPE for frame in frames):
            self._raw_seen = True
        self._terminated = True
        # on_error 产出的就是本轮终止帧（无论扩展定义了哪种协议），不再告警"缺终帧"。
        self._terminal_seen = True
        return frames

    async def _send_outputs(self, frames: list[dict[str, Any]]) -> bool:
        ok = True
        sent_any = False
        for frame in frames:
            if frame.get("type") == self.RAW_FRAME_TYPE:
                sent = await self._inner.send_raw(frame.get("data"))
            else:
                sent = await self._inner.send_wire(frame)
            if sent:
                sent_any = True
            ok = bool(sent) and ok
        # 只有真正送达过改造帧才算进入扩展模式；纯丢帧 / 全部发送失败保持 False，
        # 这样后续 map_frame 失败仍可按契约回落原生。
        if sent_any:
            self._engaged = True
        return ok
