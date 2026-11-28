# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""agent_template / agent_group 适配器：统一导入导出链路对接扩展包管理。

导出定位与发布链路（marketplace.asset_publish_api.resolve_local_asset）
同口径：Hub 资产 UUID 先换算为本地包 id，专家解析已安装包目录
（local → built_in），专家团解析 local / built_in / bundled 发布目录；
指定 version 时必须与包 manifest 当前版本一致，否则按不存在处理。

嗅探只读包根 manifest.json 的 package_type —— 专家团包内嵌完整的成员
专家包，成员 manifest 不得参与类型判定。

导入复用包管理导入链路（结构校验 → 复制进 local/<id> → 市场登记）：

- 专家包导入强制 README.md，而创建链路不产出该文件：导出时缺失则
  随包补一份从 manifest 摘抄的 README.md（只进 zip，不回写安装目录），
  保证导出包可经上传创建入口往返导入；导入侧对历史导出包与第三方
  手工包仍补同款内容；
- 专家团保持既有「导入即安装」语义；
- 专家团要求包目录名与 manifest name 一致，不一致时先重命名为规范
  名（与包管理 zip 导入对根目录名的矫正一致）；
- 同名冲突一律拒绝覆盖：与内置/货架冲突收敛 BUILTIN_READ_ONLY，本地
  同 id 与专家团重名收敛 ALREADY_EXISTS；其余校验失败收敛
  INVALID_PACKAGE；企业租户未绑定属环境问题，保持内部异常交由入口层
  以 INTERNAL_ERROR 收敛。

pending_connectors 汇总包 manifest 的 ``mcps[].connector`` 声明（专家团
含各成员包）并经 unready 过滤；汇总失败不影响导入结果。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn

from jiuwenswarm.server.runtime import extension_package_manager as package_manager
from jiuwenswarm.server.runtime.assets.contract import AssetKind, validate_asset_id
from jiuwenswarm.server.runtime.assets.errors import AssetError, AssetErrorCode
from jiuwenswarm.server.runtime.assets.registry import (
    ExportSource,
    ImportOutcome,
    get_asset_adapter,
    register_asset_adapter,
)

logger = logging.getLogger(__name__)

# 包管理 kind 键（本地包目录名复数形式），与其存储/市场登记口径一致。
_TEMPLATE_PACKAGE_KIND = "agent_templates"
_GROUP_PACKAGE_KIND = "agent_groups"

# manifest 类型标记值（单数形式），与包创建/发布链路写入的一致。
_TEMPLATE_PACKAGE_TYPE = "agent_template"
_GROUP_PACKAGE_TYPE = "agent_group"

# 专家团内部错误码：租户未绑定属环境问题；重复为同 id / 重名冲突。
_GROUP_TENANT_REQUIRED = "AGENT_GROUP_TENANT_REQUIRED"
_GROUP_DUPLICATE = "AGENT_GROUP_DUPLICATE"

# 包管理导入链路的本地冲突消息标记（_assert_package_id_available /
# _assert_agent_group_name_available 口径），命中任一即收敛 ALREADY_EXISTS。
_DUPLICATE_MESSAGE_MARKERS = (
    "already exists in local",
    "display name already exists",
    "package conflict",
)


def _root_package_type(package_dir: Path) -> str:
    """读包根 manifest.json 的 package_type；缺失/损坏返回空串。"""
    manifest = package_manager.read_package_manifest(package_dir)
    if not isinstance(manifest, dict):
        return ""
    value = manifest.get("package_type")
    return value.strip() if isinstance(value, str) else ""


def _runtime_package_id(package_kind: str, local_id: str) -> str:
    """Hub 资产 UUID → 本地包 id；普通本地 id 原样返回。

    换算失败不提前拒绝，交由后续目录解析判定（确不存在时收敛
    NOT_FOUND），避免把存储层抖动放大成业务错误。
    """
    try:
        return package_manager.resolve_equipment_runtime_id(package_kind, local_id)
    except Exception:  # noqa: BLE001 换算失败按原始 id 继续解析
        return local_id


