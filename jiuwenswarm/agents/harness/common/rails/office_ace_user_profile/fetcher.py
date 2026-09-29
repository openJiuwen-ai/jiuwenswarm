# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""UserProfileFetcher — 周期拉取云端用户画像概览，原子写本地缓存。

部署形态分流（OFFICE_ACE_DEPLOYMENT）：
* cloud：``GET {endpoint}/v1/core/internal/spaces/{space_id}/memory-user-profile/{actor_id}``
  （api-memory-access-internal.yaml），Bearer 凭据，actor_id=user_id，path 携带 space_id。
* pc：``GET {endpoint}/v1/appapi/memory/user-profile``
  （chat-service-app-api-memory.yaml），Bearer 凭据，用户身份经 ``X-Chat-User-Id`` 请求头传递。

两端响应体一致（``{revision, content, updated_at}``），无 ETag——每次返回最新画像或 404。

降级契约（对齐设计文档"任何失败均不阻断对话"）：
* 网络错误 / 5xx → 指数退避 ≤3 次，静默依赖本地缓存（可能略旧）
* 业务态（画像未生成 / 凭据无效 / 未开启画像）→ 静默不注入，不计失败
* 内容 >256KB → 拒绝写，用旧缓存或跳过
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# 设计文档约束：画像大小硬上限 256KB
_MAX_CONTENT_BYTES = 256 * 1024


@dataclass
class UserProfileConfig:
    """``memory.office_ace_user_profile`` 配置切片。"""

    enabled: bool
    endpoint: str
    api_key: str
    user_id: str
    fetch_interval_minutes: int = 10
    max_chars: int = 8000
    timeout_seconds: float = 15.0
    space_id: str = ""


def _default_cache_dir(user_id: str) -> Path:
    """``~/.office-claw/users/<userId>/``（与 relay-claw resolveUserConfigDir 对齐）。

    global root 取 ``OFFICE_CLAW_GLOBAL_CONFIG_ROOT``，否则 ``Path.home()``。
    userId 段经 ``urllib.parse.quote`` 编码（对齐 relay-claw encodeUserIdPathSegment）。
    无 userId 时回落到 ``~/.office-claw/`` 全局目录。
    """
    from urllib.parse import quote

    global_root = (os.getenv("OFFICE_CLAW_GLOBAL_CONFIG_ROOT") or "").strip()
    base = Path(global_root) if global_root else Path.home()
    office_claw_dir = base / ".office-claw"
    normalized_uid = (user_id or "").strip()
    if not normalized_uid:
        return office_claw_dir
    return office_claw_dir / "users" / quote(normalized_uid, safe="")


