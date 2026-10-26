from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha1
import os
from pathlib import Path
import tempfile
from typing import Dict, Sequence
from urllib.parse import urlparse

_SUPPORTED_SCHEMES = {"s3", "obs"}
_DEFAULT_DOWNLOAD_NAMES = (
    "manifest.json",
    "tree_index.yaml",
    "catalog.jsonl",
    "tree_index.html",
)


@dataclass(frozen=True)
class S3Location:
    scheme: str
    bucket: str
    key: str

    @property
    def uri(self) -> str:
        if self.key:
            return f"{self.scheme}://{self.bucket}/{self.key}"
        return f"{self.scheme}://{self.bucket}"


def is_s3_uri(value: str | Path) -> bool:
    parsed = urlparse(str(value).strip())
    return parsed.scheme.lower() in _SUPPORTED_SCHEMES


def parse_s3_uri(uri: str, *, require_key: bool = False) -> S3Location:
    parsed = urlparse(str(uri).strip())
    scheme = str(parsed.scheme or "").strip().lower()
    bucket = str(parsed.netloc or "").strip()
    key = str(parsed.path or "").lstrip("/").strip()
    invalid_location = scheme not in _SUPPORTED_SCHEMES or not bucket
    missing_required_key = require_key and not key
    if invalid_location or missing_required_key:
        raise ValueError(f"Invalid S3 URI: {uri}")
    return S3Location(scheme=scheme, bucket=bucket, key=key)


def join_s3_uri(base_uri: str, *parts: str) -> str:
    location = parse_s3_uri(base_uri)
    suffix = "/".join(str(part or "").strip("/").strip() for part in parts if str(part or "").strip("/").strip())
    key = str(location.key or "").strip("/")
    if key and suffix:
        final_key = f"{key}/{suffix}"
    elif suffix:
        final_key = suffix
    else:
        final_key = key
    return S3Location(scheme=location.scheme, bucket=location.bucket, key=final_key).uri


def read_s3_bytes(uri: str) -> bytes:
    location = parse_s3_uri(uri, require_key=True)
    client = create_s3_client()
    response = client.get_object(Bucket=location.bucket, Key=location.key)
    body = response.get("Body")
    if body is None:
        raise RuntimeError(f"S3 object body is empty: {uri}")
    try:
        return body.read()
    finally:
        with suppress(Exception):
            body.close()


def read_s3_text(uri: str, *, encoding: str = "utf-8") -> str:
    return read_s3_bytes(uri).decode(encoding)


