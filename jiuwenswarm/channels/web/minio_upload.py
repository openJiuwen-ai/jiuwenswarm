# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""MinIO (S3-compatible) upload helpers for web chat attachments."""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse


def _max_upload_filename_bytes() -> int:
    """Return OS filename component limit."""
    # Windows: NTFS 通常支持 255 个字符
    if sys.platform == "win32":
        return 255
    
    try:
        # Unix/Linux/macOS
        return int(os.pathconf(tempfile.gettempdir(), "PC_NAME_MAX"))
    except (OSError, ValueError, AttributeError):
        return 255


def _validate_upload_filename(filename: str) -> None:
    max_bytes = _max_upload_filename_bytes()
    name_bytes = filename.encode("utf-8")
    if len(name_bytes) > max_bytes:
        raise ValueError(
            f"文件名过长（当前 {len(name_bytes)} 字节），最大长度为 {max_bytes} 字节，请缩短文件名后重试"
        )


@dataclass
class MinioUploadConfig:
    """Self-hosted MinIO endpoint and bucket settings."""

    endpoint: str
    access_key: str
    secret_key: str
    bucket: str
    secure: bool = False
    public_base_url: str = ""
    region: str = "default"


def _parse_endpoint(raw: str) -> tuple[str, bool | None]:
    """Return host:port and optional secure flag inferred from URL scheme."""
    value = raw.strip()
    if not value:
        raise ValueError("empty endpoint")
    if "://" not in value:
        return value, None
    parsed = urlparse(value)
    host = parsed.netloc or parsed.path
    if not host:
        raise ValueError(f"invalid endpoint: {raw}")
    return host, parsed.scheme == "https"


def _parse_bool(value: str | bool | None, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def load_minio_upload_config() -> MinioUploadConfig:
    """Load object-store settings from ``OBS_*`` env or ``config.yaml`` ``minio``.

    Deploy contract is ``deploy/enterprise/.env.example`` ``OBS_*``
    (``OBS_URL`` / ``OBS_ACCESS_KEY`` / ``OBS_SECRET_KEY`` / …).
    """
    endpoint = os.environ.get("OBS_URL", "").strip()
    access_key = os.environ.get("OBS_ACCESS_KEY", "").strip()
    secret_key = os.environ.get("OBS_SECRET_KEY", "").strip()
    bucket = os.environ.get("OBS_BUCKET", "").strip()
    public_base_url = os.environ.get("OBS_PUBLIC_BASE_URL", "").strip()
    secure_env = os.environ.get("OBS_SECURE", "").strip()
    region = os.environ.get("OBS_REGION", "").strip()

    yaml_secure: bool | None = None
    if not all((endpoint, access_key, secret_key, bucket)):
        from jiuwenswarm.common.config import get_config

        mc = get_config().get("minio") or {}
        endpoint = endpoint or str(mc.get("endpoint") or "").strip()
        access_key = access_key or str(mc.get("access_key") or "").strip()
        secret_key = secret_key or str(mc.get("secret_key") or "").strip()
        bucket = bucket or str(mc.get("bucket") or "jiuwenclaw").strip()
        if not public_base_url:
            public_base_url = str(mc.get("public_base_url") or "").strip()
        if mc.get("secure") is not None:
            yaml_secure = _parse_bool(mc.get("secure"))

    if not endpoint or not access_key or not secret_key:
        raise RuntimeError(
            "Object storage config missing: set OBS_URL / OBS_ACCESS_KEY / "
            "OBS_SECRET_KEY (optional OBS_BUCKET / OBS_SECURE / OBS_REGION / "
            "OBS_PUBLIC_BASE_URL), or configure minio.* in config.yaml"
        )

    host, secure_from_url = _parse_endpoint(endpoint)
    secure = secure_from_url if secure_from_url is not None else False
    if secure_env:
        secure = _parse_bool(secure_env, secure)
    elif yaml_secure is not None:
        secure = yaml_secure

    return MinioUploadConfig(
        endpoint=host,
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket or "jiuwenclaw",
        secure=secure,
        public_base_url=public_base_url,
        region=region or "default",
    )


def upload_local_file_to_minio(
    config: MinioUploadConfig,
    file_path: str,
    *,
    filename: str | None = None,
    presign_days: int = 7,
    object_prefix: str = "uploads",
) -> dict[str, str | int]:
    """Upload a local file to MinIO and return url/name/size.

    ``object_prefix`` defaults to ``uploads`` (browser upload-obs). Enterprise
    Agent→user download uses ``downloads``.
    """
    try:
        from minio import Minio
    except ImportError as exc:
        raise RuntimeError("minio package not installed; run: pip install minio") from exc

    display_name = filename or os.path.basename(file_path)
    safe_name = display_name.replace("\\", "_").replace("/", "_")
    prefix = (object_prefix or "uploads").strip().strip("/") or "uploads"
    object_name = f"{prefix}/{uuid.uuid4().hex}_{safe_name}"

    client = Minio(
        config.endpoint,
        access_key=config.access_key,
        secret_key=config.secret_key,
        secure=config.secure,
        region=config.region,
    )
    if not client.bucket_exists(config.bucket):
        client.make_bucket(config.bucket)

    client.fput_object(config.bucket, object_name, file_path)
    file_size = os.path.getsize(file_path)

    if config.public_base_url:
        base = config.public_base_url.rstrip("/")
        url = f"{base}/{config.bucket}/{object_name}"
    else:
        url = client.presigned_get_object(
            config.bucket,
            object_name,
            expires=timedelta(days=presign_days),
        )

    return {"url": url, "name": display_name, "size": file_size}


def upload_base64_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Upload a browser file payload (filename + content_base64) to MinIO."""
    filename = str(payload.get("filename") or "upload.bin")
    _validate_upload_filename(filename)
    content_b64 = payload.get("content_base64")
    if not isinstance(content_b64, str) or not content_b64:
        raise RuntimeError("missing content_base64")
    content = base64.b64decode(content_b64)
    fd, path = tempfile.mkstemp(prefix="minio_upload_", suffix=".bin")
    os.close(fd)
    try:
        with open(path, "wb") as handle:
            handle.write(content)
        cfg = load_minio_upload_config()
        uploaded = upload_local_file_to_minio(cfg, path, filename=filename)
        return {"ok": True, **uploaded}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
