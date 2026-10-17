# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""HealthCheck(旧 Heartbeat 探活)— Gateway 内周期性向 AgentServer 发送探活请求.

本模块承接旧 ``gateway/heartbeat/heartbeat.py`` 的探活能力：
旧 ``heartbeat.*`` 探活协议改名到 ``health_check.*`` 命名空间,与新
Heartbeat 任务(线程续跑,``gateway/heartbeat/``)严格区分。探活不再读取
``HEARTBEAT.md`` 或执行其中的用户任务。

按固定间隔向 AgentServer 发探活请求,检测 Agent 是否存活,
结果通过 ``health_check.relay`` 事件回传到指定 channel(默认 web)。
只做连通性检查,不再作为任务系统。

迁移已完成:旧 ``gateway/heartbeat/heartbeat.py`` shim 已删除;IM 渠道 8 文件
+ web_connect 的 relay 分支已全量切换到 ``EventType.HEALTH_CHECK_RELAY``;
探活配置已从 config.yaml 的 ``heartbeat`` 段迁移到 ``health_check`` 段。
环境变量 ``HEARTBEAT_RELAY_CHANNEL_ID`` 等保留旧名(运维侧不断裂)。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from jiuwenswarm.gateway.routing.agent_client import AgentServerClient
    from jiuwenswarm.gateway.message_handler import MessageHandler

# 探活请求使用的默认标识,AgentServer 可据此识别探活请求。
HEALTH_CHECK_CHANNEL_ID = "__health_check__"

HEALTH_CHECK_OK = "HEALTH_CHECK_OK"

# 探活请求只验证 AgentServer 请求链路，不读取或执行任何用户任务文件。
HEALTH_CHECK_PROMPT = "这是一次系统连通性检查。不要执行用户任务，仅回复 HEALTH_CHECK_OK。"


