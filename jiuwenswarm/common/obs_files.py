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
OBJECT_ASSET_KIND = "obs_asset_v1"


def download_object_key(tenant_key: str, asset_id: str, filename: str) -> str:
    if (
        not tenant_key
        or not re.fullmatch(r"[0-9a-f]{32}", asset_id)
        or not filename
        or filename in {".", ".."}
        or any(char in filename for char in "/\\\0")
    ):
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
            http_client=PoolManager(timeout=Timeout(connect=5, read=60), retries=False),
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

    def upload_file(self, path: Path, key: str) -> dict:
        """Upload the workspace file directly; no local delivery copy is created."""
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("delivery source must be a regular file")
            size = info.st_size
            if size > MAX_FILE_BYTES:
                raise ValueError("file exceeds 50 MiB")
            checksum = hashlib.sha256()
            while chunk := source.read(1024 * 1024):
                checksum.update(chunk)
            digest = f"sha256:{checksum.hexdigest()}"
            source.seek(0)
            reader = _DigestReader(source)
            self._ensure_bucket()
            result = self.client.put_object(
                self.bucket, key, reader, size, metadata={"sha256": digest}
            )
            if f"sha256:{reader.digest.hexdigest()}" != digest:
                self.client.remove_object(self.bucket, key)
                raise ValueError("delivery source changed during upload")
            return {"size": size, "digest": digest, "etag": result.etag}

    def download_file(self, payload: dict, destination: Path) -> None:
        """Materialize a signed delivery for consumers such as Skill-package import."""
        if payload["bucket"] != self.bucket:
            raise ValueError("download bucket mismatch")
        response = self.client.get_object(
            self.bucket,
            payload["object_key"],
            request_headers={"If-Match": f'"{payload["etag"]}"'},
        )
        digest, total = hashlib.sha256(), 0
        created = False
        try:
            with destination.open("xb") as output:
                created = True
                while chunk := response.read(65536):
                    total += len(chunk)
                    if total > payload["size"]:
                        raise ValueError("download size mismatch")
                    digest.update(chunk)
                    output.write(chunk)
            if (
                total != payload["size"]
                or f"sha256:{digest.hexdigest()}" != payload["digest"]
            ):
                raise ValueError("download digest mismatch")
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise
        finally:
            try:
                response.close()
            finally:
                response.release_conn()

    def _upload_key(self, url: str, prefix: str) -> str:
        parsed = urlsplit(url)
        if f"{parsed.scheme}://{parsed.netloc}" != self.origin or parsed.fragment:
            raise ValueError("attachment URL must use the configured OBS origin")
        path_prefix = f"/{self.bucket}/"
        if not parsed.path.startswith(path_prefix):
            raise ValueError("attachment URL must use the configured bucket")
        key = unquote(parsed.path[len(path_prefix) :])
        suffix = key.removeprefix(prefix).split("/")
        if (
            not key.startswith(prefix)
            or len(suffix) != 2
            or not re.fullmatch(r"[0-9a-f]{32}", suffix[0])
            or not suffix[1]
            or suffix[1] in {".", ".."}
            or any(char in suffix[1] for char in "\\\0")
        ):
            raise ValueError("attachment does not belong to the current context")
        return key

    def remove_upload(self, url: str, prefix: str) -> None:
        """Delete only a temporary upload belonging to the authenticated scope."""
        self.client.remove_object(self.bucket, self._upload_key(url, prefix))

    def read(self, url: str, prefix: str) -> bytes:
        self._upload_key(url, prefix)
        # Use the signed URL so its expiry/signature is enforced by OBS. Never
        # follow redirects or reinterpret the URL as an unrestricted SDK key.
        transport = PoolManager(timeout=Timeout(connect=5, read=60), retries=False)
        try:
            response = transport.request(
                "GET", url, redirect=False, preload_content=False
            )
            try:
                if response.status != 200:
                    raise ValueError("attachment URL rejected by OBS")
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
