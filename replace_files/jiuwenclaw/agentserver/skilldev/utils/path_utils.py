# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""路径工具函数：工作区路径解析与校验。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)


def resolve_workspace_dir() -> str | None:
    """获取当前请求的有效工作区目录。

    Returns:
        工作区目录的绝对路径，如果无法获取则返回 None。
    """
    try:
        from jiuwenclaw.agentserver.tools.subagent_executor.context_vars import (
            get_effective_request_workspace_dir,
        )
        return get_effective_request_workspace_dir()
    except Exception:
        return None


def validate_path_in_workspace(
        file_path: str,
        *,
        workspace_dir: str | None = None,
        strict: bool = True,
) -> Tuple[bool, str, str | None]:
    """校验文件路径是否在工作区内。

    Args:
        file_path: 待校验的文件路径（可以是相对路径或绝对路径）
        workspace_dir: 工作区目录，如果为 None 则自动获取
        strict: 如果为 True，无法获取工作区时返回失败；
                如果为 False，无法获取工作区时仅做路径标准化

    Returns:
        Tuple[是否校验通过, 标准化后的绝对路径, 错误信息（成功时为 None）]

    Examples:
        >>> validate_path_in_workspace("docs/file.pdf", workspace_dir="/workspace")
        (True, "/workspace/docs/file.pdf", None)

        >>> validate_path_in_workspace("../etc/passwd", workspace_dir="/workspace/task")
        (False, "/etc/passwd", "文件路径必须位于工作区内")
    """
    if not file_path:
        return False, "", "文件路径不能为空"

    if not isinstance(file_path, str):
        return False, "", "文件路径必须是字符串"

    if workspace_dir is None:
        workspace_dir = resolve_workspace_dir()

    if not workspace_dir:
        logger.warning("[validate_path_in_workspace] 无法获取工作区目录")
        if strict:
            return False, file_path, "无法获取工作区目录，无法进行路径安全校验"
        # 非严格模式下，仅做路径标准化
        try:
            resolved = Path(file_path).expanduser().resolve()
            return True, str(resolved), None
        except Exception as e:
            logger.error("[validate_path_in_workspace] 路径解析失败: %s", e)
            return False, file_path, "路径解析失败"

    try:
        # 标准化工作区路径（解析符号链接）
        workspace = Path(workspace_dir).resolve()

        # 标准化目标路径（解析符号链接、.. 等）
        target = Path(file_path).expanduser().resolve()

        # 校验：目标路径必须是工作区的子路径
        try:
            target.relative_to(workspace)
            return True, str(target), None
        except ValueError:
            # 路径不在工作区内
            logger.error(
                "[validate_path_in_workspace] 路径越界: target=%s, workspace=%s",
                target, workspace
            )
            # 向用户返回泛化错误，不暴露工作区绝对路径
            return False, str(target), "文件路径必须位于工作区内"

    except Exception as e:
        logger.exception("[validate_path_in_workspace] 路径校验异常: %s", e)
        return False, file_path, "路径校验失败"


def resolve_and_validate_path(
        file_path: str,
        *,
        workspace_dir: str | None = None,
        must_exist: bool = True,
        must_be_file: bool = True,
) -> Tuple[bool, str, str | None]:
    """完整的路径解析和校验（包含存在性检查）。

    Args:
        file_path: 待校验的文件路径
        workspace_dir: 工作区目录，如果为 None 则自动获取
        must_exist: 是否要求文件必须存在
        must_be_file: 是否要求路径必须是文件（而非目录）

    Returns:
        Tuple[是否校验通过, 标准化后的绝对路径, 错误信息（成功时为 None）]
    """
    # 第一步：工作区边界校验
    is_valid, resolved_path, error_msg = validate_path_in_workspace(
        file_path, workspace_dir=workspace_dir
    )
    if not is_valid:
        return False, resolved_path, error_msg

    # 第二步：存在性校验（向用户返回泛化错误，不暴露绝对路径）
    if must_exist and not Path(resolved_path).exists():
        logger.error("[resolve_and_validate_path] 文件不存在: %s", resolved_path)
        return False, resolved_path, "文件不存在"

    # 第三步：类型校验（向用户返回泛化错误，不暴露绝对路径）
    if must_be_file and not Path(resolved_path).is_file():
        logger.error("[resolve_and_validate_path] 路径不是文件: %s", resolved_path)
        return False, resolved_path, "路径不是文件"

    return True, resolved_path, None


__all__ = [
    "resolve_workspace_dir",
    "validate_path_in_workspace",
    "resolve_and_validate_path",
]
