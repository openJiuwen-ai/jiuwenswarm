# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""用户 overlay 抽离与系统文件同步（键名不改，只换文件）。

小艺能感到且应跨升级保留的项写入 ``config.user.yaml``；系统 ``config.yaml``
与 ``builtin_rules.yaml`` 与模板内容不同则整文件覆盖。``permissions`` 暂跟
系统文件走，不进 overlay（见 ``_SCALAR_PATHS`` 拆分建议）。

分层、强制覆盖、写路径与新增配置步骤：
``docs/zh/配置分层与升级.md``（英文入口 ``docs/en/ConfigLayersAndUpgrade.md``）。
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
from io import StringIO
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML

from jiuwenswarm.common.utils import (
    get_builtin_rules_file,
    get_config_file,
    get_package_config_file,
    get_package_resources_dir,
    get_user_overlay_file,
)

logger = logging.getLogger(__name__)

_EXTRACT_LOCK = threading.Lock()

# 小艺能感到、且升级覆盖系统文件后仍应保留的路径。
#
# permissions 暂不进 overlay：整段跟模板走，升级整文件覆盖。桌面档位本次会话
# 仍可写用户 config.yaml，下次启动以模板为准；发消息时 syncPermissionProfile
# 会再打一次。
#
# permissions 后续拆分建议（产品把档位旋钮从系统树拆开后再加回本表，不要抽 tools 整段）：
#   用户侧：enabled / permission_mode / tools.bash /
#     tools.mcp_free_search|mcp_paid_search|mcp_fetch_webpage /
#     file_guard.defaults.read|write
#   系统侧（继续整文件覆盖）：schema / shell_guard / defaults["*"] /
#     tools 其余键 / rules / file_guard.enabled|workspace|paths /
#     external_directory
_SCALAR_PATHS: tuple[tuple[str, ...], ...] = (
    ("auto_memory_enabled",),
    ("channels", "xiaoyi", "enabled"),
    ("channels", "xiaoyi", "ws_url1"),
    ("channels", "xiaoyi", "file_upload_url"),
    ("mcp", "servers"),
    ("sandbox", "enabled"),
)


def _plain(value: Any) -> Any:
    """ruamel / 自定义类型 → 可比较的纯 Python 对象。

    ``sort_keys=False``：保留用户 yaml 里的键顺序（PyYAML dump 默认会按字母序排，
    导致 overlay 里 GaussPD server 变成 ``command`` 在前，桌面补丁认不出 ``- name:``）。
    """
    if value is None:
        return None
    return yaml.safe_load(
        yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
    )


def _dump_overlay(data: dict[str, Any]) -> str:
    """写出稀疏 overlay：键序保持插入顺序，列表缩进与 ``dump_yaml_round_trip`` 相同。"""
    if not data:
        return ""
    rt = YAML()
    rt.preserve_quotes = True
    rt.default_flow_style = False
    rt.indent(mapping=2, sequence=4, offset=2)
    rt.width = 4096
    buf = StringIO()
    rt.dump(data, buf)
    return buf.getvalue()


def _eq(left: Any, right: Any) -> bool:
    return _plain(left) == _plain(right)


