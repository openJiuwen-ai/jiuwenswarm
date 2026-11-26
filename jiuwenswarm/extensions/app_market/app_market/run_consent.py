# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persistent first-run consent for Hub-distributed applications."""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def consent_key(owner_id: str, app_id: str, artifact_sha256: str) -> str:
    return f"{owner_id}\0{app_id}\0{artifact_sha256.strip().lower()}"


class RunConsentStore:
    """Store consents under ``<market data>/run_consents.json``.

    The consent is bound to the installed artifact digest, so an update always
    requires a new confirmation while repeated starts stay silent.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.path = Path(data_dir) / "run_consents.json"

    def _read(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        entries = payload.get("consents") if isinstance(payload, dict) else None
        if not isinstance(entries, dict):
            return {}
        return {
            str(key): str(value)
            for key, value in entries.items()
            if isinstance(key, str) and isinstance(value, str)
        }

    def _write(self, entries: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.parent / f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump({"consents": entries}, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def has(self, owner_id: str, app_id: str, artifact_sha256: str) -> bool:
        return consent_key(owner_id, app_id, artifact_sha256) in self._read()

    def grant(self, owner_id: str, app_id: str, artifact_sha256: str) -> None:
        entries = self._read()
        entries[consent_key(owner_id, app_id, artifact_sha256)] = (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        self._write(entries)

    def revoke_app(self, app_id: str) -> None:
        entries = self._read()
        suffix = f"\0{app_id}\0"
        kept = {key: value for key, value in entries.items() if suffix not in key}
        if len(kept) != len(entries):
            self._write(kept)


__all__ = ["RunConsentStore", "consent_key"]
