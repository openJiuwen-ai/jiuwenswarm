# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Web File Download Token Manager

提供基于 HMAC 签名的文件下载令牌生成与验证，支持跨进程（AgentServer / Gateway / app_web.py）
无需共享内存即可安全校验。

协议：
- 令牌格式: Base64URL(payload_json) + "." + Hex(HMAC-SHA256)
- 普通下载 payload: path, sid；可选 exp（省略则不过期，用于 send_file_to_user 交付产物）
- Skill 正文图片 / 上传等短期令牌仍携带 exp
- Skill 正文图片 payload: purpose=skill_content_image, name, version, relative_path, exp, sid（无 path）
- 受管资产令牌额外绑定: asset_id, size, digest, name
- 密钥来源: 环境变量 JIUWENSWARM_FILE_DOWNLOAD_SECRET 或自动生成并写入共享文件
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

if TYPE_CHECKING:
    from jiuwenswarm.agents.harness.common.tools.verified_download_assets import (
        VerifiedDownloadAsset,
        VerifiedDownloadAssetOwner,
    )

logger = logging.getLogger(__name__)

_DEFAULT_EXPIRES_SECONDS = 600
_SECRET_ENV_KEY = "JIUWENSWARM_FILE_DOWNLOAD_SECRET"
_SECRET_FILE_NAME = ".file_download_secret"
_DOWNLOAD_HTTP_BASE_ENV_KEY = "JIUWENSWARM_AGENT_DOWNLOAD_HTTP_BASE"
_UPLOAD_HTTP_BASE_ENV_KEY = "JIUWENSWARM_AGENT_UPLOAD_HTTP_BASE"
_LEGACY_HTTP_BASE_ENV_KEY = "JIUWENSWARM_AGENT_HTTP_BASE"
_VERIFIED_ASSET_TOKEN_KIND = "verified_asset_v1"
_LEGACY_TOKEN_KEYS = frozenset({"path", "sid"})
_ROUTED_DOWNLOAD_TOKEN_KEYS = frozenset(
    {"path", "sid", "download_http_base"}
)
PURPOSE_SKILL_CONTENT_IMAGE = "skill_content_image"


def _get_secret_file_path() -> Path:
    workspace = os.getenv("JIUWENSWARM_WORKSPACE")
    if workspace:
        return Path(workspace) / "config" / _SECRET_FILE_NAME
    return Path.home() / ".jiuwenswarm" / "config" / _SECRET_FILE_NAME


def _load_or_create_secret() -> str:
    secret = os.getenv(_SECRET_ENV_KEY)
    if secret and len(secret) >= 32:
        return secret

    secret_file = _get_secret_file_path()
    try:
        if secret_file.exists():
            existing = secret_file.read_text(encoding="utf-8").strip()
            if existing and len(existing) >= 32:
                return existing
    except Exception:
        logger.debug("[WebFileDownload] 读取密钥文件失败，将重新生成")

    new_secret = secrets.token_hex(32)
    try:
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(new_secret, encoding="utf-8")
        os.chmod(secret_file, 0o600)
    except Exception:
        logger.warning("[WebFileDownload] 写入密钥文件失败，使用内存密钥（重启后失效）")

    return new_secret


