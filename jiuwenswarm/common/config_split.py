# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""升级覆盖时按白名单保留用户感知配置（键名不改，仍落盘 ``config.yaml``）。

活配置只有用户根 ``config.yaml``。包内模板只当拷贝源。

**重启 vs 升级**：戳 ``config/.template.sha256`` 比的是包内模板哈希，不是
用户 yaml 是否被改过。运行时写沙箱 / HITL / 管道 / ``last_*`` 再重启不会
被判成升级。不要用安装包版本号当 copy2 主信号。升级当且仅当戳缺失、哈希
变了、无用户 yaml、缺 builtin_rules、或 ``init -f``。

覆盖前快照白名单（仅与**当前模板不同**的用户值）与 keep-set（``last_*`` /
``push_id``），``copy2`` 后写回。从 ``_SCALAR_PATHS`` / ``LIST_PATHS`` 拿掉
= 下次升级跟模板锁定；新增须是小艺 PC/手机点击且键与点击一一对应。

现网遗留 ``config.user.yaml`` 只在启动时把当前白名单叶折进 yaml，之后不读不写。
分层说明：``docs/zh/配置分层与升级.md``。
"""

from __future__ import annotations

import hashlib
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

# 小艺 PC/手机点过、升级 copy2 后写回 yaml（仅当值与当前包内模板不同）。
# 新增：键与点击一一对应，写入 config.yaml，不要预写模板默认。
# 删减：从本表拿掉，下次升级跟模板锁定。不要抽 tools 整段。
# xiaoyi / GaussPD / auto_memory / 权限档 knobs 不要加本表（桌面打回 / 跟模板）。
_SCALAR_PATHS: tuple[tuple[str, ...], ...] = (
    ("sandbox", "enabled"),
)

# 整工具档位：写 config.yaml。不进升级白名单（升级后桌面 waitForUp 打回）。
PERMISSION_KNOB_PATHS: tuple[tuple[str, ...], ...] = (
    ("permissions", "enabled"),
    ("permissions", "permission_mode"),
    ("permissions", "tools", "bash"),
    ("permissions", "tools", "mcp_free_search"),
    ("permissions", "tools", "mcp_paid_search"),
    ("permissions", "tools", "mcp_fetch_webpage"),
    ("permissions", "file_guard", "defaults", "read"),
    ("permissions", "file_guard", "defaults", "write"),
)

# 小艺 PC/手机 HITL「永久记住」。按 id（路径条目按 path）upsert 回新模板 list。
# 从本表拿掉 = 下次升级不再写回用户条目。Web/TUI rules、/add-dir 不进本表。
LIST_PATHS: tuple[tuple[str, ...], ...] = (
    ("permissions", "approval_overrides"),
    ("permissions", "file_guard", "paths"),
)

# 用户 config 目录；比的是包内模板哈希，不是用户 yaml 是否等于模板。
TEMPLATE_STAMP_NAME = ".template.sha256"

_XIAOYI_RUNTIME_KEEP_KEYS: tuple[str, ...] = (
    "last_session_id",
    "last_task_id",
    "last_message_id",
    "push_id",
)


def _plain(value: Any) -> Any:
    """ruamel / 自定义类型 → 可比较的纯 Python 对象。

    ``sort_keys=False``：保留用户 yaml 里的键顺序（PyYAML dump 默认会按字母序排，
    导致 Celia server 变成 ``command`` 在前，桌面补丁认不出 ``- name:``）。
    """
    if value is None:
        return None
    return yaml.safe_load(
        yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
    )


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


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        logger.exception("failed to parse %s", path)
        return {}
    return data if isinstance(data, dict) else {}


def _dump_ruamel_mapping(path: Path, loaded: Any) -> None:
    rt = YAML()
    rt.preserve_quotes = True
    rt.default_flow_style = False
    rt.indent(mapping=2, sequence=4, offset=2)
    rt.width = 4096
    buf = StringIO()
    rt.dump(loaded, buf)
    path.write_text(buf.getvalue(), encoding="utf-8")


def _set_path_on_mapping(loaded: Any, path: tuple[str, ...], value: Any) -> None:
    if not isinstance(loaded, dict) or not path:
        return
    current: Any = loaded
    for key in path[:-1]:
        nxt = current.get(key) if isinstance(current, dict) else None
        if not isinstance(nxt, dict):
            nxt = {}
            if isinstance(current, dict):
                current[key] = nxt
        current = nxt
    if isinstance(current, dict):
        current[path[-1]] = value


def _list_item_key(item: Any, path: tuple[str, ...]) -> str | None:
    if not isinstance(item, dict):
        return None
    rid = str(item.get("id") or "").strip()
    if rid:
        return f"id:{rid}"
    if path == ("permissions", "file_guard", "paths"):
        raw = str(item.get("path") or "").replace("\\", "/").rstrip("/")
        return f"path:{raw}" if raw else None
    return None


def _filter_user_list_items(items: Any, path: tuple[str, ...]) -> list[Any]:
    if not isinstance(items, list):
        return []
    out: list[Any] = []
    for item in items:
        if isinstance(item, dict):
            out.append(item)
    return out


def _user_only_list_items(
    user_items: Any,
    package_items: Any,
    path: tuple[str, ...],
) -> list[Any]:
    """相对模板多出来的用户条目。"""
    user_items = _filter_user_list_items(user_items, path)
    pkg_by_key: dict[str, Any] = {}
    if isinstance(package_items, list):
        for item in package_items:
            key = _list_item_key(item, path)
            if key:
                pkg_by_key[key] = item
    extra: list[Any] = []
    for item in user_items:
        key = _list_item_key(item, path)
        if not key:
            extra.append(item)
            continue
        pkg_item = pkg_by_key.get(key)
        if pkg_item is None or not _eq(item, pkg_item):
            extra.append(item)
    return extra


def upsert_list_by_id(
    base_list: Any,
    overlay_list: Any,
    path: tuple[str, ...],
) -> list[Any]:
    """按 id（路径条目按 path）upsert：后者同键赢，独有键追加。"""
    result: list[Any] = []
    index_by_key: dict[str, int] = {}

    def _append(item: Any) -> None:
        key = _list_item_key(item, path)
        if key and key in index_by_key:
            result[index_by_key[key]] = _plain(item)
            return
        if key:
            index_by_key[key] = len(result)
        result.append(_plain(item) if isinstance(item, dict) else item)

    if isinstance(base_list, list):
        for item in base_list:
            _append(item)
    if isinstance(overlay_list, list):
        for item in overlay_list:
            _append(item)
    return result


def extract_user_keep_from_legacy(user: Any, package: Any) -> dict[str, Any]:
    """从旧完整 yaml 抽出白名单：标量仅当与**当前**包内模板不同；list 仅用户条目。

    与模板相同则不进快照——升级后跟新默认。强制改掉用户点过的值：从白名单拿掉。
    """
    keep: dict[str, Any] = {}
    if not isinstance(user, dict):
        return keep
    if not isinstance(package, dict):
        package = {}
    for path in _SCALAR_PATHS:
        if not _has(user, path):
            continue
        user_val = _get(user, path)
        pkg_val = _get(package, path)
        if _eq(user_val, pkg_val):
            continue
        _set(keep, path, user_val)
    for path in LIST_PATHS:
        extra = _user_only_list_items(_get(user, path), _get(package, path), path)
        if extra:
            _set(keep, path, extra)
    return keep


def project_user_keep(data: Any) -> dict[str, Any]:
    """只保留白名单叶；名单外的键丢掉。"""
    projected: dict[str, Any] = {}
    if not isinstance(data, dict):
        return projected
    for path in _SCALAR_PATHS:
        if _has(data, path):
            _set(projected, path, _get(data, path))
    for path in LIST_PATHS:
        if not _has(data, path):
            continue
        value = _get(data, path)
        if value is None:
            continue
        _set(projected, path, _plain(value))
    return projected


def snapshot_user_keep_set(
    *,
    user_yaml: Path,
    overlay_yaml: Path | None,
    package_yaml: Path | None,
) -> dict[str, Any]:
    """覆盖前：yaml 与遗留 overlay 的白名单并集（overlay 同键赢）。"""
    package = _load_yaml_mapping(package_yaml) if package_yaml is not None else {}
    from_yaml = extract_user_keep_from_legacy(_load_yaml_mapping(user_yaml), package)
    from_overlay = project_user_keep(
        _load_yaml_mapping(overlay_yaml) if overlay_yaml is not None else {}
    )
    keep: dict[str, Any] = dict(from_yaml)
    for path in _SCALAR_PATHS:
        if _has(from_overlay, path):
            _set(keep, path, _get(from_overlay, path))
    for path in LIST_PATHS:
        merged = upsert_list_by_id(_get(keep, path), _get(from_overlay, path), path)
        if merged:
            _set(keep, path, merged)
    return keep


def restore_user_keep_set(user_yaml: Path, keep: dict[str, Any]) -> None:
    """覆盖后把白名单写回 yaml：标量覆盖，list 按 id upsert 到新模板上。"""
    if not keep or not user_yaml.is_file():
        return
    rt = YAML()
    rt.preserve_quotes = True
    try:
        loaded = rt.load(user_yaml.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("user-keep restore failed to parse %s", user_yaml)
        return
    if not isinstance(loaded, dict):
        return
    for path in _SCALAR_PATHS:
        if _has(keep, path):
            _set_path_on_mapping(loaded, path, _plain(_get(keep, path)))
    for path in LIST_PATHS:
        if not _has(keep, path):
            continue
        existing = _get(loaded, path)
        _set_path_on_mapping(
            loaded, path, upsert_list_by_id(existing, _get(keep, path), path)
        )
    _dump_ruamel_mapping(user_yaml, loaded)
    logger.info("restored user-keep onto %s", user_yaml)


def drop_legacy_overlay(overlay_yaml: Path) -> bool:
    """折进 yaml 之后删掉遗留 overlay，避免下次启动再叠一层。"""
    if not overlay_yaml.is_file():
        return False
    try:
        overlay_yaml.unlink()
    except OSError:
        logger.exception("failed to remove leftover overlay %s", overlay_yaml)
        return False
    logger.info("removed leftover overlay %s", overlay_yaml)
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


def template_stamp_path(user_yaml: Path) -> Path:
    return Path(user_yaml).with_name(TEMPLATE_STAMP_NAME)


def _file_sha256(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compute_template_stamp(
    package_yaml: Path | None, package_builtin_rules: Path | None
) -> str:
    """包内模板指纹。用户 yaml 内容不参与；安装包版本号不参与。"""
    return (
        f"config.yaml {_file_sha256(package_yaml)}\n"
        f"builtin_rules.yaml {_file_sha256(package_builtin_rules)}\n"
    )


def read_template_stamp(user_yaml: Path) -> str | None:
    path = template_stamp_path(user_yaml)
    if not path.is_file():
        return None
    # 按字节读再收 CRLF：Windows 文本写会把 \n 落成 \r\n，桌面 Node 按原样比对。
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def write_template_stamp(
    user_yaml: Path,
    package_yaml: Path | None,
    package_builtin_rules: Path | None,
) -> None:
    path = template_stamp_path(user_yaml)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        compute_template_stamp(package_yaml, package_builtin_rules).encode("utf-8")
    )


def _nonempty_str(value: Any) -> str:
    text = str(value).strip() if value is not None else ""
    return text


def snapshot_runtime_keep_set(user_yaml: Path) -> dict[str, Any]:
    """覆盖前从旧 yaml 抽出运行时身份键。无文件或全空则 ``{}``。"""
    if not user_yaml.is_file():
        return {}
    try:
        data = yaml.safe_load(user_yaml.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        logger.exception("keep-set snapshot failed to parse %s", user_yaml)
        return {}
    if not isinstance(data, dict):
        return {}
    keep: dict[str, Any] = {}
    channels = data.get("channels")
    xiaoyi = channels.get("xiaoyi") if isinstance(channels, dict) else None
    if isinstance(xiaoyi, dict):
        runtime: dict[str, Any] = {}
        for key in _XIAOYI_RUNTIME_KEEP_KEYS:
            raw = xiaoyi.get(key)
            if raw is None:
                continue
            if isinstance(raw, str) and not raw.strip():
                continue
            runtime[key] = raw
        if runtime:
            keep["xiaoyi_runtime"] = runtime
        apps_push: list[dict[str, Any]] = []
        apps = xiaoyi.get("apps")
        if isinstance(apps, list):
            for app in apps:
                if not isinstance(app, dict):
                    continue
                push_id = _nonempty_str(app.get("push_id"))
                if not push_id:
                    continue
                apps_push.append(
                    {
                        "name": app.get("name"),
                        "api_id": app.get("api_id"),
                        "agent_id": app.get("agent_id"),
                        "push_id": push_id,
                    }
                )
        if apps_push:
            keep["xiaoyi_apps_push_id"] = apps_push
    return keep


def _ensure_map(parent: dict[str, Any], key: str) -> dict[str, Any]:
    current = parent.get(key)
    if not isinstance(current, dict):
        current = {}
        parent[key] = current
    return current


def _match_xiaoyi_app(apps: list[Any], entry: dict[str, Any]) -> dict[str, Any] | None:
    name = entry.get("name")
    api_id = _nonempty_str(entry.get("api_id"))
    agent_id = _nonempty_str(entry.get("agent_id"))
    dict_apps = [app for app in apps if isinstance(app, dict)]
    if name:
        for app in dict_apps:
            if app.get("name") == name:
                return app
    if api_id:
        for app in dict_apps:
            if _nonempty_str(app.get("api_id")) == api_id:
                return app
    if agent_id:
        for app in dict_apps:
            if _nonempty_str(app.get("agent_id")) == agent_id:
                return app
    if len(dict_apps) == 1:
        return dict_apps[0]
    return None


def restore_runtime_keep_set(user_yaml: Path, keep: dict[str, Any]) -> None:
    """覆盖后把 keep-set 写回 yaml（ruamel，尽量保留模板注释）。"""
    if not keep or not user_yaml.is_file():
        return
    rt = YAML()
    rt.preserve_quotes = True
    try:
        loaded = rt.load(user_yaml.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("keep-set restore failed to parse %s", user_yaml)
        return
    if not isinstance(loaded, dict):
        return
    runtime = keep.get("xiaoyi_runtime")
    apps_push = keep.get("xiaoyi_apps_push_id")
    if runtime or apps_push:
        channels = _ensure_map(loaded, "channels")
        xiaoyi = _ensure_map(channels, "xiaoyi")
        if isinstance(runtime, dict):
            for key, value in runtime.items():
                xiaoyi[key] = value
        if isinstance(apps_push, list):
            apps = xiaoyi.get("apps")
            if isinstance(apps, list):
                for entry in apps_push:
                    if not isinstance(entry, dict):
                        continue
                    push_id = _nonempty_str(entry.get("push_id"))
                    if not push_id:
                        continue
                    target = _match_xiaoyi_app(apps, entry)
                    if target is not None:
                        target["push_id"] = push_id
    _dump_ruamel_mapping(user_yaml, loaded)
    logger.info("restored runtime keep-set onto %s", user_yaml)


def _needs_template_upgrade(
    *,
    user_yaml: Path,
    user_builtin_rules: Path | None,
    package_yaml: Path | None,
    package_builtin_rules: Path | None,
    force_upgrade: bool,
) -> bool:
    """是否走升级 copy2。比包内模板哈希戳，不比用户 yaml 内容；版本号不是信号。"""
    if force_upgrade:
        return True
    if not user_yaml.is_file():
        return True
    if package_builtin_rules is not None and user_builtin_rules is not None:
        if package_builtin_rules.is_file() and not user_builtin_rules.is_file():
            return True
    stamp = read_template_stamp(user_yaml)
    if stamp is None:
        return True
    return stamp != compute_template_stamp(package_yaml, package_builtin_rules)


def _sync_copy_template(src: Path | None, dest: Path | None, *, force: bool) -> bool:
    if src is None or dest is None or not src.is_file():
        return False
    if force:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        logger.info("synced system file %s from %s", dest, src)
        return True
    return copy_if_missing_or_changed(src, dest)


def _fold_legacy_overlay_into_yaml(
    *,
    user_yaml: Path,
    overlay_yaml: Path,
    package_yaml: Path | None,
) -> bool:
    """把遗留 overlay 白名单写进 yaml 并删除 overlay。无 overlay 则 False。"""
    if not overlay_yaml.is_file():
        return False
    keep = snapshot_user_keep_set(
        user_yaml=user_yaml,
        overlay_yaml=overlay_yaml,
        package_yaml=package_yaml,
    )
    if keep:
        if not user_yaml.is_file():
            user_yaml.parent.mkdir(parents=True, exist_ok=True)
            user_yaml.write_text("{}\n", encoding="utf-8")
        restore_user_keep_set(user_yaml, keep)
    drop_legacy_overlay(overlay_yaml)
    return True


def sync_system_files_from_package(
    *,
    user_yaml: Path,
    overlay_yaml: Path,
    package_yaml: Path | None,
    user_builtin_rules: Path | None = None,
    package_builtin_rules: Path | None = None,
    force_upgrade: bool = False,
) -> bool:
    """升级才 copy2；白名单与 keep-set 写回 yaml。遗留 overlay 折进 yaml 后删除。

    重启（戳与当前**包内**模板哈希相同）：不覆盖 yaml；若还有 overlay 则折进 yaml。
    用户运行时改 yaml 不改戳，不会误判为升级。
    升级：快照 keep-set + 白名单 → copy2 → 写回 keep-set 与白名单 → 删 overlay。
    """
    need_upgrade = _needs_template_upgrade(
        user_yaml=user_yaml,
        user_builtin_rules=user_builtin_rules,
        package_yaml=package_yaml,
        package_builtin_rules=package_builtin_rules,
        force_upgrade=force_upgrade,
    )
    user_keep = snapshot_user_keep_set(
        user_yaml=user_yaml,
        overlay_yaml=overlay_yaml,
        package_yaml=package_yaml,
    )
    if need_upgrade:
        keep = snapshot_runtime_keep_set(user_yaml)
        _sync_copy_template(package_yaml, user_yaml, force=force_upgrade)
        _sync_copy_template(
            package_builtin_rules, user_builtin_rules, force=force_upgrade
        )
        restore_runtime_keep_set(user_yaml, keep)
        restore_user_keep_set(user_yaml, user_keep)
        write_template_stamp(user_yaml, package_yaml, package_builtin_rules)
        drop_legacy_overlay(overlay_yaml)
        return True
    folded = _fold_legacy_overlay_into_yaml(
        user_yaml=user_yaml,
        overlay_yaml=overlay_yaml,
        package_yaml=package_yaml,
    )
    return folded


def _package_builtin_rules_file() -> Path | None:
    res = get_package_resources_dir()
    if res is None:
        return None
    path = res / "builtin_rules.yaml"
    return path if path.is_file() else None


def maybe_fold_legacy_overlay() -> bool:
    """对当前 ``JIUWENSWARM_DATA_DIR`` 用户根做一次幂等同步。

    升级按戳 copy2；遗留 ``config.user.yaml`` 折进 yaml 后删除。
    ``JIUWENSWARM_SKIP_SYSTEM_FILE_SYNC``：桌面 Gateway 在 Agent 已覆盖并打回
    xiaoyi/GaussPD 之后不 copy2；仍把遗留 overlay 折进 yaml。
    """
    if "pytest" in sys.modules and not os.environ.get("JIUWENSWARM_ALLOW_OVERLAY_EXTRACT"):
        return False
    overlay = get_user_overlay_file()
    if os.environ.get("JIUWENSWARM_SKIP_SYSTEM_FILE_SYNC"):
        with _EXTRACT_LOCK:
            return _fold_legacy_overlay_into_yaml(
                user_yaml=get_config_file(),
                overlay_yaml=overlay,
                package_yaml=get_package_config_file(),
            )
    with _EXTRACT_LOCK:
        return sync_system_files_from_package(
            user_yaml=get_config_file(),
            overlay_yaml=overlay,
            package_yaml=get_package_config_file(),
            user_builtin_rules=get_builtin_rules_file(),
            package_builtin_rules=_package_builtin_rules_file(),
        )
