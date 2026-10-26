# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""文件 PersistentStore：按注入的布局注册表读写 JSON / YAML。"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import portalocker
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from jiuwenswarm.gateway.storage.errors import (
    DuplicateRecordError,
    StorageUnavailableError,
)
from jiuwenswarm.gateway.storage.registry.store_registry import (
    FileLayout,
    StoreLayout,
    StoreRegistry,
)

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_FILE_LOCK_TIMEOUT_SEC = 10.0
_UNSAFE_PATH = re.compile(r"[\\/]|\.\.")


def _plain(value: Any) -> Any:
    """把 YAML/JSON 节点收成普通 dict/list，去掉 ruamel 包装。"""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _matches(row: dict[str, Any], key: dict[str, Any]) -> bool:
    """判断记录是否满足等值条件。"""
    return all(row.get(k) == v for k, v in key.items())


def _apply_order(rows: list[dict[str, Any]], order_by: str) -> list[dict[str, Any]]:
    """按 ``order_by``（``field`` / ``field DESC``）原地排序。"""
    if not order_by:
        return rows
    reverse = " DESC" in order_by.upper()
    field = order_by.replace(" DESC", "").replace(" desc", "")
    field = field.replace(" ASC", "").replace(" asc", "").strip()
    if not field:
        return rows
    rows.sort(
        key=lambda item: (item.get(field) is None, item.get(field)),
        reverse=reverse,
    )
    return rows


def _pointer_parts(pointer: str) -> list[str]:
    """把 ``/channels`` 这类 YAML pointer 拆成路径段。"""
    if not pointer or pointer == "/":
        return []
    return [part for part in str(pointer).split("/") if part]


def _identity(layout: FileLayout, record: dict[str, Any]) -> dict[str, Any]:
    """从记录里抽出主键字段，用于判重。"""
    if layout.key_fields:
        return {field: record[field] for field in layout.key_fields if field in record}
    return {}


def _inject_map_key(layout: FileLayout, map_key: str, value: Any) -> dict[str, Any]:
    """mapping 的 key 写回 record（如 channels.web → ``id=web``）。"""
    record = dict(value) if isinstance(value, dict) else {"value": value}
    if layout.key_fields:
        record.setdefault(layout.key_fields[0], map_key)
    return record


def _strip_map_key(layout: FileLayout, record: dict[str, Any]) -> dict[str, Any]:
    """写回 mapping 前去掉主键字段，避免和 map key 重复。"""
    if not layout.key_fields:
        return dict(record)
    out = dict(record)
    out.pop(layout.key_fields[0], None)
    return out


def _validate_path_value(value: Any) -> str:
    """校验路径占位符，拒绝 ``..`` 和路径分隔符。"""
    text = str(value)
    if not text or _UNSAFE_PATH.search(text):
        raise StorageUnavailableError(f"unsafe path substitution: {text!r}")
    return text


