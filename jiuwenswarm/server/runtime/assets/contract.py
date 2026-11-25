# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""资产统一导入导出的契约层。

- kind 枚举与现有发布体系保持一致，共五类，禁止自造命名；
- 导出文件名统一 ``<id>[-<version>].zip``，非法路径字符安全清洗；
  ``<id>`` 必须与压缩包顶层目录名严格一致，不含路径分隔符或特殊字符；
- ``assets.export`` 入参 ``{kind, local_id, version?}``（``items?``
  协议层预留批量导出），出参 ``{download_url, filename, size_bytes, sha256}``；
  ``assets.import`` 入参 ``{path, kind?}``，出参 ``{kind, id, pending_connectors?}``。

本模块只依赖标准库：入口 handler、各 kind 适配器与前端契约测试共享同一份
定义，且不引入循环依赖。解析失败一律抛 ``ValueError``，由入口层收敛为
标准错误码（见 ``errors.py``），契约层自身不抛业务异常。
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import PurePosixPath
from typing import Any


class AssetKind(str, Enum):
    """资产类型枚举 —— 固定五类、与现有发布体系一致，禁止新增值。

    出参 payload、包内目录名等对外可见位置一律使用这里的规范值。
    """

    SKILL = "skill"
    SKILLPACK = "skillpack"
    AGENT_TEMPLATE = "agent_template"
    AGENT_GROUP = "agent_group"
    PLUGIN = "plugin"


# 入口容忍的等价写法 → 规范 kind。只兼容两类既有写法，避免调用方在
# "本地包目录名（复数）/ Hub 发布类型（连字符）" 之间换算出错：
# - 本地包目录名（extension_package_manager 与各前端 service 长期用复数）；
# - Hub 发布体系连字符类型（marketplace.hub_asset_type_adapter 的契约表）。
_KIND_ALIASES: dict[str, AssetKind] = {
    "skills": AssetKind.SKILL,
    "skillpacks": AssetKind.SKILLPACK,
    "agent_templates": AssetKind.AGENT_TEMPLATE,
    "agent-template": AssetKind.AGENT_TEMPLATE,
    "agent_groups": AssetKind.AGENT_GROUP,
    "agent-group": AssetKind.AGENT_GROUP,
    "plugins": AssetKind.PLUGIN,
    "plugin_packages": AssetKind.PLUGIN,
    "agent-plugin": AssetKind.PLUGIN,
}


def normalize_asset_kind(raw: Any) -> AssetKind:
    """把各种等价写法收敛为规范 kind；未知类型抛 ``ValueError``。"""
    if isinstance(raw, AssetKind):
        return raw
    if not isinstance(raw, str):
        raise ValueError(f"unsupported asset kind: {raw!r}")
    key = raw.strip()
    try:
        return AssetKind(key)
    except ValueError:
        pass
    lowered = key.lower()
    try:
        return AssetKind(lowered)
    except ValueError:
        pass
    alias = _KIND_ALIASES.get(lowered)
    if alias is None and lowered.endswith("s"):
        # 连字符写法的复数（agent-templates 等）
        alias = _KIND_ALIASES.get(lowered[:-1])
    if alias is None:
        raise ValueError(f"unsupported asset kind: {raw!r}")
    return alias


# 文件名/资产 id 的不安全字符：路径分隔符、Windows 保留字符与控制字符。
# 只影响下载文件名，不改变包内 <id> 目录名（那由 validate_asset_id 把关）。
_FILENAME_UNSAFE_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def validate_asset_id(asset_id: Any) -> str:
    """校验将作为压缩包顶层目录名的资产 id。

    ``<id>`` 必须与压缩包顶层目录名严格一致：非空、不含路径分隔符、
    文件名特殊字符（``\\ / : * ? " < > |``）与控制字符、不是 ``.`` / ``..``；
    返回去除首尾空白的 id。特殊字符一并拒绝，保证 id 与下载文件名
    一一对应，而不是靠 ``sanitize_export_stem`` 做多对一的有损清洗。
    """
    if not isinstance(asset_id, str):
        raise ValueError(f"invalid asset id: {asset_id!r}")
    value = asset_id.strip()
    if not value or value in {".", ".."}:
        raise ValueError(f"invalid asset id: {asset_id!r}")
    if "/" in value or "\\" in value:
        raise ValueError(f"asset id must not contain path separators: {asset_id!r}")
    if re.search(r'[*?:"<>|]', value):
        raise ValueError(f"asset id must not contain special characters: {asset_id!r}")
    if any(ord(ch) < 32 for ch in value):
        raise ValueError(f"asset id must not contain control characters: {asset_id!r}")
    return value


