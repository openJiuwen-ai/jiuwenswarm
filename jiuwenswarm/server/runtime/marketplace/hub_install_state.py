# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persistent provenance for marketplace packages prepared from Hub."""

from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from jiuwenswarm.extensions.swarm_connector_hub.constants import OPENJIUWEN_HUB_SOURCE_ID
from jiuwenswarm.server.runtime.marketplace.hub_asset_port import HubAssetKind
from jiuwenswarm.server.runtime.marketplace.hub_asset_type_adapter import (
    HubAssetTypeConflictError,
    resolve_hub_asset_kind,
)


def normalize_install_source_id(source_id: str | None) -> str:
    """Default Hub install provenance when ledger / callers omit ``source_id``."""
    value = str(source_id or "").strip()
    return value or OPENJIUWEN_HUB_SOURCE_ID


def ledger_key(source_id: str | None, asset_id: str) -> str:
    """In-memory map key: ``(source_id, asset_id)``, aligned with SPI ConnectorRef.

    JSON on disk still stores full records; this key is rebuilt on read.
    """
    asset = str(asset_id or "").strip()
    return f"{normalize_install_source_id(source_id)}\0{asset}"


@dataclass(frozen=True, slots=True)
class HubInstallRecord:
    asset_id: str
    kind: HubAssetKind
    version: str
    checksum_sha256: str
    installed_at: str
    package_id: str = ""
    source_id: str = OPENJIUWEN_HUB_SOURCE_ID

    @classmethod
    def from_dict(cls, value: dict) -> "HubInstallRecord | None":
        raw_kind = value.get("kind")
        if raw_kind not in {"agent_template", "agent_group", "plugin", "mcp", "system_plugin", "app"}:
            try:
                raw_kind = resolve_hub_asset_kind(
                    str(value.get("plugin_type") or "")
                )
            except HubAssetTypeConflictError:
                return None
        fields = {
            key: value.get(key)
            for key in ("asset_id", "version", "checksum_sha256", "installed_at")
        }
        if raw_kind is None or not all(
            isinstance(item, str) for item in fields.values()
        ):
            return None
        if not fields["asset_id"] or not fields["version"]:
            return None
        package_id = value.get("package_id")
        if not isinstance(package_id, str) or not package_id.strip():
            package_id = fields["asset_id"]
        # CMS-V-05 / I-05: old ledgers without source_id backfill to default Hub.
        source_id = normalize_install_source_id(value.get("source_id"))
        return cls(
            kind=raw_kind,
            package_id=package_id.strip(),
            source_id=source_id,
            **fields,
        )


class HubInstallStateStore:
    def __init__(self, kind_root: Path) -> None:
        self.path = kind_root / "hub_state.json"

    def _read(self) -> dict[str, HubInstallRecord]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        records = payload.get("packages") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            return {}
        result: dict[str, HubInstallRecord] = {}
        for value in records:
            if not isinstance(value, dict):
                continue
            record = HubInstallRecord.from_dict(value)
            if record is not None:
                result[ledger_key(record.source_id, record.asset_id)] = record
        return result

    def _write(self, records: dict[str, HubInstallRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temp_path = Path(stream.name)
                json.dump(
                    {"packages": [asdict(records[key]) for key in sorted(records)]},
                    stream,
                    ensure_ascii=False,
                    indent=2,
                )
                stream.write("\n")
            temp_path.replace(self.path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def get(self, asset_id: str, *, source_id: str | None = None) -> HubInstallRecord | None:
        """Lookup by ``(source_id, asset_id)``.

        When ``source_id`` is omitted/blank: try default Hub slot, then a unique
        match on ``asset_id`` across sources (compat for legacy single-source
        callers). Ambiguous multi-source hits return ``None``.
        """
        asset = str(asset_id or "").strip()
        if not asset:
            return None
        records = self._read()
        explicit = str(source_id or "").strip()
        if explicit:
            return records.get(ledger_key(explicit, asset))
        default_hit = records.get(ledger_key(None, asset))
        if default_hit is not None:
            return default_hit
        matches = [record for record in records.values() if record.asset_id == asset]
        if len(matches) == 1:
            return matches[0]
        return None

    def all(self) -> dict[str, HubInstallRecord]:
        """Return ledger keyed by :func:`ledger_key` (not bare ``asset_id``)."""
        return self._read()

    def get_by_package_id(self, package_id: str) -> HubInstallRecord | None:
        wanted = str(package_id or "").strip()
        if not wanted:
            return None
        return next(
            (record for record in self._read().values() if record.package_id == wanted),
            None,
        )

    def assert_package_id_available(
        self,
        package_id: str,
        *,
        asset_id: str,
        source_id: str | None,
        kind: str = "asset",
    ) -> None:
        """Reject cross-source or cross-asset ownership of the same package id (D-09 #6)."""
        owner = self.get_by_package_id(package_id)
        if owner is None:
            return
        wanted_source = normalize_install_source_id(source_id)
        owner_source = normalize_install_source_id(owner.source_id)
        if owner_source != wanted_source:
            raise ValueError(
                f"Hub {kind} package id {package_id!r} is already installed "
                f"from source {owner_source}"
            )
        if owner.asset_id != str(asset_id or "").strip():
            raise ValueError(
                f"Hub {kind} package id {package_id!r} is owned by another Hub asset"
            )

    def upsert(self, record: HubInstallRecord) -> None:
        records = self._read()
        records[ledger_key(record.source_id, record.asset_id)] = record
        self._write(records)

    def remove(self, asset_id: str, *, source_id: str | None = None) -> None:
        """Remove ``(source_id, asset_id)``. Same lookup rules as :meth:`get`."""
        asset = str(asset_id or "").strip()
        if not asset:
            return
        records = self._read()
        explicit = str(source_id or "").strip()
        if explicit:
            key = ledger_key(explicit, asset)
            if key not in records:
                return
            del records[key]
            self._write(records)
            return
        key = ledger_key(None, asset)
        if key in records:
            del records[key]
            self._write(records)
            return
        matches = [k for k, record in records.items() if record.asset_id == asset]
        if len(matches) != 1:
            return
        del records[matches[0]]
        self._write(records)
