"""File medium adapter (yaml/json/text)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import yaml

from jiuwenswarm.common.secrets.persistence._dotted import delete_dotted, get_dotted, set_dotted
from jiuwenswarm.common.secrets.registry import StorageLocation, infer_format_from_path, resolve_file_path

logger = logging.getLogger(__name__)


class FileMediumAdapter:
    def __init__(self, *, config_dir: Path, workspace_dir: Path) -> None:
        self._config_dir = config_dir
        self._workspace_dir = workspace_dir

    def read_raw(self, loc: StorageLocation) -> str:
        if loc.medium != "file":
            raise ValueError("FileMediumAdapter requires medium=file")
        path = resolve_file_path(loc.path, config_dir=self._config_dir, workspace_dir=self._workspace_dir)
        fmt = loc.format or infer_format_from_path(loc.path)
        if not loc.field:
            if not path.is_file():
                return ""
            return path.read_text(encoding="utf-8")
        if not path.is_file():
            return ""
        data = _load_file(path, fmt)
        value = get_dotted(data, loc.field)
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)

    def write_raw(self, loc: StorageLocation, raw: str) -> None:
        if loc.medium != "file":
            raise ValueError("FileMediumAdapter requires medium=file")
        path = resolve_file_path(loc.path, config_dir=self._config_dir, workspace_dir=self._workspace_dir)
        fmt = loc.format or infer_format_from_path(loc.path)
        if not loc.field:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(raw, encoding="utf-8")
            return
        data: dict = {}
        if path.is_file():
            loaded = _load_file(path, fmt)
            if isinstance(loaded, dict):
                data = loaded
        if raw == "":
            delete_dotted(data, loc.field)
        else:
            set_dotted(data, loc.field, raw)
        _save_file(path, fmt, data)

    def delete_raw(self, loc: StorageLocation) -> None:
        self.write_raw(loc, "")


def _load_file(path: Path, fmt: str) -> object:
    text = path.read_text(encoding="utf-8")
    if fmt == "json":
        return json.loads(text) if text.strip() else {}
    if fmt == "yaml":
        return yaml.safe_load(text) or {}
    return text


def _save_file(path: Path, fmt: str, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    elif fmt == "yaml":
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    else:
        path.write_text(str(data), encoding="utf-8")
