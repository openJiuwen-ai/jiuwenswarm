# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Gateway 落盘清单：业务 name → FileLayout / DbLayout。

按落盘形态分组（不是按业务域）：

- YAML + DB：``config.yaml`` 片段 ↔ 同名表（双端同构）
- 仅 YAML：``config.yaml`` 片段，无企业表（personal-only）
- JSON + DB：独立 JSON 文件 ↔ 同名表
- 仅 DB：企业专属表，无 file
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from gateway.storage.registry.store_registry import (
    DbLayout,
    FileLayout,
    StoreLayout,
    StoreRegistry,
)

# name → (yaml_pointer, key_fields, yaml_scalar_field)
_YamlSectionSpec = tuple[str, tuple[str, ...], str]
# name → (rel_path, shape, key_fields)
_JsonAndDbSpec = tuple[str, Literal["map", "list"], tuple[str, ...]]


def _yaml_and_db_section(
    table: str,
    pointer: str,
    *,
    config_file: Path | None,
    key_fields: tuple[str, ...] = (),
    yaml_scalar_field: str = "",
) -> StoreLayout:
    """形态：YAML 片段 + 同名 DB 表。"""
    file: FileLayout | None = None
    if config_file is not None:
        file = FileLayout(
            path=str(Path(config_file)),
            format="yaml",
            yaml_pointer=pointer,
            key_fields=key_fields,
            yaml_scalar_field=yaml_scalar_field,
        )
    return StoreLayout(file=file, db=DbLayout(table=table))


def _yaml_only_section(
    pointer: str,
    *,
    config_file: Path | None,
    key_fields: tuple[str, ...] = (),
    yaml_scalar_field: str = "",
) -> StoreLayout | None:
    """形态：仅 YAML 片段（personal-only，``db=None``）。

    ``config_file`` 为 None（enterprise 装配）时不注册。
    """
    if config_file is None:
        return None
    return StoreLayout(
        file=FileLayout(
            path=str(Path(config_file)),
            format="yaml",
            yaml_pointer=pointer,
            key_fields=key_fields,
            yaml_scalar_field=yaml_scalar_field,
        ),
        db=None,
    )


def _json_and_db(
    table: str,
    rel: str,
    *,
    persistent_root: Path | None,
    shape: Literal["map", "list"] = "map",
    key_fields: tuple[str, ...] = (),
) -> StoreLayout:
    """形态：独立 JSON 文件 + 同名 DB 表。"""
    file: FileLayout | None = None
    if persistent_root is not None:
        file = FileLayout(
            path=str(persistent_root / rel),
            format="json",
            shape=shape,
            key_fields=key_fields,
        )
    return StoreLayout(file=file, db=DbLayout(table=table))


def _json_only(
    rel: str,
    *,
    persistent_root: Path | None,
    shape: Literal["map", "list"] = "map",
    key_fields: tuple[str, ...] = (),
) -> StoreLayout | None:
    """形态：仅个人版独立 JSON；企业装配时不注册。"""
    if persistent_root is None:
        return None
    return StoreLayout(
        file=FileLayout(
            path=str(persistent_root / rel),
            format="json",
            shape=shape,
            key_fields=key_fields,
        ),
        db=None,
    )


def _json_and_db_at_path(
    table: str,
    path: Path,
    *,
    shape: Literal["map", "list"] = "map",
    key_fields: tuple[str, ...] = (),
) -> StoreLayout:
    """JSON 文件使用装配层提供的绝对路径（兼容既有落盘位置）。"""
    return StoreLayout(
        file=FileLayout(
            path=str(path.resolve()),
            format="json",
            shape=shape,
            key_fields=key_fields,
        ),
        db=DbLayout(table=table),
    )


def _db_table(table: str) -> StoreLayout:
    """形态：仅企业 DB 表（无 file）。"""
    return StoreLayout(db=DbLayout(table=table))


# ---------------------------------------------------------------------------
# 落盘清单（按形态；store name 勿与企业 catalog 漂移）
# ---------------------------------------------------------------------------

_YAML_AND_DB_SECTIONS: dict[str, _YamlSectionSpec] = {
    "channel_config": ("/channels", ("id",), ""),
    "logging_config": ("/logging", (), ""),
    "memory_config": ("/memory", (), ""),
}

_YAML_ONLY_SECTIONS: dict[str, _YamlSectionSpec] = {
    # 企业策略走 permissions_template 槽位，不再落实例级 permissions_config 表
    "permissions_config": ("/permissions", (), ""),
}

