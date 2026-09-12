# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""ModelRegistry — 管理由 skill 注册的主机模型信息.

设计见 docs/superpowers/specs/2026-08-15-model-management-design.md:

- 注册表是唯一数据源,持久化为 ``get_agent_root_dir()/model_registry.json``。
- 运行状态不落库:``list_with_status()`` 对每个条目并发健康检查实时合成
  ``status`` / ``latency_ms`` / ``last_checked_at``。
- 实例无内存状态(状态全在 JSON 文件),多实例可互换——interface 持有一个
  实例服务 RPC,harness 工具层经 ``get_default_registry()`` 取共享实例。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from jiuwenswarm.common.utils import get_agent_root_dir

logger = logging.getLogger(__name__)

_REGISTRY_FILENAME = "model_registry.json"
_HEALTH_CHECK_TIMEOUT_S = 2.0

MODEL_TYPES = frozenset({"llm", "vision", "audio", "video", "embedding", "rerank"})

# 注册条目中允许出现、且原样持久化的可选字段。
_OPTIONAL_STR_FIELDS = ("provider", "description", "param_size", "quantization")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def mask_api_key(api_key: str) -> str:
    """脱敏 API Key:保留前 3 后 4,其余以 **** 代替;过短则全部掩码。"""
    key = str(api_key or "")
    if len(key) <= 7:
        return "****"
    return f"{key[:3]}****{key[-4:]}"


