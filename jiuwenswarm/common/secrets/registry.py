"""L2: secret_registry.yaml routing."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Union

import yaml

from jiuwenswarm.common.utils import get_config_dir, get_workspace_dir

logger = logging.getLogger(__name__)

Medium = Literal["env", "file", "db"]
Format = Literal["yaml", "json", "text"]

_VALID_MEDIA: frozenset[str] = frozenset({"env", "file", "db"})
_VALID_FORMATS: frozenset[str] = frozenset({"yaml", "json", "text"})

BUNDLED_REGISTRY_NAME = "secret_registry.yaml"


def bundled_registry_path(*, resources_dir: Path | None = None) -> Path:
    base = resources_dir or (
        Path(__file__).resolve().parent.parent.parent / "resources"
    )
    return base / BUNDLED_REGISTRY_NAME


@dataclass(frozen=True)
class StorageLocation:
    medium: Medium
    path: str
    field: str | None = None
    format: Format | None = None


@dataclass(frozen=True)
class DefaultLocation:
    logical_key: str


StorageTarget = Union[StorageLocation, DefaultLocation]


def derive_legacy_name(logical_key: str, target: StorageTarget) -> str:
    if isinstance(target, StorageLocation) and target.medium == "env":
        return target.path
    return logical_key


def resolve_file_path(path: str, *, config_dir: Path, workspace_dir: Path) -> Path:
    if path.startswith("workspace/"):
        rel = path[len("workspace/") :]
        return (workspace_dir / rel).resolve()
    p = Path(path)
    if p.is_absolute() or (len(path) >= 2 and path[1] == ":"):
        return p.expanduser().resolve()
    if path.startswith("~"):
        return p.expanduser().resolve()
    if path.startswith("/"):
        return p.resolve()
    return (config_dir / path).resolve()


class SecretRegistry:
    def __init__(
        self,
        *,
        config_dir: Path | None = None,
        workspace_dir: Path | None = None,
        bundled_path: Path | None = None,
        user_path: Path | None = None,
        entries: dict[str, StorageLocation] | None = None,
    ) -> None:
        self._config_dir = (config_dir or get_config_dir()).resolve()
        self._workspace_dir = (workspace_dir or get_workspace_dir()).resolve()
        if entries is not None:
            self._entries = entries
        else:
            bundled = bundled_path or bundled_registry_path()
            user = user_path or (self._config_dir / BUNDLED_REGISTRY_NAME)
            self._entries = _load_merged_entries(bundled, user)

    @property
    def config_dir(self) -> Path:
        return self._config_dir

    @property
    def workspace_dir(self) -> Path:
        return self._workspace_dir

    def resolve(self, logical_key: str) -> StorageTarget:
        loc = self._entries.get(logical_key)
        if loc is None:
            return DefaultLocation(logical_key=logical_key)
        return loc

    def reload(self) -> None:
        bundled = bundled_registry_path()
        user = self._config_dir / BUNDLED_REGISTRY_NAME
        self._entries = _load_merged_entries(bundled, user)

    def resolve_file_absolute(self, loc: StorageLocation) -> Path:
        if loc.medium != "file":
            raise ValueError("resolve_file_absolute requires medium=file")
        return resolve_file_path(
            loc.path, config_dir=self._config_dir, workspace_dir=self._workspace_dir
        )


def _load_merged_entries(bundled: Path, user: Path) -> dict[str, StorageLocation]:
    merged: dict[str, dict] = {}
    if bundled.is_file():
        merged.update(_read_yaml_mapping(bundled))
    if user.is_file():
        merged.update(_read_yaml_mapping(user))
    return {k: _parse_entry(k, v) for k, v in merged.items()}


def _read_yaml_mapping(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"secret_registry must be a mapping: {path}")
    return data


def _parse_entry(logical_key: str, raw: object) -> StorageLocation:
    if not isinstance(raw, dict):
        raise ValueError(f"secret_registry entry for {logical_key!r} must be a mapping")
    medium = raw.get("medium")
    path = raw.get("path")
    if medium not in _VALID_MEDIA:
        raise ValueError(f"secret_registry {logical_key!r}: invalid medium {medium!r}")
    if not path or not isinstance(path, str):
        raise ValueError(f"secret_registry {logical_key!r}: path is required")
    field = raw.get("field")
    if field is not None and not isinstance(field, str):
        raise ValueError(f"secret_registry {logical_key!r}: field must be a string")
    fmt = raw.get("format")
    if fmt is not None and fmt not in _VALID_FORMATS:
        raise ValueError(f"secret_registry {logical_key!r}: invalid format {fmt!r}")
    if medium == "env" and field:
        raise ValueError(
            f"secret_registry {logical_key!r}: env medium must not use field"
        )
    if medium == "env" and fmt:
        raise ValueError(
            f"secret_registry {logical_key!r}: env medium must not use format"
        )
    if fmt == "text" and field:
        raise ValueError(
            f"secret_registry {logical_key!r}: format=text must not use field"
        )
    return StorageLocation(
        medium=medium,  # type: ignore[arg-type]
        path=path.strip(),
        field=field.strip() if isinstance(field, str) and field.strip() else None,
        format=fmt,  # type: ignore[arg-type]
    )


def infer_format_from_path(path: str) -> Format:
    lower = path.lower()
    if lower.endswith((".yaml", ".yml")):
        return "yaml"
    if lower.endswith(".json"):
        return "json"
    return "text"