_JSON_ONLY_STORES: dict[str, _JsonAndDbSpec] = {}

_JSON_AND_DB_STORES: dict[str, _JsonAndDbSpec] = {
    "session_map": ("session_map.json", "map", ("identity_key",)),
}


def _legacy_gateway_cron_job_layout(path_template: str | None) -> StoreLayout:
    """Gateway cron：沿用 ``gateway/cron/service_*/agent_*/cron_jobs.json`` 与 CronJobStore 包装格式。"""
    file: FileLayout | None = None
    if path_template is not None:
        file = FileLayout(
            path=path_template,
            format="json",
            shape="list",
            key_fields=("id",),
            json_document_key="jobs",
        )
    return StoreLayout(file=file, db=DbLayout(table="cron_job"))


def _enterprise_record_store_names() -> tuple[str, ...]:
    """企业专属表名。catalog 无法导入时返回空。"""
    try:
        from gateway.config.enterprise.catalog import (
            ENTERPRISE_RECORD_STORE_NAMES,
        )
    except ImportError:
        return ()
    return tuple(ENTERPRISE_RECORD_STORE_NAMES)


def _build_layouts(
    *,
    persistent_root: Path | None,
    config_file: Path | None,
    session_map_file: Path | None = None,
    cron_jobs_path_template: str | None = None,
) -> dict[str, StoreLayout]:
    """按形态工厂组装全部业务 name → StoreLayout。

    ``cron_job`` 一定带数据库表。没给文件路径时只挂表，企业版启动读这张表就靠它。
    给了路径时，同时保留个人版那份 JSON 文件布局。
    """
    layouts: dict[str, StoreLayout] = {}

    layouts["cron_job"] = _legacy_gateway_cron_job_layout(cron_jobs_path_template)

    for name, (rel, shape, key_fields) in _JSON_ONLY_STORES.items():
        layout = _json_only(
            rel,
            persistent_root=persistent_root,
            shape=shape,
            key_fields=key_fields,
        )
        if layout is not None:
            layouts[name] = layout

    for name, (rel, shape, key_fields) in _JSON_AND_DB_STORES.items():
        if name == "session_map":
            continue
        layouts[name] = _json_and_db(
            name,
            rel,
            persistent_root=persistent_root,
            shape=shape,
            key_fields=key_fields,
        )

    if session_map_file is not None:
        layouts["session_map"] = _json_and_db_at_path(
            "session_map",
            session_map_file,
            shape="map",
            key_fields=("identity_key",),
        )
    else:
        layouts["session_map"] = _json_and_db(
            "session_map",
            "session_map.json",
            persistent_root=persistent_root,
            shape="map",
            key_fields=("identity_key",),
        )

    for name, (pointer, key_fields, yaml_scalar_field) in _YAML_AND_DB_SECTIONS.items():
        layouts[name] = _yaml_and_db_section(
            name,
            pointer,
            config_file=config_file,
            key_fields=key_fields,
            yaml_scalar_field=yaml_scalar_field,
        )

    for name, (pointer, key_fields, yaml_scalar_field) in _YAML_ONLY_SECTIONS.items():
        layout = _yaml_only_section(
            pointer,
            config_file=config_file,
            key_fields=key_fields,
            yaml_scalar_field=yaml_scalar_field,
        )
        if layout is not None:
            layouts[name] = layout

    for table in _enterprise_record_store_names():
        if table == "cron_job":
            continue
        if persistent_root is not None and table in _JSON_ONLY_STORES:
            continue
        layouts[table] = _db_table(table)

    return layouts


def build_gateway_store_registry(
    *,
    persistent_root: Path | None = None,
    config_file: Path | None = None,
    session_map_file: Path | None = None,
    cron_jobs_path_template: str | None = None,
) -> StoreRegistry:
    """装配 name 对应的落盘布局。

    personal 传入绝对 ``persistent_root`` / ``config_file``；
    ``session_map_file`` 默认与 ``LocalSessionStorage`` 相同（``.checkpoint/session_map.json``）。
    ``cron_jobs_path_template`` 有值时，``cron_job`` 同时挂 JSON 文件；没有则只挂数据库表。
    enterprise 可不传（file 为空，YAML-only name 不注册，只挂 DB）。
    """
    registry = StoreRegistry()
    registry.register_many(
        _build_layouts(
            persistent_root=persistent_root,
            config_file=config_file,
            session_map_file=session_map_file,
            cron_jobs_path_template=cron_jobs_path_template,
        )
    )
    return registry


__all__ = ["build_gateway_store_registry"]
