"""配置布尔与心跳文案的轻量解析工具。

职责：
- ``_parse_bool`` / ``_env_truthy``：容忍 YAML / env 插值后的字符串布尔
- ``_clean_heartbeat_content``：去掉 HEARTBEAT.md 的 HTML 注释与空行

与模型路由、context rail 装配无关；只放无状态小函数。
"""

from __future__ import annotations

import os
from typing import Any


def _clean_heartbeat_content(content: str) -> str:
    """Remove HTML comments and blank lines from HEARTBEAT.md content."""
    cleaned_lines: list[str] = []
    for line in content.splitlines():
        stripped_line = line.strip()
        if stripped_line.startswith("<!--") and stripped_line.endswith("-->"):
            continue
        if stripped_line:
            cleaned_lines.append(stripped_line)
    return "\n".join(cleaned_lines)


def _parse_bool(value: Any, *, default: bool = False) -> bool:
    """Parse YAML bool / env-var-backed boolean strings.

    ``${VAR:-default}`` 插值会把 env 值解析成字符串（config.resolve_env_vars），
    所以「环境变量驱动的开关」必须容忍 "true"/"1"/"yes"/"on" 这类字符串，不能
    用 ``is True`` 判等。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    return default


def _env_truthy(name: str) -> bool:
    """True when the environment variable is explicitly enabled."""
    return _parse_bool(os.getenv(name), default=False)
