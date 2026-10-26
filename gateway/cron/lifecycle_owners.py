"""Durable routing identities for project lifecycle recovery.

This is Gateway routing metadata, not a copy of AgentServer lifecycle state.
Keep identities after the last job is removed so recovery can still reach its
owner after a Gateway restart.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import portalocker


class LifecycleOwners:
    def __init__(self, cron_path: Path):
        self.path = cron_path.with_name("cron_lifecycle_owners.json")

    def read(self) -> set[str]:
        if not self.path.exists():
            return {""}
        owners = json.loads(self.path.read_text(encoding="utf-8"))["owners"]
        if not isinstance(owners, list) or any(
            not isinstance(owner, str) for owner in owners
        ):
            raise ValueError("invalid cron lifecycle routing registry")
        return set(owners) | {""}

    def remember(self, owner: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(str(self.path) + ".lock", timeout=10):
            owners = self.read()
            if owner in owners and self.path.exists():
                return
            owners.add(owner)
            temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("x", encoding="utf-8") as stream:
                    json.dump({"owners": sorted(owners)}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
