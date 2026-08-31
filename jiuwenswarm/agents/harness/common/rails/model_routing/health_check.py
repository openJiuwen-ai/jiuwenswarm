"""model_routing.health_check — 模型端点健康检查。

设计原则：
1. 不修改 ModelCapability — 健康状态完全由 ModelHealthChecker 内部 _status_map 缓存维护
2. 缓存 TTL — 默认 600s（10 分钟），用 time.monotonic() 不受系统时钟调整影响
3. 进程级共享 — 缓存存在 ModelHealthChecker 实例上，不持久化（重启后重新检查）
4. 全不健康回退 — 全不健康时回退原表，不阻断路由
5. 能力验证 — 通用模型用纯文本 ping；vision/audio 模型发送已知内容验证模型真正具备多模态能力
6. 后台异步 — 由 ModelRoutingRail 在首次 before_invoke 时启动后台循环周期调用，
   路由决策直接读缓存，不阻塞
"""
from __future__ import annotations
import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from jiuwenswarm.common.utils import logger
from .capability import ModelCapability
from .health_assets import _RED_SQUARE_PNG_BASE64, _NIHAO_WAV_BASE64


# --------------------------------------------------------------------------- #
# 数据类
# --------------------------------------------------------------------------- #

@dataclass
class HealthStatus:
    """单个模型的健康状态记录。"""
    model_id: str
    model_name: str
    model_type: str = ""
    healthy: bool = True
    last_check_time: float = 0.0
    last_failure_reason: Optional[str] = None
    consecutive_failures: int = 0
    consecutive_successes: int = 0


@dataclass
class HealthCheckConfig:
    """健康检查配置。"""
    enabled: bool = True
    interval_seconds: float = 600.0
    timeout_seconds: float = 10.0
    max_rounds: int = 1
    max_consecutive_failures: int = 2
    recovery_consecutive_successes: int = 1
    health_check_prompt: str = "hi"
    max_tokens: int = 1

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> HealthCheckConfig:
        """从字典构建，缺失字段用默认值。"""
        if not d or not isinstance(d, dict):
            return cls()
        return cls(
            enabled=bool(d.get("enabled", True)),
            interval_seconds=float(d.get("interval_seconds", 600.0)),
            timeout_seconds=float(d.get("timeout_seconds", 10.0)),
            max_rounds=int(d.get("max_rounds", 1)),
            max_consecutive_failures=int(d.get("max_consecutive_failures", 2)),
            recovery_consecutive_successes=int(d.get("recovery_consecutive_successes", 1)),
            health_check_prompt=str(d.get("health_check_prompt", "hi")),
            max_tokens=int(d.get("max_tokens", 1)),
        )


# --------------------------------------------------------------------------- #
# ModelHealthChecker
# --------------------------------------------------------------------------- #

