# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Environment variable loader for AgentServer.

Supports blocking wait for env file upload from Gateway (warm-up pool mode).
Implements security checks for Kubernetes/container environment.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, Optional

from jiuwenclaw.common.env_schema import (
    ALLOWED_ENV_KEYS,
    PROTECTED_ENV_KEYS,
)

logger = logging.getLogger(__name__)

# 环境变量文件路径
ENV_FILE_PATH = Path("/opt/huawei/app/jiuwenclaw/agentserver_env.json")

# 默认配置
DEFAULT_POLL_INTERVAL = 0.5  # 轮询间隔（秒）


async def wait_and_load_env(
        timeout: Optional[float] = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> Dict[str, str]:
    """阻塞等待并加载环境变量文件.

    用于预热模式：AgentServer 启动时阻塞等待 GW 上传 env 文件.
    默认无限等待，直到文件出现。

    Args:
        timeout: 最大等待时间（秒），None 表示无限等待
        poll_interval: 轮询检查间隔（秒），默认 0.5s

    Returns:
        加载的环境变量字典（key -> value）
    """
    import asyncio

    start_time = time.time()

    if timeout is None:
        logger.info(
            "[EnvLoader] Waiting for env file: %s (infinite wait)",
            ENV_FILE_PATH,
        )
    else:
        logger.info(
            "[EnvLoader] Waiting for env file: %s (timeout=%ss)",
            ENV_FILE_PATH,
            timeout,
        )

    # 轮询等待文件出现
    poll_count = 0
    while not ENV_FILE_PATH.exists():
        elapsed = time.time() - start_time
        if timeout is not None and elapsed >= timeout:
            logger.warning(
                "[EnvLoader] Timeout waiting for env file after %.1fs, using fallback",
                elapsed,
            )
            return _load_local_fallback()

        # 每轮询 10 次记录一次日志
        poll_count += 1
        if poll_count % 10 == 0:
            if timeout is None:
                logger.info(
                    "[EnvLoader] Still waiting for env file... (%.1fs elapsed, %d polls)",
                    elapsed,
                    poll_count,
                )
            else:
                logger.info(
                    "[EnvLoader] Still waiting for env file... (%.1fs/%ss, %d polls)",
                    elapsed,
                    timeout,
                    poll_count,
                )

        await asyncio.sleep(poll_interval)

    # 文件存在，加载
    logger.info("[EnvLoader] Env file detected, loading...")
    result = _load_from_file()

    # 打印加载的环境变量汇总
    if result:
        logger.info(
            "[EnvLoader] Environment variables ready: %s",
            ", ".join(result.keys()),
        )

    return result


def load_env_from_file() -> Dict[str, str]:
    """非阻塞方式加载环境变量文件.

    用于非预热模式或已确定文件存在的场景.

    Returns:
        加载的环境变量字典
    """
    if not ENV_FILE_PATH.exists():
        logger.debug("[EnvLoader] Env file not found: %s", ENV_FILE_PATH)
        return {}

    return _load_from_file()


def _load_from_file() -> Dict[str, str]:
    """从 JSON 文件加载环境变量（内部方法）."""
    try:
        with open(ENV_FILE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        env_vars = data.get("env_vars", {})
        filtered = _filter_env_vars(env_vars)

        # 应用到当前进程
        applied = []
        for key, value in filtered.items():
            os.environ[key] = str(value)
            applied.append(key)

        # 打印更新的环境变量名称
        if applied:
            logger.info(
                "[EnvLoader] Updated environment variables: %s",
                ", ".join(applied),
            )
        logger.info(
            "[EnvLoader] Loaded %d env vars from %s",
            len(applied),
            data.get("source", "unknown"),
        )
        return filtered

    except json.JSONDecodeError as e:
        logger.error("[EnvLoader] Invalid JSON in env file: %s", e)
        return _load_local_fallback()
    except Exception as e:
        logger.exception("[EnvLoader] Failed to load env file: %s", e)
        return _load_local_fallback()


def _load_local_fallback() -> Dict[str, str]:
    """降级：使用本地已存在的环境变量."""
    logger.info("[EnvLoader] Using local environment variables as fallback")

    fallback_vars = {}
    for key in ALLOWED_ENV_KEYS:
        value = os.getenv(key)
        if value:
            fallback_vars[key] = value

    if fallback_vars:
        logger.info(
            "[EnvLoader] Using local environment variables: %s",
            ", ".join(fallback_vars.keys()),
        )
    logger.info(
        "[EnvLoader] Found %d env vars from local environment",
        len(fallback_vars),
    )
    return fallback_vars


def _filter_env_vars(env_vars: Dict[str, str]) -> Dict[str, str]:
    """过滤环境变量（白名单 + 安全检查）.

    基于 Kubernetes 和容器化环境的安全要求：
    - 禁止修改 K8s 系统变量
    - 禁止修改 Sidecar/框架变量
    - 只允许白名单中的应用配置变量

    Args:
        env_vars: 从文件读取的原始环境变量

    Returns:
        过滤后的环境变量
    """
    filtered = {}
    rejected = []

    for key, value in env_vars.items():
        # 检查是否为受保护的系统变量
        if key in PROTECTED_ENV_KEYS:
            rejected.append(f"{key}(protected)")
            logger.error(
                "[EnvLoader] BLOCKED attempt to modify protected env var: %s. "
                "This could cause K8s/Sidecar malfunction!",
                key,
            )
            continue

        # 检查是否在白名单中
        if key not in ALLOWED_ENV_KEYS:
            rejected.append(f"{key}(not_in_whitelist)")
            logger.debug("[EnvLoader] Rejected non-whitelist env var: %s", key)
            continue

        filtered[key] = str(value)

    # 汇总日志
    if rejected:
        logger.warning("[EnvLoader] Rejected %d env vars: %s", len(rejected), rejected)

    # 记录 K8s 系统变量保护状态
    k8s_protected = [k for k in env_vars if k.startswith("KUBERNETES_")]
    if k8s_protected:
        logger.info(
            "[EnvLoader] Protected %d K8s system variables from modification",
            len(k8s_protected),
        )

    return filtered
