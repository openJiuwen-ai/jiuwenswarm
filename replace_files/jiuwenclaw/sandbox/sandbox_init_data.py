from __future__ import annotations

import json
import os
import posixpath
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from jiuwenclaw.utils import logger

if TYPE_CHECKING:
    from jiuwenclaw.sandbox.sandbox_client import SandboxClient

SANDBOX_INIT_DATA_PATH_ENV = "SANDBOX_INIT_DATA_PATH"
USE_SIDECAR_ENV = "USE_SIDECAR"
DEFAULT_SANDBOX_INIT_DATA_PATH = "/opt/huawei/app/jiuwenclaw/init_data.json"


def get_sandbox_init_data_path() -> str:
    raw = os.environ.get(SANDBOX_INIT_DATA_PATH_ENV, "").strip()
    if raw:
        return raw
    return DEFAULT_SANDBOX_INIT_DATA_PATH


def sandbox_sidecar_enabled() -> bool:
    raw = os.environ.get(USE_SIDECAR_ENV)
    if raw is None:
        return False
    return raw.strip().lower() == "true"


def strip_sandbox_remote_path_prefix(path: str) -> str:
    """Strip any directory prefix from a sandbox-side path, returning the basename.

    ``SandboxClient.upload_file`` expects ``remote_path`` to be a name relative
    to the sandbox upload root, while ``SANDBOX_INIT_DATA_PATH`` is configured
    as an absolute path (so AgentServer inside the sandbox can read it
    directly). Drop every directory component before invoking ``upload_file``.
    """

    return posixpath.basename(path.strip())


def build_sandbox_init_data_payload(
    *,
    api_key: str,
    sandbox_id: str,
    include_api_key: bool = True,
) -> dict[str, str]:
    payload: dict[str, str] = {}
    if include_api_key:
        payload["apiKey"] = str(api_key).strip()
    payload["sandboxId"] = str(sandbox_id).strip()
    return payload


def serialize_sandbox_init_data(
    *,
    api_key: str,
    sandbox_id: str,
    include_api_key: bool = True,
) -> str:
    payload = build_sandbox_init_data_payload(
        api_key=api_key,
        sandbox_id=sandbox_id,
        include_api_key=include_api_key,
    )
    return json.dumps(payload, ensure_ascii=False)


async def upload_sandbox_init_data(
    sandbox_client: SandboxClient,
    *,
    sandbox_id: str,
    api_key: str,
    user_id: str,
    remote_path: str | None = None,
) -> None:
    effective_user_id = str(user_id).strip()
    if sandbox_sidecar_enabled():
        # 上传环境变量文件到沙箱
        try:
            from jiuwenclaw.gateway.env_sync_file import upload_env_to_agentserver
            await upload_env_to_agentserver(sandbox_client, sandbox_id)
        except Exception as e:
            logger.warning(
                "Failed to upload env to sandbox %s: %s, continuing anyway",
                sandbox_id, e
            )

        sidecar_result = await sandbox_client.upload_apikey_to_sidecar(
            sandbox_id,
            api_key,
            effective_user_id,
        )
        if not sidecar_result.success:
            raise RuntimeError(
                sidecar_result.error or "upload_apikey_to_sidecar failed"
            )
        logger.info("Uploaded sandbox sidecar config for sandbox_id=%s", sandbox_id)
        return

    target_path = (remote_path or get_sandbox_init_data_path()).strip()
    if not target_path:
        raise ValueError("sandbox init data remote path is empty")
    upload_path = strip_sandbox_remote_path_prefix(target_path)
    if not upload_path:
        raise ValueError("sandbox init data upload path is empty after prefix strip")
    content = serialize_sandbox_init_data(
        api_key=api_key,
        sandbox_id=sandbox_id,
    )
    local_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            delete=False,
        ) as handle:
            handle.write(content)
            local_path = handle.name
        result = await sandbox_client.upload_file(
            local_path,
            upload_path,
            sandbox_id,
        )
        if not result.success:
            raise RuntimeError(result.error or "upload_file failed for sandbox init data")
        logger.info(
            "Uploaded sandbox init data for sandbox_id=%s remote_path=%s upload_path=%s",
            sandbox_id,
            target_path,
            upload_path,
        )
    finally:
        if local_path:
            try:
                Path(local_path).unlink(missing_ok=True)
            except OSError:
                logger.exception("Failed to remove temporary sandbox init data file: %s", local_path)