class WebFileDownloadManager:
    """管理 Web 端文件下载令牌的生成与验证。

    使用 HMAC-SHA256 签名保证令牌不可伪造，
    密钥通过环境变量或共享文件在 AgentServer / Gateway / app_web.py 间共享。
    """

    _instance: WebFileDownloadManager | None = None

    def __init__(
        self,
        secret: str | None = None,
        *,
        asset_owner: VerifiedDownloadAssetOwner | None = None,
    ) -> None:
        self._secret = secret or _load_or_create_secret()
        self._asset_owner = asset_owner

    @classmethod
    def get_instance(cls) -> WebFileDownloadManager:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    def _sign_payload(self, payload: dict[str, Any]) -> str:
        payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        payload_b64 = base64.urlsafe_b64encode(payload_json.encode("utf-8")).decode("ascii")
        signature = hmac.new(self._secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{payload_b64}.{signature}"

    def generate_verified_asset_token(
        self,
        asset: VerifiedDownloadAsset,
        *,
        file_name: str,
        session_id: str = "",
    ) -> str:
        """Generate a token bound to one durable verified asset registration."""

        payload = {
            "kind": _VERIFIED_ASSET_TOKEN_KIND,
            "asset_id": asset.asset_id,
            "path": asset.sealed_path.as_posix(),
            "exp": asset.expires_at,
            "size": asset.size_bytes,
            "digest": asset.content_digest,
            "name": Path(file_name).name,
            "sid": session_id,
        }
        return self._sign_payload(payload)

    def generate_token(
        self,
        file_path: str,
        session_id: str = "",
        expires_in: int | None = None,
        *,
        agent_http_base: str | None = None,
        agent_http_base_key: str = "",
    ) -> str:
        payload: dict[str, Any] = {
            "path": file_path,
            "sid": session_id,
        }
        # expires_in=None：不写入 exp，校验侧视为不过期（send_file 交付产物）。
        if expires_in is not None:
            payload["exp"] = int(time.time()) + int(expires_in)
        # AgentOS 下 token 由目标 AgentServer 签发。将部署注入的、可访问该
        # sandbox 的 HTTP bridge 基址随 token 返回，使独立运行的 Web 静态进程
        # 不必持有用户态目录或 AgentOS Router 对象也能代理到正确用户。
        base = str(agent_http_base or "").strip()
        if base and agent_http_base_key:
            payload[agent_http_base_key] = base.rstrip("/")
        return self._sign_payload(payload)

    def generate_skill_content_image_token(
        self, *, name: str, version: str | None, relative_path: str,
        session_id: str, expires_in: int = _DEFAULT_EXPIRES_SECONDS,
    ) -> str:
        sid = str(session_id or "").strip()
        skill_name = str(name or "").strip()
        rel = str(relative_path or "").strip().replace("\\", "/")
        if not sid or not skill_name or not rel:
            raise ValueError("skill_content_image token requires session_id, name and relative_path")
        return self._sign_payload({
            "purpose": PURPOSE_SKILL_CONTENT_IMAGE, "name": skill_name,
            "version": version if version is None else str(version).strip() or None,
            "relative_path": rel, "exp": int(time.time()) + expires_in, "sid": sid,
        })

    def validate_token(self, token: str, *, session_id: str | None = None,
                       check_expiry: bool = True) -> dict[str, Any] | None:
        try:
            parts = token.split(".")
            if len(parts) != 2:
                return None
            payload_b64, signature = parts
            expected_sig = hmac.new(
                self._secret.encode("utf-8"),
                payload_b64.encode("ascii"),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(signature, expected_sig):
                logger.warning("[WebFileDownload] 令牌签名校验失败")
                return None
            payload_json = base64.urlsafe_b64decode(payload_b64).decode("utf-8")
            payload = json.loads(payload_json)
            if not isinstance(payload, dict):
                return None
            if check_expiry:
                # 兼容无 exp 字段的交付令牌：不强制过期；有 exp 则严格校验。
                exp = payload.get("exp")
                if exp is None and not _is_legacy_no_expiration_payload(payload):
                    return None
                if exp is not None and (
                    not isinstance(exp, (int, float)) or int(exp) < int(time.time())
                ):
                    logger.warning("[WebFileDownload] 令牌已过期")
                    return None
            if session_id is not None and str(session_id).strip():
                if str(payload.get("sid") or "").strip() != str(session_id).strip():
                    return None
            token_kind = payload.get("kind")
            if token_kind not in (None, _VERIFIED_ASSET_TOKEN_KIND):
                return None
            if token_kind == _VERIFIED_ASSET_TOKEN_KIND:
                expires_at = float(payload.get("exp"))
                owner = self._asset_owner
                if owner is None:
                    from jiuwenswarm.agents.harness.common.tools.verified_download_assets import (
                        get_verified_download_asset_owner,
                    )

                    owner = get_verified_download_asset_owner()
                if not owner.is_active(
                    asset_id=str(payload.get("asset_id") or ""),
                    sealed_path=str(payload.get("path") or ""),
                    expires_at=expires_at,
                    size_bytes=int(payload.get("size")),
                    content_digest=str(payload.get("digest") or ""),
                ):
                    return None
            return payload
        except Exception:
            logger.debug("[WebFileDownload] 令牌解析异常", exc_info=True)
            return None

    @staticmethod
    def generate_download_url(token: str, user_id: str = "") -> str:
        query: dict[str, str] = {"token": token}
        normalized_user_id = str(user_id or "").strip()
        if normalized_user_id:
            query["user_id"] = normalized_user_id
        return f"/file-api/download?{urlencode(query)}"


def _is_legacy_no_expiration_payload(payload: dict[str, Any]) -> bool:
    """Accept only the two exact signed download schemas allowed without ``exp``."""

    keys = set(payload)
    if keys not in {_LEGACY_TOKEN_KEYS, _ROUTED_DOWNLOAD_TOKEN_KEYS}:
        return False
    return all(isinstance(payload[key], str) and payload[key] for key in keys)


def generate_file_download_token(
    file_path: str,
    session_id: str = "",
    expires_in: int | None = None,
) -> str:
    """签发文件下载令牌。

    ``expires_in=None``（默认）表示不过期，供 ``send_file_to_user`` 等持久交付使用；
    需要短期有效时显式传入秒数。
    """
    return WebFileDownloadManager.get_instance().generate_token(
        file_path,
        session_id,
        expires_in,
        agent_http_base=(
            os.getenv(_DOWNLOAD_HTTP_BASE_ENV_KEY)
            or os.getenv(_LEGACY_HTTP_BASE_ENV_KEY)
        ),
        agent_http_base_key="download_http_base",
    )


def generate_skill_content_image_token(
    *, name: str, version: str | None, relative_path: str, session_id: str,
    expires_in: int = _DEFAULT_EXPIRES_SECONDS,
) -> str:
    return WebFileDownloadManager.get_instance().generate_skill_content_image_token(
        name=name, version=version, relative_path=relative_path,
        session_id=session_id, expires_in=expires_in,
    )


def _resolve_user_dirs() -> list[Path]:
    """返回用户态业务目录根（注入目录 workspace / sessions / agent workspace + 项目目录）。

    项目目录（``project_store``）可位于注入目录之外（如 code 模式下用户自选的工作目录），
    ``send_file`` 等工具会把项目目录内的文件作为下载目标；若仅按注入目录三个根校验，
    会误拒绝这些合法下载。故把已登记项目（含隐藏）的 ``project_dir`` 一并纳入边界。
    """
    from jiuwenswarm.common.utils import (
        get_agent_sessions_dir,
        get_agent_workspace_dir,
        get_user_workspace_dir,
    )

    roots: list[Path] = []
    for factory in (get_user_workspace_dir, get_agent_sessions_dir, get_agent_workspace_dir):
        try:
            root = Path(factory()).resolve(strict=False)
        except Exception:  # noqa: BLE001
            continue
        roots.append(root)

    # 已登记项目目录同样属于合法下载边界。project_store 依赖注入目录内的
    # projects.json，读盘失败/尚未初始化时仅影响项目目录回退，不影响上面的三根。
    try:
        from jiuwenswarm.server.runtime.session import project_store

        for project in project_store.list_projects(include_hidden=True):
            project_dir = str(project.project_dir or "").strip()
            if not project_dir:
                continue
            try:
                roots.append(Path(project_dir).resolve(strict=False))
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return roots


def is_path_within_user_dirs(path_str: str) -> bool:
    """判断路径是否位于用户态业务目录内（Phase 2 下载/上传边界校验）。

    下载与上传端点只允许访问注入目录内的用户数据；越界路径一律拒绝。
    """
    if not path_str or not str(path_str).strip():
        return False
    try:
        candidate = Path(str(path_str)).resolve(strict=False)
    except OSError:
        return False
    for root in _resolve_user_dirs():
        if candidate == root or root in candidate.parents:
            return True
    return False


def generate_file_upload_token(
    target_rel_path: str,
    session_id: str = "",
    expires_in: int = _DEFAULT_EXPIRES_SECONDS,
) -> str:
    """生成文件上传令牌（Phase 2 HTTP bridge 上传端点）。

    payload 的 ``path`` 为**相对用户目录根**（``get_user_workspace_dir()``）
    的目标路径（如 ``agent/workspace/upload.txt`` / ``agent/sessions/<sid>/uploads/x.png``）；
    上传端点校验令牌后按相对路径落盘注入目录，并做目录边界校验。
    """
    return WebFileDownloadManager.get_instance().generate_token(
        str(target_rel_path),
        session_id,
        expires_in,
        # Uploads use a dedicated listener (normally WS port + 1).  The
        # download/WS base must never be embedded as an upload target.
        agent_http_base=os.getenv(_UPLOAD_HTTP_BASE_ENV_KEY),
        agent_http_base_key="upload_http_base",
    )


def validate_file_download_token(
    token: str, *, session_id: str | None = None, check_expiry: bool = True,
) -> dict[str, Any] | None:
    return WebFileDownloadManager.get_instance().validate_token(
        token, session_id=session_id, check_expiry=check_expiry,
    )


def build_file_download_info(
    file_path: str,
    file_name: str,
    session_id: str = "",
    expires_in: int | None = None,
    user_id: str = "",
) -> dict[str, Any]:
    """构建可投递的文件下载信息。

    默认签发不过期令牌（``expires_in=None``），与 ``send_file_to_user`` 产物语义一致。
    """
    token = generate_file_download_token(file_path, session_id, expires_in)
    download_url = WebFileDownloadManager.get_instance().generate_download_url(token, user_id)

    file_size = 0
    mime_type = "application/octet-stream"
    try:
        file_size = os.path.getsize(file_path)
    except OSError:
        pass

    import mimetypes

    guessed_type, _ = mimetypes.guess_type(file_name)
    if guessed_type:
        mime_type = guessed_type

    return {
        "name": file_name,
        "size": file_size,
        "mime_type": mime_type,
        "download_url": download_url,
        "download_token": token,
    }


def build_verified_asset_download_info(
    asset: VerifiedDownloadAsset,
    file_name: str,
    session_id: str = "",
    user_id: str = "",
) -> dict[str, Any]:
    """Build download metadata whose token is backed by a staged asset."""

    manager = WebFileDownloadManager.get_instance()
    token = manager.generate_verified_asset_token(
        asset,
        file_name=file_name,
        session_id=session_id,
    )
    mime_type = "application/octet-stream"

    import mimetypes

    guessed_type, _ = mimetypes.guess_type(file_name)
    if guessed_type:
        mime_type = guessed_type

    return {
        "name": Path(file_name).name,
        "size": asset.size_bytes,
        "mime_type": mime_type,
        "download_url": manager.generate_download_url(token, user_id),
        "download_token": token,
    }