class FilePersistentBackend:
    """文件 backend。"""

    def __init__(
        self,
        *,
        registry: StoreRegistry,
        file_lock_timeout: float = _FILE_LOCK_TIMEOUT_SEC,
        on_write: Callable[[Path], None] | None = None,
    ) -> None:
        """``FileLayout.path`` 须为绝对路径或绝对模板；本 backend 只替换 ``{field}``。"""
        self._registry = registry
        self._file_lock_timeout = float(file_lock_timeout)
        self._on_write = on_write
        self._lock = asyncio.Lock()

    async def ensure_ready(self) -> None:
        """文件 backend 无连接；写时再创建父目录。"""
        return None

    async def close(self) -> None:
        """文件 backend 无连接，无需释放。"""
        return None

    def _layout(self, name: str) -> tuple[StoreLayout, FileLayout]:
        """按 name 取文件布局；没有 file 布局则失败。"""
        layout = self._registry.get(name)
        if layout is None or layout.file is None:
            raise StorageUnavailableError(
                f"name {name!r} has no file layout"
            )
        return layout, layout.file

    @staticmethod
    def _as_absolute_path(rendered: str) -> Path:
        """布局 path 必须是绝对路径（或替换占位符后仍为绝对路径）。"""
        path = Path(rendered)
        if not path.is_absolute():
            raise StorageUnavailableError(
                f"FileLayout.path must be absolute, got {rendered!r}"
            )
        return path

    def _resolve_file(self, file_layout: FileLayout, values: dict[str, Any]) -> Path:
        """把布局 path 里的 ``{field}`` 换成实值；不拼接目录。"""
        raw = file_layout.path
        self._as_absolute_path(raw)
        placeholders = _PLACEHOLDER.findall(raw)
        mapping = {
            name: _validate_path_value(values[name])
            for name in placeholders
            if name in values
        }
        missing = [name for name in placeholders if name not in mapping]
        if missing:
            raise StorageUnavailableError(
                f"path {raw!r} missing fields: {', '.join(missing)}"
            )
        rendered = raw.format(**mapping) if mapping else raw
        return self._as_absolute_path(rendered)

    def _glob_files(self, file_layout: FileLayout, values: dict[str, Any]) -> list[Path]:
        """路径占位符未知时用 ``*`` 展开，列出已有文件。"""
        raw = file_layout.path
        self._as_absolute_path(raw)
        placeholders = _PLACEHOLDER.findall(raw)

        def _repl(match: re.Match[str]) -> str:
            """已知占位符换成实值，未知换成 glob ``*``。"""
            name = match.group(1)
            if name in values:
                return _validate_path_value(values[name])
            return "*"

        pattern = _PLACEHOLDER.sub(_repl, raw) if placeholders else raw
        path = self._as_absolute_path(pattern)
        matches = sorted(Path(path.anchor).glob(str(path.relative_to(path.anchor))))
        return [item for item in matches if item.is_file()]

    @staticmethod
    def _yaml() -> YAML:
        """round-trip YAML，尽量保留引号和缩进。"""
        rt = YAML()
        rt.preserve_quotes = True
        rt.default_flow_style = False
        rt.indent(mapping=2, sequence=4, offset=2)
        rt.width = 4096
        return rt

    @staticmethod
    def _atomic_replace(tmp: Path, dest: Path) -> None:
        """先写临时文件再 replace，避免写到一半损坏。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp.replace(dest)

    @staticmethod
    def _lock_path(path: Path) -> Path:
        """同目录旁路锁文件路径。"""
        return path.with_name(path.name + ".lock")

    def _with_file_lock(self, path: Path, fn: Any) -> Any:
        """持有文件锁后执行同步写操作。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(str(self._lock_path(path)), timeout=self._file_lock_timeout):
            return fn()

    @staticmethod
    def _load_json(path: Path, layout: FileLayout) -> list[dict[str, Any]]:
        """读 JSON：list 原样；map + key_fields 则把 mapping 拆成多条 record。"""
        if not path.exists():
            return []
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, list):
            return [dict(item) for item in data if isinstance(item, dict)]
        doc_key = str(layout.json_document_key or "").strip()
        if isinstance(data, dict) and doc_key:
            wrapped = data.get(doc_key)
            if isinstance(wrapped, list):
                return [dict(item) for item in wrapped if isinstance(item, dict)]
            return []
        if isinstance(data, dict) and layout.shape == "map" and layout.key_fields:
            field = layout.key_fields[0]
            records: list[dict[str, Any]] = []
            for map_key, value in data.items():
                record = _inject_map_key(layout, str(map_key), value)
                record[field] = str(map_key)
                records.append(record)
            return records
        return []

    def _dump_json(self, path: Path, layout: FileLayout, records: list[dict[str, Any]]) -> None:
        """写 JSON；map 且单主键时写回 mapping，否则写 list。"""
        if (
            layout.shape == "map"
            and layout.key_fields
            and len(layout.key_fields) == 1
        ):
            field = layout.key_fields[0]
            payload: Any = {
                str(row[field]): _strip_map_key(layout, row)
                for row in records
                if field in row
            }
        elif layout.shape == "list" and str(layout.json_document_key or "").strip():
            payload = {
                "version": 1,
                str(layout.json_document_key).strip(): records,
            }
        else:
            payload = records
        tmp = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            self._atomic_replace(tmp, path)
        except BaseException:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            raise

    def _load_yaml_root(self, path: Path) -> Any:
        """解析整个 YAML 文件；文件不存在则返回空 mapping。"""
        if not path.exists():
            return CommentedMap()
        with open(path, "r", encoding="utf-8") as handle:
            data = self._yaml().load(handle)
        return CommentedMap() if data is None else data

    def _dump_yaml_root(self, path: Path, root: Any) -> None:
        """原子写回整份 YAML，并触发 ``on_write``（如清配置缓存）。"""
        tmp = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.yaml.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                self._yaml().dump(root, handle)
            self._atomic_replace(tmp, path)
        except BaseException:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            raise
        if self._on_write is not None:
            self._on_write(path)

    @staticmethod
    def _yaml_node(root: Any, parts: list[str], *, create: bool) -> Any:
        """沿 pointer 段下钻；``create=True`` 时补齐缺失的 mapping。"""
        if not parts:
            return root
        cursor = root
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            if not isinstance(cursor, dict):
                raise StorageUnavailableError("yaml pointer parent is not a mapping")
            if part not in cursor:
                if not create:
                    return None
                cursor[part] = CommentedMap()
            if last:
                return cursor[part]
            cursor = cursor[part]
        return cursor

    @staticmethod
    def _set_yaml_node(root: Any, parts: list[str], value: Any) -> Any:
        """把 value 写到 pointer 指向的节点，必要时创建中间 mapping。"""
        if not parts:
            return value
        cursor = root
        for part in parts[:-1]:
            if not isinstance(cursor, dict):
                raise StorageUnavailableError("yaml pointer parent is not a mapping")
            child = cursor.get(part)
            if not isinstance(child, dict):
                child = CommentedMap()
                cursor[part] = child
            cursor = child
        cursor[parts[-1]] = value
        return root

    def _load_yaml_records(self, path: Path, layout: FileLayout) -> list[dict[str, Any]]:
        """按 yaml_pointer 取片段，再按 key_fields 转成 record 列表。"""
        root = self._load_yaml_root(path)
        node = self._yaml_node(root, _pointer_parts(layout.yaml_pointer), create=False)
        if node is None:
            return []
        if layout.key_fields:
            if not isinstance(node, dict):
                return []
            return [
                _inject_map_key(layout, str(map_key), value)
                for map_key, value in node.items()
            ]
        scalar_field = str(layout.yaml_scalar_field or "").strip()
        if scalar_field:
            if isinstance(node, dict):
                return [_plain(node)]
            return [{scalar_field: _plain(node)}]
        if isinstance(node, dict):
            return [_plain(node)]
        if isinstance(node, list):
            return [_plain(item) for item in node if isinstance(item, dict)]
        return []

    def _write_yaml_records(
        self,
        path: Path,
        layout: FileLayout,
        records: list[dict[str, Any]],
    ) -> None:
        """只改 pointer 对应片段，其余 YAML 原样写回。"""
        root = self._load_yaml_root(path)
        parts = _pointer_parts(layout.yaml_pointer)
        if layout.key_fields:
            node = CommentedMap()
            field = layout.key_fields[0]
            for record in records:
                if field not in record:
                    continue
                node[str(record[field])] = _strip_map_key(layout, record)
            root = self._set_yaml_node(root, parts, node)
        elif len(records) == 0:
            if parts:
                parent_parts, leaf = parts[:-1], parts[-1]
                parent = self._yaml_node(root, parent_parts, create=True)
                if isinstance(parent, dict):
                    parent.pop(leaf, None)
            else:
                root = CommentedMap()
        elif len(records) == 1:
            scalar_field = str(layout.yaml_scalar_field or "").strip()
            if scalar_field:
                root = self._set_yaml_node(
                    root, parts, records[0].get(scalar_field)
                )
            else:
                root = self._set_yaml_node(root, parts, records[0])
        else:
            seq = CommentedSeq(records)
            root = self._set_yaml_node(root, parts, seq)
        self._dump_yaml_root(path, root)

    def _load_records(self, path: Path, layout: FileLayout) -> list[dict[str, Any]]:
        """按 layout.format 分发到 YAML 或 JSON 读取。"""
        if layout.format == "yaml":
            return self._load_yaml_records(path, layout)
        return self._load_json(path, layout)

    def _save_records(
        self,
        path: Path,
        layout: FileLayout,
        records: list[dict[str, Any]],
    ) -> None:
        """按 layout.format 分发到 YAML 或 JSON 写入。"""
        if layout.format == "yaml":
            self._write_yaml_records(path, layout, records)
            return
        self._dump_json(path, layout, records)

    def _read_files(
        self,
        file_layout: FileLayout,
        values: dict[str, Any],
    ) -> list[tuple[Path, list[dict[str, Any]]]]:
        """路径完整则读单文件；占位符缺失则 glob 多文件后合并。"""
        placeholders = _PLACEHOLDER.findall(file_layout.path)
        missing = [name for name in placeholders if name not in values]
        if not placeholders or not missing:
            path = self._resolve_file(file_layout, values)
            return [(path, self._load_records(path, file_layout))]
        packed: list[tuple[Path, list[dict[str, Any]]]] = []
        for path in self._glob_files(file_layout, values):
            packed.append((path, self._load_records(path, file_layout)))
        return packed

    @staticmethod
    def _filter_page(
        rows: list[dict[str, Any]],
        *,
        filters: dict[str, Any] | None,
        order_by: str,
        limit: int | None,
        offset: int,
    ) -> list[dict[str, Any]]:
        """等值过滤后再排序、分页。"""
        key = dict(filters or {})
        if key:
            rows = [row for row in rows if _matches(row, key)]
        rows = _apply_order(rows, order_by)
        if offset:
            rows = rows[offset:]
        if limit is not None:
            rows = rows[:limit]
        return [dict(row) for row in rows]

    async def _run(self, fn: Any) -> Any:
        """进程内串行，把同步文件 IO 丢到线程。"""
        async with self._lock:
            return await asyncio.to_thread(fn)

    async def list(
        self,
        name: str,
        *,
        filters: dict[str, Any] | None = None,
        order_by: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """列出记录；filters 里匹配路径占位符的字段只用于定位文件。"""
        _, file_layout = self._layout(name)
        values = dict(filters or {})
        path_fields = set(_PLACEHOLDER.findall(file_layout.path))
        record_filters = {
            key: value for key, value in values.items() if key not in path_fields
        }

        def _body() -> list[dict[str, Any]]:
            rows: list[dict[str, Any]] = []
            for _, records in self._read_files(file_layout, values):
                rows.extend(records)
            return self._filter_page(
                rows,
                filters=record_filters,
                order_by=order_by,
                limit=limit,
                offset=offset,
            )

        return await self._run(_body)

    async def get(self, name: str, key: dict[str, Any]) -> dict[str, Any] | None:
        """按主键取一条；内部复用 list(limit=1)。"""
        rows = await self.list(name, filters=key, limit=1)
        return rows[0] if rows else None

    async def create(self, name: str, record: dict[str, Any]) -> dict[str, Any]:
        """插入一条；主键冲突或单文档 name 已有内容则报 DuplicateRecordError。"""
        _, file_layout = self._layout(name)
        created = dict(record)
        identity = _identity(file_layout, created)
        values = dict(created)

        def _body() -> dict[str, Any]:
            path = self._resolve_file(file_layout, values)

            def _mutate() -> dict[str, Any]:
                records = self._load_records(path, file_layout)
                if identity and any(_matches(row, identity) for row in records):
                    raise DuplicateRecordError(
                        f"duplicate {name} key {identity}"
                    )
                if not file_layout.key_fields and records:
                    raise DuplicateRecordError(
                        f"name {name!r} already has a document"
                    )
                records.append(created)
                self._save_records(path, file_layout, records)
                return dict(created)

            return self._with_file_lock(path, _mutate)

        return await self._run(_body)

    async def update(
        self,
        name: str,
        key: dict[str, Any],
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """按主键浅合并 ``updates``；找不到记录返回 None。不删字段。"""
        _, file_layout = self._layout(name)
        values = {**dict(key), **dict(updates)}

        def _body() -> dict[str, Any] | None:
            try:
                path = self._resolve_file(file_layout, values)
            except StorageUnavailableError:
                packed = self._read_files(file_layout, dict(key))
                path = packed[0][0] if packed else None
                if path is None:
                    return None

            def _mutate() -> dict[str, Any] | None:
                records = self._load_records(path, file_layout)
                for idx, row in enumerate(records):
                    if _matches(row, key):
                        updated = dict(row)
                        updated.update(updates)
                        records[idx] = updated
                        self._save_records(path, file_layout, records)
                        return dict(updated)
                return None

            return self._with_file_lock(path, _mutate)

        return await self._run(_body)

    async def delete(self, name: str, key: dict[str, Any]) -> bool:
        """按主键删除；返回是否删到记录。"""
        _, file_layout = self._layout(name)

        def _body() -> bool:
            try:
                path = self._resolve_file(file_layout, dict(key))
                targets = [path]
            except StorageUnavailableError:
                targets = [item[0] for item in self._read_files(file_layout, dict(key))]

            deleted = False
            for path in targets:

                def _mutate(current: Path = path) -> bool:
                    records = self._load_records(current, file_layout)
                    kept = [row for row in records if not _matches(row, key)]
                    if len(kept) == len(records):
                        return False
                    self._save_records(current, file_layout, kept)
                    return True

                deleted = self._with_file_lock(path, _mutate) or deleted
            return deleted

        return await self._run(_body)


__all__ = ["FilePersistentBackend"]