class UserProfileFetcher:
    """周期拉取云端用户画像概览，原子写本地缓存。

    缓存文件（默认）：
      * ``~/.office-claw/users/<userId>/user-profile.md`` — 概览正文

    新鲜度判定读缓存文件 mtime（距上次写 < fetch_interval 即新鲜），无需 meta 边车文件。

    线程安全：``read_cache`` 同步读，``fetch_once`` 异步写。后台周期任务
    与 ``before_model_call`` 的同步拉取可能并发写同一文件；原子写（tmp→rename）
    保证读者只会看到完整旧文件或完整新文件，不会读到半截内容。
    """

    def __init__(
        self,
        config: UserProfileConfig,
        cache_dir: Optional[Path] = None,
    ) -> None:
        self.config = config
        self._cache_dir = cache_dir or _default_cache_dir(config.user_id)

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    @property
    def cache_path(self) -> Path:
        return self._cache_dir / "user-profile.md"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_once(self) -> Optional[str]:
        """拉取一次。成功返回画像正文（字符串）；失败返回 None。

        失败含：网络错误、5xx、业务态错误、大小超限。
        任何失败都不抛异常（调用方零阻断），仅记日志。

        部署形态分流（OFFICE_ACE_DEPLOYMENT）：
          * cloud：GET /v1/core/internal/spaces/{space_id}/memory-user-profile/{actor_id}
            （api-memory-access-internal.yaml，path 带 space_id + actor_id=user_id）
          * pc：GET /v1/appapi/memory/user-profile
            （chat-service-app-api-memory.yaml，X-Chat-User-Id 头鉴权）
        """
        if not self._is_configured():
            return None

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Accept": "application/json",
        }
        # pc 端 appapi 用 X-Chat-User-Id 头传递用户身份；cloud 端 internal 用 path actor_id。
        if not self._is_cloud():
            headers["X-Chat-User-Id"] = self.config.user_id
            headers["Authorization"] = f"OfficeAceToken {self.config.api_key}"

        url = self._build_url()
        last_err: Optional[str] = None
        # 指数退避：1s, 2s, 4s（仅对网络/5xx 类错误重试）
        backoff = 1.0
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(
                    trust_env=False,
                    verify=False,
                    timeout=self.config.timeout_seconds,
                ) as client:
                    resp = await client.get(url, headers=headers)
                return self._handle_response(resp)
            except httpx.HTTPError as exc:
                last_err = f"network: {exc}"
                logger.debug(
                    "[UserProfileFetcher] fetch attempt %d failed: %s",
                    attempt + 1,
                    exc,
                )
            except Exception as exc:  # noqa: BLE001
                last_err = f"unexpected: {exc}"
                logger.debug(
                    "[UserProfileFetcher] fetch attempt %d error: %s",
                    attempt + 1,
                    exc,
                )
            if attempt < 2:
                await asyncio.sleep(backoff)
                backoff *= 2

        logger.info(
            "[UserProfileFetcher] fetch failed after retries (user=%s): %s",
            self.config.user_id,
            last_err,
        )
        return None

    def read_cache(self) -> Optional[str]:
        """同步读缓存文件。不存在/读失败返回 None。"""
        try:
            if not self.cache_path.exists():
                return None
            content = self.cache_path.read_text(encoding="utf-8")
            return content or None
        except OSError as exc:
            logger.debug(
                "[UserProfileFetcher] read cache failed (user=%s): %s",
                self.config.user_id,
                exc,
            )
            return None

    def is_cache_fresh(self) -> bool:
        """本地缓存是否新鲜（距上次写入 < fetch_interval）。

        读缓存文件 mtime（``cache_path.stat().st_mtime``）+ ``fetch_interval_minutes`` 判断。
        缓存文件不存在或 mtime 已过 fetch_interval → False。无需 meta 边车文件。
        """
        try:
            mtime = self.cache_path.stat().st_mtime
        except OSError as exc:
            logger.debug(
                "[UserProfileFetcher] stat cache failed (user=%s): %s",
                self.config.user_id,
                exc,
            )
            return False
        elapsed = time.time() - mtime
        return elapsed < self.config.fetch_interval_minutes * 60

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _is_configured(self) -> bool:
        # cloud 端 internal 接口还需 space_id（path 参数）
        if self._is_cloud():
            return bool(
                self.config.endpoint
                and self.config.api_key
                and self.config.user_id
                and self.config.space_id,
            )
        return bool(
            self.config.endpoint
            and self.config.api_key
            and self.config.user_id,
        )

    @staticmethod
    def _is_cloud() -> bool:
        """True iff OFFICE_ACE_DEPLOYMENT=cloud。"""
        return os.environ.get("OFFICE_ACE_DEPLOYMENT", "pc").strip().lower() == "cloud"

    def _build_url(self) -> str:
        base = self.config.endpoint.rstrip("/")
        if self._is_cloud():
            # api-memory-access-internal.yaml：
            # GET /v1/core/internal/spaces/{space_id}/memory-user-profile/{actor_id}
            actor_id = self.config.user_id
            return (
                f"{base}/v1/core/internal/spaces/{self.config.space_id}"
                f"/memory-user-profile/{actor_id}"
            )
        # chat-service-app-api-memory.yaml：GET /v1/appapi/memory/user-profile
        return f"{base}/v1/appapi/memory/user-profile"

    def _handle_response(
        self,
        resp: httpx.Response,
    ) -> Optional[str]:
        """处理 D2 响应。返回画像正文或 None。"""
        # 业务态错误码：画像不存在 / 未开启画像 / 凭据无效
        # 这些是正常业务状态，不计失败，静默不注入
        #   403 = 鉴权失败/未开启画像功能
        #   404 = 画像不存在
        if resp.status_code in (400, 401, 403, 404):
            logger.info(
                "[UserProfileFetcher] business status %d (user=%s): %s",
                resp.status_code,
                self.config.user_id,
                self._safe_body_snippet(resp),
            )
            return None

        if resp.status_code != 200:
            logger.info(
                "[UserProfileFetcher] unexpected status %d (user=%s)",
                resp.status_code,
                self.config.user_id,
            )
            return None

        # 200：解析响应体
        body = self._parse_overview_body(resp)
        if body is None:
            return None

        # 大小硬校验（256KB）
        body_bytes = body.encode("utf-8")
        if len(body_bytes) > _MAX_CONTENT_BYTES:
            logger.warning(
                "[UserProfileFetcher] profile too large: %d bytes > %d (user=%s)",
                len(body_bytes),
                _MAX_CONTENT_BYTES,
                self.config.user_id,
            )
            return None

        self._write_cache_atomic(body)
        logger.info(
            "[UserProfileFetcher] fetched ok (user=%s, %d chars)",
            self.config.user_id,
            len(body),
        )
        return body

    def _parse_overview_body(self, resp: httpx.Response) -> Optional[str]:
        """从 D2 响应解析画像正文。

        契约（chat-service-app-api-memory.yaml AppMemoryUserProfile）：
        ``{revision?, content, updated_at?}``
        ``content`` 字段为 markdown 正文。若服务端直接返回 text/plain（非 JSON），
        则整个响应体作为正文（兼容性兜底）。
        """
        try:
            data = resp.json()
        except ValueError:
            # 非 JSON：响应体作为 markdown 正文（兼容兜底）
            text = resp.text
            return text.strip() or None

        if not isinstance(data, dict):
            return None
        content = data.get("content")
        if not isinstance(content, str) or not content.strip():
            logger.info(
                "[UserProfileFetcher] empty/missing 'content' field (user=%s)",
                self.config.user_id,
            )
            return None
        return content

    def _write_cache_atomic(self, body: str) -> None:
        """tmp→rename 原子写 md。新鲜度由文件 mtime 判定，无需 meta 边车文件。"""
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            self._atomic_write_text(self.cache_path, body)
        except OSError as exc:
            logger.warning(
                "[UserProfileFetcher] write cache failed (user=%s): %s",
                self.config.user_id,
                exc,
            )

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        """tmp→rename 原子写，保证读者不会看到半截文件。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _safe_body_snippet(resp: httpx.Response) -> str:
        """取响应体前 200 字符用于日志（脱敏，不含凭据）。"""
        try:
            text = resp.text[:200]
            return text.replace("\n", " ")
        except Exception:  # noqa: BLE001
            return "<unreadable>"


__all__ = ["UserProfileConfig", "UserProfileFetcher"]
