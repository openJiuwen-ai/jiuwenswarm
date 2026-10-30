# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Private OBS attachments and file deliveries; tenant directories belong to AgentServer."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlsplit

from minio import Minio
from minio.error import S3Error
from urllib3 import PoolManager, Timeout

MAX_FILE_BYTES = 50 * 1024 * 1024
DOWNLOAD_TTL_SECONDS = 7 * 24 * 60 * 60


def download_object_key(tenant_key: str, asset_id: str, filename: str) -> str:
    asset_id_ok = bool(re.fullmatch(r"[0-9a-f]{32}", asset_id))
    filename_ok = (
        bool(filename)
        and filename not in {".", ".."}
        and not any(char in filename for char in "/\\\0")
    )
    if not tenant_key or not asset_id_ok or not filename_ok:
        raise ValueError("invalid download object scope")
    scope = hashlib.sha256(tenant_key.encode()).hexdigest()
    return f"downloads/{scope}/{asset_id}/{filename}"


class _DigestReader:
    def __init__(self, source):
        self.source = source
        self.digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        data = self.source.read(size)
        self.digest.update(data)
        return data


def object_prefix(group_id: str, bot_id: str, user_id: str) -> str:
    if not all((group_id, bot_id, user_id)):
        raise ValueError("user_id, group_id and bot_id are required")
    scope = json.dumps([group_id, bot_id, user_id], ensure_ascii=False).encode()
    return f"uploads/{hashlib.sha256(scope).hexdigest()}/"


def _obs_http_client() -> PoolManager:
    """Share the OBS-only TLS policy between SDK and presigned-URL requests."""
    verify = os.getenv("JIUWENCLAW_SSL_VERIFY", "false").strip().lower() not in {
        "", "0", "false", "no", "off",
    }
    return PoolManager(
        timeout=Timeout(connect=5, read=60), retries=False,
        cert_reqs="CERT_REQUIRED" if verify else "CERT_NONE",
    )