def _get(data: Any, path: tuple[str, ...]) -> Any:
    current = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _set(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    if not path:
        return
    current: dict[str, Any] = data
    for key in path[:-1]:
        nxt = current.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            current[key] = nxt
        current = nxt
    current[path[-1]] = _plain(value)


def _has(data: Any, path: tuple[str, ...]) -> bool:
    current = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return False
        current = current[key]
    return True


def _copy_if_user_changed(
    overlay: dict[str, Any],
    user: Any,
    package: Any,
    path: tuple[str, ...],
) -> None:
    if not _has(user, path):
        return
    user_val = _get(user, path)
    pkg_val = _get(package, path) if isinstance(package, dict) else None
    if _eq(user_val, pkg_val):
        return
    _set(overlay, path, user_val)


def extract_overlay_from_legacy(user: Any, package: Any) -> dict[str, Any]:
    """按白名单从旧完整 yaml 抽出与模板不同的用户子树。"""
    overlay: dict[str, Any] = {}
    if not isinstance(user, dict):
        return overlay
    if not isinstance(package, dict):
        package = {}
    for path in _SCALAR_PATHS:
        _copy_if_user_changed(overlay, user, package, path)
    return overlay


def extract_user_overlay(
    *,
    user_yaml: Path,
    overlay_yaml: Path,
    package_yaml: Path | None,
) -> bool:
    """旧完整 ``config.yaml`` → 稀疏 ``config.user.yaml``。已有 overlay 则跳过。

    不改名/删除 ``config.yaml``（回滚旧 exe 仍认该文件）。
    """
    if overlay_yaml.is_file():
        return False
    if not user_yaml.is_file():
        return False
    if package_yaml is None or not package_yaml.is_file():
        logger.warning("skip overlay extract: package config.yaml missing")
        return False

    try:
        user_data = yaml.safe_load(user_yaml.read_text(encoding="utf-8")) or {}
        package_data = yaml.safe_load(package_yaml.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        logger.exception("overlay extract failed to parse yaml")
        return False

    overlay = extract_overlay_from_legacy(user_data, package_data)
    overlay_yaml.parent.mkdir(parents=True, exist_ok=True)
    overlay_yaml.write_text(_dump_overlay(overlay), encoding="utf-8")
    logger.info("wrote user overlay %s (%s top-level keys)", overlay_yaml, len(overlay))
    return True


def copy_if_missing_or_changed(src: Path, dest: Path) -> bool:
    """源存在且（目标缺失或内容不同）时 ``copy2``。内容相同则跳过。"""
    if not src.is_file():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.read_bytes() == src.read_bytes():
        return False
    shutil.copy2(src, dest)
    logger.info("synced system file %s from %s", dest, src)
    return True


def drop_permissions_from_overlay(overlay_yaml: Path) -> bool:
    """已抽出的 overlay 若仍带 permissions，删掉该键（档位暂跟系统文件）。"""
    if not overlay_yaml.is_file():
        return False
    try:
        data = yaml.safe_load(overlay_yaml.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        logger.exception("drop permissions from overlay failed to parse %s", overlay_yaml)
        return False
    if not isinstance(data, dict) or "permissions" not in data:
        return False
    data.pop("permissions", None)
    overlay_yaml.write_text(_dump_overlay(data), encoding="utf-8")
    logger.info("removed permissions from user overlay %s", overlay_yaml)
    return True


def sync_system_files_from_package(
    *,
    user_yaml: Path,
    overlay_yaml: Path,
    package_yaml: Path | None,
    user_builtin_rules: Path | None = None,
    package_builtin_rules: Path | None = None,
) -> bool:
    """先抽 overlay，再按内容覆盖用户系统 yaml / builtin_rules。"""
    extracted = extract_user_overlay(
        user_yaml=user_yaml,
        overlay_yaml=overlay_yaml,
        package_yaml=package_yaml,
    )
    stripped = drop_permissions_from_overlay(overlay_yaml)
    copied = False
    if package_yaml is not None:
        copied = copy_if_missing_or_changed(package_yaml, user_yaml) or copied
    if package_builtin_rules is not None and user_builtin_rules is not None:
        copied = (
            copy_if_missing_or_changed(package_builtin_rules, user_builtin_rules)
            or copied
        )
    return extracted or stripped or copied


def _package_builtin_rules_file() -> Path | None:
    res = get_package_resources_dir()
    if res is None:
        return None
    path = res / "builtin_rules.yaml"
    return path if path.is_file() else None


def maybe_extract_user_overlay() -> bool:
    """对当前 ``JIUWENSWARM_DATA_DIR`` 用户根做一次幂等抽离，并同步系统文件。"""
    if "pytest" in sys.modules and not os.environ.get("JIUWENSWARM_ALLOW_OVERLAY_EXTRACT"):
        return False
    with _EXTRACT_LOCK:
        return sync_system_files_from_package(
            user_yaml=get_config_file(),
            overlay_yaml=get_user_overlay_file(),
            package_yaml=get_package_config_file(),
            user_builtin_rules=get_builtin_rules_file(),
            package_builtin_rules=_package_builtin_rules_file(),
        )


def overlay_sibling_of(config_yaml_path: Path) -> Path:
    """``config.yaml`` 同目录的 ``config.user.yaml``（尊重测试里替换的 CONFIG_YAML_PATH）。"""
    return Path(config_yaml_path).with_name("config.user.yaml")


def resolve_user_config_io_path(requested: Path, config_yaml_path: Path) -> Path:
    """写路径：已有 overlay 则写 overlay；否则仍写 ``config.yaml``（抽离前 / 单测）。"""
    requested = Path(requested)
    try:
        if requested.resolve() != Path(config_yaml_path).resolve():
            return requested
    except OSError:
        return requested
    overlay = overlay_sibling_of(config_yaml_path)
    if overlay.is_file():
        return overlay
    if not Path(config_yaml_path).is_file():
        return overlay
    return requested