def download_s3_object_to_path(uri: str, destination_path: str | Path) -> Path:
    target = Path(destination_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(read_s3_bytes(uri))
    return target


def download_s3_relative_object_if_exists(*, base_uri: str, relative_path: str, destination_path: str | Path) -> bool:
    try:
        download_s3_object_to_path(join_s3_uri(base_uri, relative_path), destination_path)
        return True
    except Exception as exc:
        if _is_missing_object_error(exc):
            return False
        raise


def upload_s3_bytes(uri: str, payload: bytes) -> None:
    location = parse_s3_uri(uri, require_key=True)
    client = create_s3_client()
    client.put_object(Bucket=location.bucket, Key=location.key, Body=payload)


def upload_local_dir_to_s3(local_dir: str | Path, base_uri: str) -> None:
    directory = Path(local_dir)
    if not directory.exists():
        raise FileNotFoundError(directory)
    if not directory.is_dir():
        raise NotADirectoryError(directory)

    primary_signing = _resolve_payload_signing_enabled_from_env()
    primary_addressing = _resolve_addressing_style_from_env()
    alternative_addressing = "path" if primary_addressing == "virtual" else "virtual"
    clients: Dict[tuple[bool, str], object] = {}

    def get_client(signing: bool, style: str):
        key = (signing, style)
        if key not in clients:
            clients[key] = create_s3_client(payload_signing_enabled=signing, addressing_style=style)
        return clients[key]

    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        relative = str(path.relative_to(directory)).replace("\\", "/")
        payload = path.read_bytes()
        destination_uri = join_s3_uri(base_uri, relative)
        location = parse_s3_uri(destination_uri, require_key=True)
        attempts = (
            (primary_signing, primary_addressing),
            (not primary_signing, primary_addressing),
            (primary_signing, alternative_addressing),
            (not primary_signing, alternative_addressing),
        )
        last_error: Exception | None = None
        for signing, style in attempts:
            client = get_client(signing, style)
            try:
                client.put_object(Bucket=location.bucket, Key=location.key, Body=payload)
                last_error = None
                break
            except Exception as exc:
                if not _is_retryable_put_error(exc):
                    raise
                last_error = exc
        if last_error is not None:
            raise last_error


def materialize_s3_dir(
    base_uri: str,
    *,
    relative_paths: Sequence[str] | None = None,
    cache_namespace: str = "s3-dir-cache",
) -> Path:
    normalized_base = str(base_uri).rstrip("/")
    cache_root = Path(tempfile.gettempdir()) / cache_namespace
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_key = sha1(normalized_base.encode("utf-8")).hexdigest()[:16]
    local_dir = cache_root / cache_key
    local_dir.mkdir(parents=True, exist_ok=True)

    names = list(relative_paths or _DEFAULT_DOWNLOAD_NAMES)
    if "manifest.json" not in names:
        names = ["manifest.json", *names]
    for relative in names:
        download_s3_relative_object_if_exists(
            base_uri=normalized_base,
            relative_path=relative,
            destination_path=local_dir / relative,
        )
    manifest_path = local_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Index manifest not found under {normalized_base}")
    return local_dir


def create_s3_client(
    *,
    payload_signing_enabled: bool | None = None,
    addressing_style: str | None = None,
):
    try:
        import boto3
        from botocore.config import Config
    except Exception as exc:
        raise RuntimeError("boto3/botocore is required for S3 storage access") from exc

    endpoint = _first_env("MARKET_S3_ENDPOINT", "OBS_ENDPOINT", "S3_ENDPOINT", "AWS_ENDPOINT_URL")
    access_key = _first_env("MARKET_S3_ACCESS_KEY", "OBS_ACCESS_KEY", "AWS_ACCESS_KEY_ID")
    secret_key = _first_env("MARKET_S3_SECRET_KEY", "OBS_SECRET_KEY", "AWS_SECRET_ACCESS_KEY")
    region = _first_env("MARKET_S3_REGION", "OBS_REGION", "AWS_REGION", "AWS_DEFAULT_REGION") or "us-east-1"

    use_ssl_raw = _first_env("MARKET_S3_USE_SSL")
    if use_ssl_raw.lower() in {"1", "true", "yes", "on"}:
        use_ssl = True
    elif use_ssl_raw.lower() in {"0", "false", "no", "off"}:
        use_ssl = False
    else:
        use_ssl = endpoint.startswith("https://") if endpoint else True

    resolved_payload_signing = payload_signing_enabled
    if resolved_payload_signing is None:
        resolved_payload_signing = _resolve_payload_signing_enabled_from_env()
    resolved_addressing_style = addressing_style or _resolve_addressing_style_from_env()
    config = _new_botocore_config(
        Config,
        s3_subconfig={
            "addressing_style": resolved_addressing_style,
            "payload_signing_enabled": resolved_payload_signing,
        },
    )

    kwargs = {
        "service_name": "s3",
        "region_name": region,
        "use_ssl": use_ssl,
        "config": config,
    }
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    if access_key:
        kwargs["aws_access_key_id"] = access_key
    if secret_key:
        kwargs["aws_secret_access_key"] = secret_key
    return boto3.client(**kwargs)


def _new_botocore_config(config_cls, *, s3_subconfig: Dict[str, object]):
    try:
        return config_cls(
            signature_version="s3v4",
            s3=s3_subconfig,
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        )
    except TypeError:
        return config_cls(signature_version="s3v4", s3=s3_subconfig)


def _first_env(*names: str) -> str:
    for name in names:
        value = str(os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def _resolve_payload_signing_enabled_from_env() -> bool:
    raw = _first_env("MARKET_S3_PAYLOAD_SIGNING_ENABLED", "OBS_S3_PAYLOAD_SIGNING_ENABLED")
    return raw.lower() in {"1", "true", "yes", "on"}


def _resolve_addressing_style_from_env() -> str:
    raw = _first_env("MARKET_S3_ADDRESSING_STYLE", "OBS_S3_ADDRESSING_STYLE") or "virtual"
    value = raw.strip().lower()
    return value if value in {"virtual", "path"} else "virtual"


def _is_retryable_put_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            code = str(error.get("Code") or "").strip()
            if code in {"XAmzContentSHA256Mismatch", "VirtualHostDomainRequired"}:
                return True
    text = str(exc)
    return "XAmzContentSHA256Mismatch" in text or "VirtualHostDomainRequired" in text


def _is_missing_object_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            code = str(error.get("Code") or "").strip()
            if code in {"NoSuchKey", "404", "NotFound"}:
                return True
    text = str(exc)
    return "NoSuchKey" in text or "Not Found" in text or "404" in text


__all__ = [
    "S3Location",
    "create_s3_client",
    "download_s3_object_to_path",
    "download_s3_relative_object_if_exists",
    "is_s3_uri",
    "join_s3_uri",
    "materialize_s3_dir",
    "parse_s3_uri",
    "read_s3_bytes",
    "read_s3_text",
    "upload_local_dir_to_s3",
    "upload_s3_bytes",
]