class ModelRegistry:
    """主机模型注册表(JSON 文件持久化,按 name upsert)。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (get_agent_root_dir() / _REGISTRY_FILENAME)

    # ----- 持久化 -----

    def _read_entries(self) -> list[dict[str, Any]]:
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except FileNotFoundError:
            return []
        except Exception as exc:
            # 注册表损坏不抛错:记日志并按空表降级,避免拖垮管理页。
            logger.warning("[ModelRegistry] 注册表读取失败,按空表降级: %s (%s)", self._path, exc)
            return []
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            return []
        return [m for m in models if isinstance(m, dict) and m.get("name")]

    def _write_entries(self, entries: list[dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # tmp + replace 保证写入原子性,避免健康检查/注册并发时截断文件。
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=self._path.name, suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"models": entries}, f, ensure_ascii=False, indent=2)
            os.replace(tmp_name, self._path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # ----- 校验 -----

    @staticmethod
    def validate_entry(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """把调用方参数规整为注册条目;返回 (entry, error),error 非空即非法。"""
        name = str(params.get("name") or "").strip()
        if not name:
            return None, "缺少必填字段: name"
        api_base = str(params.get("api_base") or "").strip().rstrip("/")
        parsed = urlparse(api_base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None, f"api_base 不是合法的 http(s) URL: {api_base!r}"

        model_type = str(params.get("model_type") or "llm").strip().lower()
        if model_type not in MODEL_TYPES:
            return None, f"model_type 非法: {model_type!r},可选: {sorted(MODEL_TYPES)}"

        entry: dict[str, Any] = {
            "name": name,
            "api_base": api_base,
            "model_type": model_type,
            "protocol": str(params.get("protocol") or "openai").strip().lower() or "openai",
        }
        for field in _OPTIONAL_STR_FIELDS:
            value = str(params.get(field) or "").strip()
            if value:
                entry[field] = value
        api_key = str(params.get("api_key") or "").strip()
        if api_key:
            entry["api_key"] = api_key
        context_length = params.get("context_length")
        if context_length not in (None, ""):
            try:
                entry["context_length"] = int(context_length)
            except (TypeError, ValueError):
                return None, f"context_length 须为整数: {context_length!r}"
        capabilities = params.get("capabilities")
        if capabilities:
            if not isinstance(capabilities, (list, tuple)):
                return None, "capabilities 须为字符串数组"
            entry["capabilities"] = [str(c).strip() for c in capabilities if str(c).strip()]
        return entry, None

    # ----- 注册 / 注销 -----

    def _register(self, params: dict[str, Any]) -> dict[str, Any]:
        entry, error = self.validate_entry(params)
        if error is not None:
            return {"success": False, "error": error}
        entries = self._read_entries()
        now = _utc_now_iso()
        existing = next((e for e in entries if e.get("name") == entry["name"]), None)
        if existing is not None:
            entry["registered_at"] = existing.get("registered_at") or now
            entries = [entry if e.get("name") == entry["name"] else e for e in entries]
        else:
            entry["registered_at"] = now
            entries.append(entry)
        entry["updated_at"] = now
        self._write_entries(entries)
        logger.info("[ModelRegistry] 模型已注册: %s (%s)", entry["name"], entry["api_base"])
        return {"success": True, "model": self._public_entry(entry)}

    def _unregister(self, name: str) -> dict[str, Any]:
        target = str(name or "").strip()
        if not target:
            return {"success": False, "error": "缺少必填字段: name"}
        entries = self._read_entries()
        remaining = [e for e in entries if e.get("name") != target]
        if len(remaining) == len(entries):
            # 幂等:目标不存在不算错误,明确提示即可。
            return {"success": True, "detail": f"模型 {target} 不在注册表中,无需注销。"}
        self._write_entries(remaining)
        logger.info("[ModelRegistry] 模型已注销: %s", target)
        return {"success": True, "detail": f"模型 {target} 已注销。"}

    async def register(self, params: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._register, params)

    async def unregister(self, name: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._unregister, name)

    # ----- 查询(含实时健康检查) -----

    @staticmethod
    def _public_entry(entry: dict[str, Any]) -> dict[str, Any]:
        """对外视图:api_key 只返回掩码,原文不出 agent server。"""
        public = dict(entry)
        api_key = public.pop("api_key", None)
        if api_key:
            public["api_key_masked"] = mask_api_key(api_key)
        return public

    async def _check_health(self, client: httpx.AsyncClient, entry: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        status = "unknown"
        try:
            if entry.get("protocol", "openai") == "openai":
                headers: dict[str, str] = {}
                if entry.get("api_key"):
                    headers["Authorization"] = f"Bearer {entry['api_key']}"
                # 任意 HTTP 响应(含 401/404)都说明服务在线。
                await client.get(f"{entry['api_base']}/models", headers=headers)
                status = "running"
            else:
                parsed = urlparse(entry["api_base"])
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(parsed.hostname, port),
                    timeout=_HEALTH_CHECK_TIMEOUT_S,
                )
                writer.close()
                status = "running"
        except (TimeoutError, httpx.TimeoutException):
            # TimeoutError 是 OSError 子类,必须先于 OSError 捕获:超时按 unknown 计。
            status = "unknown"
        except (httpx.ConnectError, OSError):
            # 连接拒绝 / 主机不可达 / DNS 失败:服务确定不在线。
            status = "stopped"
        except Exception:
            status = "unknown"
        return {
            "status": status,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "last_checked_at": _utc_now_iso(),
        }

    async def list_with_status(self) -> list[dict[str, Any]]:
        entries = await asyncio.to_thread(self._read_entries)
        if not entries:
            return []
        # trust_env=False:健康检查目标是本机服务,绝不能走 HTTP_PROXY
        # (NO_PROXY 配置不规范时 localhost 请求会被代理挂起直到超时)。
        async with httpx.AsyncClient(timeout=_HEALTH_CHECK_TIMEOUT_S, trust_env=False) as client:
            checks = await asyncio.gather(
                *(self._check_health(client, e) for e in entries)
            )
        return [
            {**self._public_entry(entry), **check}
            for entry, check in zip(entries, checks)
        ]

    # ----- Web RPC handler -----

    async def handle_models_list(self, params: dict) -> dict:
        return {"models": await self.list_with_status()}


_default_registry: ModelRegistry | None = None


def get_default_registry() -> ModelRegistry:
    """进程内共享的默认注册表实例(供 harness 工具层使用)。"""
    global _default_registry
    if _default_registry is None:
        _default_registry = ModelRegistry()
    return _default_registry