def normalize_active_hours(active_hours: dict[str, str] | None) -> dict[str, str] | None:
    """将 active_hours 的 start/end 规范为 "HH:MM" 字符串。

    YAML 中未加引号的 22:00 会被解析为 1320(60 进制),此处将数字转回 "HH:MM"。
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
    "HEALTH_CHECK_CHANNEL_ID",
    "HEALTH_CHECK_OK",
    "HEALTH_CHECK_PROMPT",
    "HealthCheckConfig",
    "IHealthCheck",
    "GatewayHealthCheckService",
    "normalize_active_hours",
]


@dataclass
class HealthCheckConfig:
    """HealthCheck 配置.

    interval_seconds: 探活间隔(秒),MUST > 0。
    timeout_seconds: 单次探活请求超时(秒),可选;若提供则 MUST > 0。
    channel_id: 探活请求使用的 channel_id,默认 __health_check__。
    relay_channel_id: 将探活响应内容回传的 channel_id(如 "web" 对应 WebChannel),
        从 .env 的 HEARTBEAT_RELAY_CHANNEL_ID 读取;为 None 则不回传。
    active_hours: 探活生效时间段,格式 {"start": "HH:MM", "end": "HH:MM"};None 表示始终生效。
    """

    interval_seconds: float
    timeout_seconds: float | None = None
    channel_id: str = HEALTH_CHECK_CHANNEL_ID
    relay_channel_id: str | None = None
    active_hours: dict[str, str] | None = None


class IHealthCheck(ABC):
    """HealthCheck 接口.

    按配置周期定时向 AgentServer 发送探活请求;
    不向任何 Channel 下发消息,成功/失败仅用于内部状态或回调。
    """

    @abstractmethod
    async def start(self) -> None:
        """启动周期任务;之后每隔 interval_seconds 执行一次探活."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """停止周期任务,不再发送探活."""
        ...

    @abstractmethod
    def is_running(self) -> bool:
        """返回周期任务是否正在运行."""
        ...


class GatewayHealthCheckService(IHealthCheck):
    """周期性向 AgentServer 发送探活请求的 IHealthCheck 实现。

    固定间隔运行循环,每次 _tick 发送一次请求;
    请求使用 HealthCheckConfig 中的 channel_id/session_id,不向任何 Channel 下发响应。

    判断是否成功:① 看日志:成功会打 INFO「Gateway health check OK」,失败会打 WARNING;
    ② 代码检查:用 last_tick_ok(True/False/None)、last_tick_at(最近一次执行时间)判断。
    """

    def __init__(
        self,
        agent_client: "AgentServerClient",
        config: HealthCheckConfig,
        message_handler: "MessageHandler | None" = None,
    ) -> None:
        self._agent_client = agent_client
        self._config = config
        self._message_handler = message_handler
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_tick_ok: bool | None = None
        self._last_tick_at: float | None = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "Gateway health check started (every %.1fs)",
            self._config.interval_seconds,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Gateway health check stopped")

    def is_running(self) -> bool:
        return self._running

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._config.interval_seconds)
                if self._running:
                    await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.exception("Gateway health check loop error: %s", e)

    async def _tick(self) -> None:
        """执行一次探活:构造 E2A 发往 AgentServer,不向 Channel 下发。"""
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields

        if not self._is_active_now():
            logger.debug(
                "Gateway health check skipped due to inactive hours: %r",
                self._config.active_hours,
            )
            return

        ts = format(int(time.time() * 1000), "x")
        suffix = secrets.token_hex(3)
        request_id = f"healthcheck-{ts}_{suffix}"
        session_id = f"health_check_{ts}_{suffix}"
        envelope = e2a_from_agent_fields(
            request_id=request_id,
            channel_id=self._config.channel_id,
            session_id=session_id,
            params={
                "health_check": HEALTH_CHECK_PROMPT,
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
            health_check_raw = payload.get("health_check")
            health_check_content = (
                health_check_raw if isinstance(health_check_raw, str) else ""
            )
            if not health_check_content:
                content = payload.get("content")
                if isinstance(content, dict):
                    output = content.get("output")
                    if isinstance(output, str):
                        health_check_content = output
                elif isinstance(content, str):
                    health_check_content = content
            logger.info("Gateway health check content: %s", health_check_content)
            if HEALTH_CHECK_OK in (
                health_check_content if isinstance(health_check_content, str) else ""
            ).upper():
                logger.info(
                    "Gateway health check OK: request_id=%s (last_tick_at=%.0f)",
                    request_id,
                    self._last_tick_at,
                )
            else:
                logger.info(
                    "Gateway health check complete: request_id=%s (last_tick_at=%.0f)",
                    request_id,
                    self._last_tick_at,
                )

            # 将响应内容作为 event 类型 Message 回传到配置的 channel(如 WebChannel)。
            # 发送 EventType.HEALTH_CHECK_RELAY(新值);接收端(8 个 IM 渠道 + web_connect)
            # 已在任务 2 全量切换到 HEALTH_CHECK_RELAY,端到端一致。
            if self._config.relay_channel_id and self._message_handler:
                from jiuwenswarm.common.schema.message import Message, EventType

                relay_msg = Message(
                    id=f"healthcheck-relay-{request_id}",
                    type="event",
                    channel_id=self._config.relay_channel_id,
                    session_id=session_id,
                    params={},
                    timestamp=time.time(),
                    ok=True,
                    payload={
                        "health_check": health_check_content,
                        # Deprecated payload alias for legacy relay consumers.
                        "heartbeat": health_check_content,
                    },
                    event_type=EventType.HEALTH_CHECK_RELAY,
                )
                await self._message_handler.publish_robot_messages(relay_msg)
                logger.debug(
                    "Gateway health check relay to channel %s",
                    self._config.relay_channel_id,
                )

        except asyncio.TimeoutError:
            self._last_tick_ok = False
            self._last_tick_at = time.time()
            logger.warning(
                "Gateway health check timeout (request_id=%s, timeout=%.1fs)",
                request_id,
                self._config.timeout_seconds or 0,
            )
        except Exception as e:  # noqa: BLE001
            self._last_tick_ok = False
            self._last_tick_at = time.time()
            logger.warning("Gateway health check request failed: %s", e)

    @property
    def last_tick_ok(self) -> bool | None:
        return self._last_tick_ok

    @property
    def last_tick_at(self) -> float | None:
        return self._last_tick_at

    def _is_active_now(self) -> bool:
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
                return start_minutes <= now_minutes < end_minutes
            return now_minutes >= start_minutes or now_minutes < end_minutes
        except Exception as e:  # noqa: BLE001
            logger.warning("Invalid health check active_hours config %r: %s", active_hours, e)
            return True

    def get_health_check_conf(self) -> dict[str, object]:
        """返回当前探活配置摘要(every/target/active_hours)。"""
        return {
            "every": self._config.interval_seconds,
            "target": self._config.relay_channel_id,
            "active_hours": normalize_active_hours(self._config.active_hours),
        }

    async def set_health_check_conf(
        self,
        *,
        every: float | None = None,
        target: str | None = None,
        active_hours: dict[str, str] | None = None,
    ) -> None:
        """更新探活配置并在需要时重启服务。"""
        updated = False

        if every is not None:
            if every <= 0:
                raise ValueError("health_check 'every' must be > 0")
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

        self._last_tick_ok = None
        self._last_tick_at = None

        if was_running:
            await self.start()

        logger.info(
            "Gateway health check config updated: every=%s, target=%s, active_hours=%s",
            self._config.interval_seconds,
            self._config.relay_channel_id,
            self._config.active_hours,
        )