def sanitize_export_stem(value: str) -> str:
    """把任意字符串（id / version）清洗成安全的下载文件名主干。"""
    cleaned = _FILENAME_UNSAFE_RE.sub("_", str(value))
    return cleaned.strip(" ._")


def build_export_filename(asset_id: str, version: str | None = None) -> str:
    """构造导出文件名 ``<id>[-<version>].zip``。

    id 先校验，再做文件名清洗；清洗后为空则拒绝导出，
    避免产出 ``.zip`` / ``-1.0.zip`` 这类无名文件。
    """
    stem = sanitize_export_stem(validate_asset_id(asset_id))
    if not stem:
        raise ValueError(f"asset id sanitizes to an empty filename: {asset_id!r}")
    if version is not None:
        version_stem = sanitize_export_stem(str(version).strip())
        if not version_stem:
            raise ValueError(f"invalid asset version: {version!r}")
        stem = f"{stem}-{version_stem}"
    return f"{stem}.zip"


def parse_export_params(params: Any) -> dict[str, Any]:
    """校验并归一化 ``assets.export`` 入参，返回 ``{kind, local_id, version?}``。

    ``kind`` 归一为 :class:`AssetKind`；``local_id`` 经
    :func:`validate_asset_id` 校验 —— 适配器按 ``(kind, local_id)`` 定位
    本地包目录，路径穿越片段（``../`` / 分隔符）必须在解析阶段拒绝，
    而不是推迟到文件名构造或目录拼接时才暴露。``items`` 是协议预留的
    批量导出字段，一期未实现——显式拒绝而不是静默忽略，避免调用方
    误以为批量已生效。
    """
    if not isinstance(params, dict):
        raise ValueError("assets.export params must be an object")
    if params.get("items") is not None:
        raise ValueError("batch export (items) is reserved and not supported yet")
    kind = normalize_asset_kind(params.get("kind"))
    parsed: dict[str, Any] = {
        "kind": kind,
        "local_id": validate_asset_id(params.get("local_id")),
    }
    version = params.get("version")
    if version is not None:
        if not isinstance(version, str) or not version.strip():
            raise ValueError("assets.export version must be a non-empty string when provided")
        parsed["version"] = version.strip()
    return parsed


def _validate_import_path(path: str) -> None:
    """导入包路径的字符串层安全校验（契约层能做的部分）。

    ``path`` 是上传服务签发的暂存路径直引用用，含 ``..`` 段、反斜杠
    分隔符变体或控制字符的取值一律是非法入参，在解析阶段拒绝。绝对路径
    允许通过 —— 是否落在受控上传根内由导入服务在打开前 resolve 收敛
    （分层防御：契约层挡语法穿越，服务层挡越权落点）。
    """
    if "\\" in path or any(ord(ch) < 32 for ch in path):
        raise ValueError(f"assets.import path contains unsafe characters: {path!r}")
    if ".." in PurePosixPath(path).parts:
        raise ValueError(
            f"assets.import path must not contain '..' segments: {path!r}"
        )


def parse_import_params(params: Any) -> dict[str, Any]:
    """校验并归一化 ``assets.import`` 入参，返回 ``{path, kind?}``。

    ``path`` 先做字符串层安全校验（见 :func:`_validate_import_path`）；
    ``kind`` 缺省时由导入服务从包内容嗅探（``SKILL.md`` → skill；
    ``manifest.json`` 的 ``package_type`` → 对应类型）。
    """
    if not isinstance(params, dict):
        raise ValueError("assets.import params must be an object")
    raw_path = params.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("assets.import requires a non-empty path")
    path = raw_path.strip()
    _validate_import_path(path)
    parsed: dict[str, Any] = {"path": path}
    if params.get("kind") is not None:
        parsed["kind"] = normalize_asset_kind(params.get("kind"))
    return parsed
