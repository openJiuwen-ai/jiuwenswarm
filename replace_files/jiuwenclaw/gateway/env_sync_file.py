# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Environment variable synchronization from Gateway to AgentServer.

Uploads environment variables to sandbox via file transfer.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from jiuwenclaw.sandbox.sandbox_client import SandboxClient

from jiuwenclaw.common.env_schema import ALLOWED_ENV_KEYS

logger = logging.getLogger(__name__)


async def upload_env_to_agentserver(
        sandbox_client: "SandboxClient",
        sandbox_id: str,
        target_path: str = "agentserver_env.json",

) -> bool:
    """上传环境变量文件到沙箱.

    Args:
        sandbox_client: 沙箱客户端实例
        sandbox_id: sandbox_id
        target_path: 目标路径（沙箱内）

    Returns:
        上传是否成功
    """
    env_file = prepare_agentserver_env_file()
    if env_file is None:
        logger.warning("[sandbox_id=%s] [EnvSync] No env vars to sync, skipping upload", sandbox_id)
        return False

    try:
        await sandbox_client.upload_file(
            local_path=str(env_file),
            remote_path=target_path,
            sandbox_id=sandbox_id,
        )
        logger.info("[sandbox_id=%s] [EnvSync] Uploaded env file to %s", sandbox_id, target_path)
        return True
    except Exception as e:
        logger.error("[sandbox_id=%s] [EnvSync] Failed to upload env file: %s", sandbox_id, e)
        return False
    finally:
        # 清理临时文件
        if env_file:
            try:
                env_file.unlink(missing_ok=True)
            except OSError:
                logger.exception(
                    "[sandbox_id=%s] [EnvSync] Failed to remove temporary sandbox "
                    "init env data file: %s", sandbox_id, env_file
                )


def collect_syncable_env() -> Dict[str, str]:
    """收集需要同步到 AgentServer 的环境变量.

    Returns:
        环境变量字典（key -> value）
    """
    result = {}
    for key in ALLOWED_ENV_KEYS:
        value = os.getenv(key)
        if value is not None:
            result[key] = value

    # 打印收集的环境变量名称
    if result:
        logger.info(
            "[EnvSync] Collected env vars to sync: %s",
            ", ".join(result.keys()),
        )

    return result


def prepare_agentserver_env_file() -> Path | None:
    """生成 AgentServer 环境变量文件（JSON 格式）.

    Returns:
        临时文件路径，如无变量需要同步则返回 None
    """
    env_vars = collect_syncable_env()

    if not env_vars:
        logger.debug("[EnvSync] No env vars to sync")
        return None

    env_data = {
        "version": "1.0",
        "generated_at": datetime.now().isoformat(),
        "source": "gateway",
        "env_vars": env_vars,
    }

    # 创建临时文件
    with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix="agentserver_env.json",
            delete=False,
    ) as f:
        json.dump(env_data, f, indent=2, ensure_ascii=False)
        return Path(f.name)
