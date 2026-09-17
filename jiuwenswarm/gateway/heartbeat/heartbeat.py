# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Heartbeat - Gateway 内周期性向 AgentServer 发送探活请求."""

from __future__ import annotations

import logging
import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING
import secrets

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from jiuwenswarm.gateway.routing.agent_client import AgentServerClient

# 心跳请求使用的默认标识，AgentServer 可据此识别探活请求
HEARTBEAT_CHANNEL_ID = "__heartbeat__"

HEARTBEAT_OK = "HEARTBEAT_OK"

# 探活请求发送的 content，AgentServer 可识别为心跳
HEARTBEAT_PROMPT = "如果你的workspace目录存在HEARTBEAT.md文件, 读取文件内容并且根据文件内容执行任务. 如果没有HEARTBEAT.md文件, 仅回复HEARTBEAT_OK"


def normalize_active_hours(active_hours: dict[str, str] | None) -> dict[str, str] | None:
    """将 active_hours 的 start/end 规范为 "HH:MM" 字符串。

    YAML 中未加引号的 22:00 会被解析为 1320（60 进制），此处将数字转回 "HH:MM"。
    """
    if not active_hours or not isinstance(active_hours, dict):
        return active_hours
    result: dict[str, str] = {}
    for k, v in active_hours.items():
        if k in ("start", "end") and isinstance(v, (int, float)):
            minutes = int(v)
            h, m = divmod(minutes, 60)
            result[k] = f"{h:02d}:{m:02d}"
        elif isinstance(v, str):
            result[k] = v
        else:
            result[k] = str(v) if v is not None else ""
    return result


__all__ = [
    "HEARTBEAT_CHANNEL_ID",
    "HEARTBEAT_PROMPT",
    "HeartbeatConfig",
    "IHeartbeat",
    "GatewayHeartbeatService",
    "normalize_active_hours",
]


@dataclass
class HeartbeatConfig:
    """Heartbeat 配置.

    interval_seconds: 心跳间隔（秒），MUST > 0。
    timeout_seconds: 单次心跳请求超时（秒），可选；若提供则 MUST > 0。
    channel_id: 心跳请求使用的 channel_id，默认 __heartbeat__。
    session_id: 心跳请求使用的 session_id，默认 __heartbeat__。
    relay_channel_id: 将心跳响应内容回传的 channel_id（如 "web" 对应 WebChannel），
        从 .env 的 HEARTBEAT_RELAY_CHANNEL_ID 读取；为 None 则不回传。
    """

    interval_seconds: float
    timeout_seconds: float | None = None
    channel_id: str = HEARTBEAT_CHANNEL_ID
    relay_channel_id: str | None = None
    # 心跳生效时间段，格式为 {"start": "HH:MM", "end": "HH:MM"}；为 None 表示始终生效
    active_hours: dict[str, str] | None = None