def _pick_display_text(value: Any) -> str:
    """显示名取值：字符串直接用；多语言 dict 按 zh → en → 其余值依次取。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for lang in ("zh", "en"):
            text = value.get(lang)
            if isinstance(text, str) and text.strip():
                return text.strip()
        for text in value.values():
            if isinstance(text, str) and text.strip():
                return text.strip()
    return ""


def _manifest_display_name(pkg_dir: Path) -> str:
    """包 manifest 的 UI 显示名（display_name → name 依次取首个可用值）。

    display_name 存在两种形态：字符串与多语言 dict（``{"zh": …, "en": …}``，
    专家与专家团的包创建链路各写一种），dict 形态按 zh 优先取值。
    本地创建的包目录名带 ``-<uuid>`` 内部后缀，直接用作下载文件名过长；
    显示名与界面列表一致，更适合做文件名主干。读取失败返回空串，
    由文件名构造回退 asset_id。
    """
    manifest = package_manager.read_package_manifest(pkg_dir)
    if not isinstance(manifest, dict):
        return ""
    for key in ("display_name", "name"):
        text = _pick_display_text(manifest.get(key))
        if text:
            return text
    return ""


def _readme_from_manifest(manifest: dict, fallback_title: str) -> str:
    """从 manifest 摘抄标题与描述，生成 README.md 内容。

    与专家团创建链路产出的 README 同款（``# 显示名`` + 描述段）：
    display_name / display_description 存在字符串与多语言 dict 两种
    形态，按 zh 优先取值，缺省依次回退 name / description；标题仍
    不可得时回退包目录名，描述缺失时只保留标题。导出侧随包补充与
    导入侧占位共用本函数，保证往返内容一致。
    """
    title = ""
    for key in ("display_name", "name"):
        title = _pick_display_text(manifest.get(key))
        if title:
            break
    if not title:
        title = fallback_title
    body = ""
    for key in ("display_description", "description"):
        body = _pick_display_text(manifest.get(key))
        if body:
            break
    if not body:
        return f"# {title}\n"
    return f"# {title}\n\n{body}\n"


def _export_source(pkg_dir: Path, version: str | None, *, label: str) -> ExportSource:
    """构造导出源；指定版本时校验与包 manifest 当前版本一致。

    本地创建的包 manifest 无 version 字段，此时仅允许不带版本的导出；
    带版本请求与实际版本不一致一律 NOT_FOUND（历史版本不可寻址）。
    """
    package_id = validate_asset_id(pkg_dir.name)
    actual = package_manager.read_manifest_version(pkg_dir)
    if version is not None and str(version).strip() != actual:
        raise AssetError(
            AssetErrorCode.NOT_FOUND,
            f"{label}指定版本不存在: {package_id}@{version}",
            details={"local_id": package_id, "version": version},
        )
    return ExportSource(
        source_dir=pkg_dir,
        asset_id=package_id,
        version=actual or None,
        display_name=_manifest_display_name(pkg_dir),
    )


def _raise_not_found(exc: Exception, local_id: str, *, label: str) -> NoReturn:
    """定位失败 → NOT_FOUND；专家团租户未绑定保持内部异常上抛。"""
    if getattr(exc, "code", "") == _GROUP_TENANT_REQUIRED:
        raise exc
    raise AssetError(
        AssetErrorCode.NOT_FOUND,
        f"{label}不存在: {local_id}",
        details={"local_id": local_id},
    ) from exc


def _require_root_manifest(package_dir: Path, package_type: str, *, label: str) -> dict:
    """导入前校验包根 manifest 存在且类型匹配（防显式 kind 误分发）。"""
    manifest = package_manager.read_package_manifest(package_dir)
    if not isinstance(manifest, dict):
        raise AssetError(
            AssetErrorCode.INVALID_PACKAGE,
            f"{label}包缺少有效的 manifest.json",
        )
    actual = manifest.get("package_type")
    if not isinstance(actual, str) or actual.strip() != package_type:
        raise AssetError(
            AssetErrorCode.INVALID_PACKAGE,
            f"包类型不匹配：期望 {package_type}，实际 {actual!r}",
        )
    return manifest


def _manifest_connector_names(manifest: dict) -> list[str]:
    """manifest ``mcps[].connector`` 名单，与包管理安装门禁读取口径一致。"""
    specs = manifest.get("mcps")
    if not isinstance(specs, list):
        return []
    names: list[str] = []
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        connector = spec.get("connector")
        if isinstance(connector, str) and connector.strip():
            names.append(connector.strip())
    return list(dict.fromkeys(names))


def _pending_connectors(manifests: list[dict]) -> tuple[str, ...]:
    """汇总多份 manifest 的连接器名并过滤未就绪项；失败不影响导入。"""
    names: list[str] = []
    for manifest in manifests:
        names.extend(_manifest_connector_names(manifest))
    names = list(dict.fromkeys(names))
    if not names:
        return ()
    try:
        return tuple(package_manager.unready_connectors(names))
    except Exception:  # noqa: BLE001 连接器状态读取失败仅降级为空名单
        logger.warning("[assets] unready connectors check failed", exc_info=True)
        return ()


def _reraise_import_failure(exc: Exception) -> NoReturn:
    """包管理导入异常 → 契约错误码。

    冲突判定依据包管理 ``_assert_package_id_available`` /
    ``_assert_agent_group_name_available`` / ``_install_package`` 的消息
    口径（``already exists in <root>`` / ``display name already exists`` /
    ``package conflict``）；专家团异常自带 ``.code``，按码兜底。
    """
    if getattr(exc, "code", "") == _GROUP_TENANT_REQUIRED:
        raise exc
    message = str(exc) or exc.__class__.__name__
    if "already exists in built_in" in message or "already exists in resources" in message:
        raise AssetError(AssetErrorCode.BUILTIN_READ_ONLY, message) from exc
    duplicated = any(marker in message for marker in _DUPLICATE_MESSAGE_MARKERS)
    if duplicated or getattr(exc, "code", "") == _GROUP_DUPLICATE:
        raise AssetError(AssetErrorCode.ALREADY_EXISTS, message) from exc
    raise AssetError(AssetErrorCode.INVALID_PACKAGE, message) from exc


def _ensure_template_readme(package_dir: Path, manifest: dict) -> None:
    """导入前缺失时补 README.md。

    包管理导入链路对专家包强制 README.md，而专家创建链路不产出该
    文件；导出链路已随包补充（见 :meth:`AgentTemplateAdapter.locate_export`），
    此处兜底覆盖历史导出包与第三方手工包，内容与导出侧同款
    （:func:`_readme_from_manifest`）。包目录为导入临时目录，写入
    不影响源资产。
    """
    readme = package_dir / "README.md"
    if readme.is_file():
        return
    readme.write_text(
        _readme_from_manifest(manifest, package_dir.name), encoding="utf-8"
    )


def _safe_group_package_id(raw_name: str) -> str:
    """重命名前校验 manifest name，杜绝以穿越路径作为目标目录名。"""
    try:
        return validate_asset_id(raw_name)
    except ValueError as exc:
        raise AssetError(
            AssetErrorCode.INVALID_PACKAGE,
            f"专家团包名不合法: {raw_name!r}",
        ) from exc


def _group_manifests(package_dir: Path) -> list[dict]:
    """专家团根 manifest + 各成员包 manifest（``agents/*/manifest.json``）。

    成员 manifest 仅用于连接器汇总，不参与类型嗅探。
    """
    manifests: list[dict] = []
    root = package_manager.read_package_manifest(package_dir)
    if isinstance(root, dict):
        manifests.append(root)
    agents_dir = package_dir / "agents"
    if not agents_dir.is_dir():
        return manifests
    for member_dir in sorted(agents_dir.iterdir()):
        if not member_dir.is_dir():
            continue
        member = package_manager.read_package_manifest(member_dir)
        if isinstance(member, dict):
            manifests.append(member)
    return manifests


class AgentTemplateAdapter:
    """专家适配器：已安装包目录导出 / 包管理导入链路落库（导入不安装）。"""

    kind = AssetKind.AGENT_TEMPLATE

    @staticmethod
    def locate_export(local_id: str, version: str | None) -> ExportSource:
        package_id = _runtime_package_id(_TEMPLATE_PACKAGE_KIND, local_id)
        try:
            pkg_dir = package_manager.resolve_agent_template_dir(package_id)
        except Exception as exc:  # noqa: BLE001 解析失败统一收敛 NOT_FOUND
            _raise_not_found(exc, local_id, label="专家")
        source = _export_source(pkg_dir, version, label="专家")
        # 导入链路对专家包强制 README.md，而创建链路不产出该文件：缺失
        # 时随包补一份按 manifest 摘抄的 README（只进 zip，不回写安装
        # 目录），保证导出包可经上传创建入口导回；已有 README 的包
        # （Hub 安装、用户自建）保持原样不覆盖。
        if not (pkg_dir / "README.md").is_file():
            manifest = package_manager.read_package_manifest(pkg_dir)
            readme = _readme_from_manifest(
                manifest if isinstance(manifest, dict) else {}, pkg_dir.name
            )
            source = replace(
                source, extra_files={"README.md": readme.encode("utf-8")}
            )
        return source

    @staticmethod
    def sniff_package(package_dir: Path) -> bool:
        return _root_package_type(package_dir) == _TEMPLATE_PACKAGE_TYPE

    @staticmethod
    def import_package(package_dir: Path) -> ImportOutcome:
        manifest = _require_root_manifest(package_dir, _TEMPLATE_PACKAGE_TYPE, label="专家")
        _ensure_template_readme(package_dir, manifest)
        try:
            result = package_manager.import_agent_template({"path": str(package_dir)})
        except Exception as exc:  # noqa: BLE001 统一收敛为契约错误码
            _reraise_import_failure(exc)
        package_id = str(result.get("id") or package_dir.name)
        return ImportOutcome(
            asset_id=package_id,
            pending_connectors=_pending_connectors([manifest]),
        )


class AgentGroupAdapter:
    """专家团适配器：发布目录导出 / 导入即安装（与既有导入链路一致）。"""

    kind = AssetKind.AGENT_GROUP

    @staticmethod
    def locate_export(local_id: str, version: str | None) -> ExportSource:
        package_id = _runtime_package_id(_GROUP_PACKAGE_KIND, local_id)
        try:
            pkg_dir = package_manager.resolve_agent_group_publish_dir(package_id)
        except Exception as exc:  # noqa: BLE001 解析失败统一收敛 NOT_FOUND
            _raise_not_found(exc, local_id, label="专家团")
        return _export_source(pkg_dir, version, label="专家团")

    @staticmethod
    def sniff_package(package_dir: Path) -> bool:
        # 只认包根标记：成员专家包的 manifest 位于 agents/<id>/ 下，
        # 不会落入本判定。
        return _root_package_type(package_dir) == _GROUP_PACKAGE_TYPE

    @staticmethod
    def import_package(package_dir: Path) -> ImportOutcome:
        manifest = _require_root_manifest(package_dir, _GROUP_PACKAGE_TYPE, label="专家团")
        raw_name = manifest.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise AssetError(AssetErrorCode.INVALID_PACKAGE, "专家团包 manifest 缺少 name")
        package_id = _safe_group_package_id(raw_name.strip())
        # 包管理导入链路要求专家团包目录名与 manifest name 一致；
        # 重命名发生在导入临时目录内，返回后随工作目录一并销毁。
        if package_dir.name != package_id:
            canonical_dir = package_dir.with_name(package_id)
            package_dir.rename(canonical_dir)
            package_dir = canonical_dir
        try:
            result = package_manager.import_agent_group({"path": str(package_dir)})
        except Exception as exc:  # noqa: BLE001 统一收敛为契约错误码
            _reraise_import_failure(exc)
        imported_id = str(result.get("id") or package_id)
        return ImportOutcome(
            asset_id=imported_id,
            pending_connectors=_pending_connectors(_group_manifests(package_dir)),
        )


_REGISTER_LOCK = threading.Lock()


def register_agent_adapters() -> None:
    """注册 agent_template / agent_group 适配器；重复调用幂等，不替换既有注册。

    由本包 ``install_default_adapters`` 统一装配（编排层模块加载期执行，
    覆盖 WS / HTTP 两条入口与全部部署形态）。
    """
    with _REGISTER_LOCK:
        for adapter in (AgentTemplateAdapter(), AgentGroupAdapter()):
            if get_asset_adapter(adapter.kind) is None:
                register_asset_adapter(adapter)