@dataclass(frozen=True)
class AttachmentStore:
    client: Minio
    bucket: str
    origin: str

    @classmethod
    def from_env(cls) -> "AttachmentStore":
        endpoint = os.getenv("OBS_URL", "").strip()
        access = os.getenv("OBS_ACCESS_KEY", "").strip()
        secret = os.getenv("OBS_SECRET_KEY", "").strip()
        bucket = os.getenv("OBS_BUCKET", "jiuwenclaw").strip()
        if not all((endpoint, access, secret, bucket)):
            raise RuntimeError(
                "OBS_URL, OBS_ACCESS_KEY, OBS_SECRET_KEY and OBS_BUCKET are required"
            )
        secure = os.getenv("OBS_SECURE", "false").lower() in {"true", "1", "yes"}
        parsed = urlsplit(
            endpoint
            if "://" in endpoint
            else f"{'https' if secure else 'http'}://{endpoint}"
        )
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("OBS_URL must be an HTTP(S) origin")
        origin = f"{parsed.scheme}://{parsed.netloc}"
        client = Minio(
            parsed.netloc,
            access_key=access,
            secret_key=secret,
            secure=parsed.scheme == "https",
            region=os.getenv("OBS_REGION", "default"),
            http_client=_obs_http_client(),
        )
        return cls(client, bucket, origin)

    def _ensure_bucket(self) -> None:
        if self.client.bucket_exists(self.bucket):
            return
        try:
            self.client.make_bucket(self.bucket)
        except S3Error as exc:
            # Another Gateway/AgentServer may have created it after the check.
            if exc.code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                raise
            if not self.client.bucket_exists(self.bucket):
                raise

    def upload(self, data: bytes, filename: str, prefix: str) -> dict:
        if not data or len(data) > MAX_FILE_BYTES:
            raise ValueError("file must contain 1..50 MiB")
        key = f"{prefix}{uuid.uuid4().hex}/{filename}"
        self._ensure_bucket()
        self.client.put_object(self.bucket, key, BytesIO(data), len(data))
        try:
            url = self.client.presigned_get_object(
                self.bucket, key, expires=timedelta(minutes=10)
            )
        except BaseException:
            self.client.remove_object(self.bucket, key)
            raise
        return {
            "url": url,
            "name": filename,
            "size": len(data),
        }

    def upload_file(self, path: Path, key: str, *, session_id: str = "") -> dict:
        """Upload the workspace file directly; no local delivery copy is created."""
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("delivery source must be a regular file")
            size = info.st_size
            if size > MAX_FILE_BYTES:
                raise ValueError("file exceeds 50 MiB")
            checksum = hashlib.sha256()
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                checksum.update(chunk)
            digest = f"sha256:{checksum.hexdigest()}"
            source.seek(0)
            reader = _DigestReader(source)
            self._ensure_bucket()
            metadata = {"sha256": digest}
            if session_id:
                metadata["session-id"] = session_id
            result = self.client.put_object(
                self.bucket, key, reader, size, metadata=metadata
            )
            if f"sha256:{reader.digest.hexdigest()}" != digest:
                self.client.remove_object(self.bucket, key)
                raise ValueError("delivery source changed during upload")
            return {"size": size, "digest": digest, "etag": result.etag}

    def presign_download(self, key: str) -> str:
        return self.client.presigned_get_object(
            self.bucket, key, expires=timedelta(seconds=DOWNLOAD_TTL_SECONDS)
        )

    def download_key(self, url: str, tenant_key: str) -> str:
        """Restrict proxy URLs to this store and the selected tenant's deliveries."""
        parsed = urlsplit(url)
        prefix = f"/{self.bucket}/"
        origin_ok = f"{parsed.scheme}://{parsed.netloc}" == self.origin
        path_ok = parsed.path.startswith(prefix)
        identity_ok = not parsed.fragment and parsed.username is None
        if not origin_ok or not path_ok or not identity_ok:
            raise ValueError("download must use the configured object store")
        key = unquote(parsed.path[len(prefix):])
        parts = key.split("/")
        if len(parts) != 4 or key != download_object_key(tenant_key, parts[2], parts[3]):
            raise ValueError("download does not belong to the current tenant")
        return key

    def _upload_key(self, url: str, prefix: str) -> str:
        parsed = urlsplit(url)
        if f"{parsed.scheme}://{parsed.netloc}" != self.origin or parsed.fragment:
            raise ValueError("attachment URL must use the configured OBS origin")
        path_prefix = f"/{self.bucket}/"
        if not parsed.path.startswith(path_prefix):
            raise ValueError("attachment URL must use the configured bucket")
        key = unquote(parsed.path[len(path_prefix):])
        suffix = key.removeprefix(prefix).split("/")
        name = suffix[1] if len(suffix) == 2 else ""
        uuid_ok = bool(re.fullmatch(r"[0-9a-f]{32}", suffix[0])) if len(suffix) == 2 else False
        name_ok = (
            bool(name)
            and name not in {".", ".."}
            and not any(char in name for char in "\\\0")
        )
        if not key.startswith(prefix) or not uuid_ok or not name_ok:
            raise ValueError("attachment does not belong to the current context")
        return key

    def remove_upload(self, url: str, prefix: str) -> None:
        """Delete only a temporary upload belonging to the authenticated scope."""
        self.client.remove_object(self.bucket, self._upload_key(url, prefix))

    def read(self, url: str, prefix: str) -> bytes:
        self._upload_key(url, prefix)
        return self._read_signed_url(url)

    def read_download(self, url: str, tenant_key: str, session_id: str = "") -> bytes:
        self.download_key(url, tenant_key)
        return self._read_signed_url(url, session_id=session_id)

    @staticmethod
    def _read_signed_url(url: str, *, session_id: str = "") -> bytes:
        # Use the signed URL so its expiry/signature is enforced by OBS. Never
        # follow redirects or reinterpret the URL as an unrestricted SDK key.
        transport = _obs_http_client()
        try:
            response = transport.request(
                "GET", url, redirect=False, preload_content=False
            )
            try:
                if response.status != 200:
                    raise ValueError("attachment URL rejected by OBS")
                if session_id and response.headers.get("x-amz-meta-session-id") != session_id:
                    raise ValueError("download session mismatch")
                data = response.read(MAX_FILE_BYTES + 1)
                if not data or len(data) > MAX_FILE_BYTES:
                    raise ValueError("file must contain 1..50 MiB")
                return data
            finally:
                try:
                    response.close()
                finally:
                    response.release_conn()
        finally:
            transport.clear()