class ModelHealthChecker:
    """模型端点健康检查器。"""

    def __init__(self, config: HealthCheckConfig) -> None:
        self._config = config
        self._status_map: dict[str, HealthStatus] = {}

    @property
    def interval_seconds(self) -> float:
        """健康检查间隔（秒），供外部读取配置，避免直接访问 _config。"""
        return self._config.interval_seconds

    async def update_health(self, caps: list[ModelCapability]) -> None:
        """更新所有 TTL 过期模型的健康状态。"""
        if not self._config.enabled:
            return
        now = time.monotonic()
        expired: list[ModelCapability] = []
        for cap in caps:
            key = cap.model_id or cap.model_name
            status = self._status_map.get(key)
            if status is None:
                expired.append(cap)
            elif (now - status.last_check_time) >= self._config.interval_seconds:
                expired.append(cap)

        if not expired:
            return

        # 并发检查 TTL 过期的模型
        tasks = [self._check_model_safe(cap) for cap in expired]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for cap, result in zip(expired, results):
            key = cap.model_id or cap.model_name
            prev = self._status_map.get(key)
            if isinstance(result, Exception):
                # 检查本身异常 → 保守视为健康，不阻断路由
                logger.debug("[ModelRouting] health check exception for %s: %s", cap.model_name, result)
                if prev is None:
                    self._status_map[key] = HealthStatus(
                        model_id=key, model_name=cap.model_name, model_type=cap.model_type,
                        healthy=True, last_check_time=now,
                    )
                else:
                    prev.last_check_time = now
                continue

            new_status: HealthStatus = result
            # 首次检查（prev=None）也需要走连续失败判定，避免单次失败直接判死
            if prev is not None:
                new_status.consecutive_failures = prev.consecutive_failures
                new_status.consecutive_successes = prev.consecutive_successes
            # 根据本次检查结果更新连续计数
            if new_status.healthy:
                new_status.consecutive_successes = (prev.consecutive_successes if prev else 0) + 1
                new_status.consecutive_failures = 0
            else:
                new_status.consecutive_failures = (prev.consecutive_failures if prev else 0) + 1
                new_status.consecutive_successes = 0

            # 判定健康状态：连续失败达到阈值才判不健康，否则保守视为健康
            if new_status.consecutive_failures >= self._config.max_consecutive_failures:
                new_status.healthy = False
            elif new_status.consecutive_successes >= self._config.recovery_consecutive_successes:
                new_status.healthy = True
            elif prev is not None:
                new_status.healthy = prev.healthy  # 保持上一次
            else:
                new_status.healthy = True  # 首次检查失败，保守视为健康

            self._status_map[key] = new_status

    def get_healthy_caps(self, caps: list[ModelCapability]) -> list[ModelCapability]:
        """返回健康子集。"""
        if not self._config.enabled:
            return caps

        healthy: list[ModelCapability] = []
        for cap in caps:
            key = cap.model_id or cap.model_name
            status = self._status_map.get(key)
            if status is None:
                # 未检查过 → 视为健康
                healthy.append(cap)
            elif status.healthy:
                healthy.append(cap)

        # 全不健康 → 回退原表
        if not healthy:
            logger.warning("[ModelRouting] all models unhealthy, falling back to full table")
            return caps

        if len(healthy) < len(caps):
            healthy_keys = {c.model_id or c.model_name for c in healthy}
            unhealthy_ids = [
                cap.model_id or cap.model_name
                for cap in caps
                if (cap.model_id or cap.model_name) not in healthy_keys
            ]
            logger.info("[ModelRouting] filtered %d unhealthy models: %s", len(unhealthy_ids), unhealthy_ids)

        return healthy

    # ---- 内部 ---- #

    async def _check_model_safe(self, cap: ModelCapability) -> HealthStatus:
        """检查单个模型（含 max_rounds 重试），返回 HealthStatus。"""
        key = cap.model_id or cap.model_name
        now = time.monotonic()
        last_reason: str | None = None

        for round_idx in range(self._config.max_rounds):
            ok, reason = await self._execute_check(cap)
            if ok:
                return HealthStatus(
                    model_id=key, model_name=cap.model_name, model_type=cap.model_type,
                    healthy=True, last_check_time=now,
                    consecutive_successes=1, consecutive_failures=0,
                )
            last_reason = reason

        return HealthStatus(
            model_id=key, model_name=cap.model_name, model_type=cap.model_type,
            healthy=False, last_check_time=now,
            last_failure_reason=last_reason,
            consecutive_failures=1, consecutive_successes=0,
        )

    async def _execute_check(self, cap: ModelCapability) -> tuple[bool, str]:
        """执行一次 HTTP 请求，返回 (ok, reason)。"""
        try:
            import httpx
        except ImportError:
            # httpx 不可用 → 保守视为健康
            return True, "httpx not available"

        mcc = cap.model.model_client_config if cap.model is not None else None
        if mcc is None:
            return True, "no model instance"

        api_base = str(getattr(mcc, "api_base", "") or "")
        api_key = str(getattr(mcc, "api_key", "") or "")
        model_name = str(getattr(mcc, "model_name", "") or cap.model_name)

        if not api_base:
            return True, "no api_base"

        url = f"{api_base.rstrip('/')}/chat/completions"
        body = self._build_request_body(cap, model_name)
        headers = {
            "Content-Type": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            async with httpx.AsyncClient(timeout=self._config.timeout_seconds) as client:
                resp = await client.post(url, json=body, headers=headers)
                if resp.status_code != 200:
                    return False, f"HTTP {resp.status_code}"

                # 能力验证
                model_type = cap.model_type
                if model_type == "vision":
                    return self._verify_vision_response(resp.text)
                elif model_type == "audio":
                    return self._verify_audio_response(resp.text)
                else:
                    return True, "ok"
        except Exception as exc:
            return False, str(exc)

    def _build_request_body(self, cap: ModelCapability, model_name: str) -> dict:
        """构建 OpenAI 兼容请求体。"""
        model_type = cap.model_type
        if model_type == "vision":
            return {
                "model": model_name,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "主色调是什么？"},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": (
                                        f"data:image/png;base64,"
                                        f"{_RED_SQUARE_PNG_BASE64}"
                                    ),
                                },
                            },
                        ],
                    }
                ],
                "max_tokens": 50,
            }
        elif model_type == "audio":
            return {
                "model": model_name,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "音频说了什么？"},
                            {"type": "input_audio", "input_audio": {"data": _NIHAO_WAV_BASE64, "format": "wav"}},
                        ],
                    }
                ],
                "max_tokens": 50,
            }
        else:
            return {
                "model": model_name,
                "messages": [{"role": "user", "content": self._config.health_check_prompt}],
                "max_tokens": self._config.max_tokens,
            }

    @staticmethod
    def _verify_vision_response(text: str) -> tuple[bool, str]:
        """验证 vision 模型响应：回答必须匹配"红"/"red"等关键词。"""
        keywords = ["红", "red", "红色", "red color"]
        lower = text.lower()
        if any(kw in lower for kw in keywords):
            return True, "ok"
        return False, "vision verification failed: response does not mention red"

    @staticmethod
    def _verify_audio_response(text: str) -> tuple[bool, str]:
        """验证 audio 模型响应：回答必须匹配"你好"/"hello"等关键词。"""
        keywords = ["你好", "hello", "蜂鸣", "beep", "nihao"]
        lower = text.lower()
        if any(kw in lower for kw in keywords):
            return True, "ok"
        return False, "audio verification failed: response does not mention expected keywords"