class IHeartbeat(ABC):
    """Heartbeat 接口.

    按配置周期定时向 AgentServer 发送探活请求；
    不向任何 Channel 下发消息，成功/失败仅用于内部状态或回调。
    """

    @abstractmethod
    async def start(self) -> None:
        """启动周期任务；之后每隔 interval_seconds 执行一次心跳."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """停止周期任务，不再发送心跳."""
        ...

    @abstractmethod
    def is_running(self) -> bool:
        """返回周期任务是否正在运行."""
        ...


class GatewayHeartbeatService(IHeartbeat):
    """
    周期性向 AgentServer 发送探活请求的 IHeartbeat 实现。

    固定间隔运行循环，每次 _tick 发送一次请求；
    请求使用 HeartbeatConfig 中的 channel_id/session_id，不向任何 Channel 下发响应。

    判断是否成功：① 看日志：成功会打 INFO「Gateway heartbeat OK」，失败会打 WARNING；
    ② 代码检查：用 last_tick_ok（True/False/None）、last_tick_at（最近一次执行时间）判断。
    """

    def __init__(
            self,
            agent_client: "AgentServerClient",
            config: HeartbeatConfig,
            message_handler: "MessageHandler | None" = None,
    ) -> None:
        self._agent_client = agent_client
        self._config = config
        self._message_handler = message_handler
        self._running = False
        self._task: asyncio.Task | None = None
        # 最近一次心跳结果，便于调用方判断是否成功
        self._last_tick_ok: bool | None = None
        self._last_tick_at: float | None = None

    async def start(self) -> None:
        """启动周期任务；之后每隔 interval_seconds 执行一次心跳."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "Gateway heartbeat started (every %.1fs)",
            self._config.interval_seconds,
        )

    async def stop(self) -> None:
        """停止周期任务，不再发送心跳."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Gateway heartbeat stopped")

    def is_running(self) -> bool:
        """返回周期任务是否正在运行."""
        return self._running

    async def _run_loop(self) -> None:
        """主循环：每隔 interval_seconds 执行一次 _tick."""
        while self._running:
            try:
                await asyncio.sleep(self._config.interval_seconds)
                if self._running:
                    await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Gateway heartbeat loop error: %s", e)

    async def _tick(self) -> None:
        """执行一次探活：构造 E2A 发往 AgentServer，不向 Channel 下发."""
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields

        # 若当前时间不在 active_hours 配置范围内，则跳过本次心跳
        if not self._is_active_now():
            logger.debug(
                "Gateway heartbeat skipped due to inactive hours: %r",
                self._config.active_hours,
            )
            return

        ts = format(int(time.time() * 1000), "x")
        suffix = secrets.token_hex(3)
        request_id = f"heartbeat-{ts}_{suffix}"
        session_id = f"heartbeat_{ts}_{suffix}"
        envelope = e2a_from_agent_fields(
            request_id=request_id,
            channel_id=self._config.channel_id,
            session_id=session_id,
            params={
                "heartbeat": HEARTBEAT_PROMPT,
                "run": {
                    "kind": "heartbeat",
                    "context": {
                        "reason": "interval",
                        "session_id": session_id,
                    },
                },
            },
        )
        try:
            if self._config.timeout_seconds is not None and self._config.timeout_seconds > 0:
                resp = await asyncio.wait_for(
                    self._agent_client.send_request(envelope),
                    timeout=self._config.timeout_seconds,
                )
            else:
                resp = await self._agent_client.send_request(envelope)
            self._last_tick_at = time.time()
            self._last_tick_ok = True
            payload = resp.payload if isinstance(resp.payload, dict) else {}
            heartbeat_raw = payload.get("heartbeat")
            heartbeat_content = heartbeat_raw if isinstance(heartbeat_raw, str) else ""
            if not heartbeat_content:
                # 兼容 Agent 在执行 HEARTBEAT.md 任务时返回的 chat 结构：
                # payload = {"content": {"output": "...", "result_type": "answer"}}
                content = payload.get("content")
                if isinstance(content, dict):
                    output = content.get("output")
                    if isinstance(output, str):
                        heartbeat_content = output
                elif isinstance(content, str):
                    heartbeat_content = content
            logger.info("Gateway heartbeat content: %s", heartbeat_content)
            if HEARTBEAT_OK in (heartbeat_content if isinstance(heartbeat_content, str) else "").upper():
                logger.info("Gateway heartbeat OK: request_id=%s (last_tick_at=%.0f)", request_id, self._last_tick_at)
            else:
                logger.info("Gateway heartbeat complete: request_id=%s (last_tick_at=%.0f)", request_id,
                            self._last_tick_at)

            # 将 resp.payload["heartbeat"] 作为 event 类型 Message 回传到配置的 channel（如 WebChannel）
            if self._config.relay_channel_id and self._message_handler:
                from jiuwenswarm.common.schema.message import Message, EventType
                relay_msg = Message(
                    id=f"heartbeat-relay-{request_id}",
                    type="event",
                    channel_id=self._config.relay_channel_id,
                    session_id=session_id,
                    params={},
                    timestamp=time.time(),
                    ok=True,
                    payload={"heartbeat": heartbeat_content},
                    event_type=EventType.HEARTBEAT_RELAY,
                )
                await self._message_handler.publish_robot_messages(relay_msg)
                logger.debug("Gateway heartbeat relay to channel %s", self._config.relay_channel_id)

        except asyncio.TimeoutError:
            self._last_tick_ok = False
            self._last_tick_at = time.time()
            logger.warning(
                "Gateway heartbeat timeout (request_id=%s, timeout=%.1fs)",
                request_id,
                self._config.timeout_seconds or 0,
            )
        except Exception as e:
            self._last_tick_ok = False
            self._last_tick_at = time.time()
            logger.warning("Gateway heartbeat request failed: %s", e)

    @property
    def last_tick_ok(self) -> bool | None:
        """最近一次心跳是否成功。None 表示尚未执行过任何一次 tick."""
        return self._last_tick_ok

    @property
    def last_tick_at(self) -> float | None:
        """最近一次心跳执行时间（Unix 时间戳）。None 表示尚未执行过."""
        return self._last_tick_at

    def _is_active_now(self) -> bool:
        """根据 active_hours 判断当前时间心跳是否应当生效."""
        active_hours = normalize_active_hours(self._config.active_hours)
        if not active_hours:
            return True
        try:
            start_str = active_hours.get("start")
            end_str = active_hours.get("end")
            if not (isinstance(start_str, str) and isinstance(end_str, str)):
                return True

            def _parse_hm(s: str) -> int:
                parts = s.split(":", 1)
                if len(parts) != 2:
                    raise ValueError(f"invalid time format: {s!r}")
                h = int(parts[0])
                m = int(parts[1])
                return h * 60 + m

            start_minutes = _parse_hm(start_str)
            end_minutes = _parse_hm(end_str)

            now_struct = time.localtime()
            now_minutes = now_struct.tm_hour * 60 + now_struct.tm_min

            if start_minutes <= end_minutes:
                # 普通区间：如 08:00-22:00
                return start_minutes <= now_minutes < end_minutes
            # 跨午夜区间：如 22:00-06:00
            return now_minutes >= start_minutes or now_minutes < end_minutes
        except Exception as e:  # noqa: BLE001
            logger.warning("Invalid heartbeat active_hours config %r: %s", active_hours, e)
            # 配置非法时，为避免误停心跳，按“始终生效”处理
            return True

    def get_heartbeat_conf(self) -> dict[str, object]:
        """返回当前心跳配置摘要（every/target/active_hours）。active_hours 的 start/end 统一为 "HH:MM" 字符串。"""
        return {
            "every": self._config.interval_seconds,
            "target": self._config.relay_channel_id,
            "active_hours": normalize_active_hours(self._config.active_hours),
        }

    async def set_heartbeat_conf(
            self,
            *,
            every: float | None = None,
            target: str | None = None,
            active_hours: dict[str, str] | None = None,
    ) -> None:
        """更新心跳配置并在需要时重启 Heartbeat 服务."""
        updated = False

        if every is not None:
            if every <= 0:
                raise ValueError("heartbeat 'every' must be > 0")
            self._config.interval_seconds = float(every)
            updated = True

        if target is not None:
            self._config.relay_channel_id = target
            updated = True

        if active_hours is not None:
            self._config.active_hours = active_hours
            updated = True

        if not updated:
            return

        was_running = self._running
        if was_running:
            await self.stop()

        # 重置最近一次心跳状态
        self._last_tick_ok = None
        self._last_tick_at = None

        if was_running:
            await self.start()

        logger.info(
            "Gateway heartbeat config updated: every=%s, target=%s, active_hours=%s",
            self._config.interval_seconds,
            self._config.relay_channel_id,
            self._config.active_hours,
        )
