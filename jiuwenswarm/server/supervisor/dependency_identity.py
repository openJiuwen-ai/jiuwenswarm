# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Observe installed dependency identities without importing Agent Runtime."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname


def environment_identity() -> str:
    """Detect package installs and editable SDK changes, including same-version upgrades."""
    digest = hashlib.sha256()
    digest.update(f"{sys.implementation.name}:{sys.version_info[:3]}:{sys.prefix}".encode())
    observations = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name", "").lower().replace("_", "-")
        if name in {"workswarm", "jiuwenswarm"}:
            continue
        try:
            item = hashlib.sha256()
            for filename in ("METADATA", "RECORD", "direct_url.json"):
                item.update((distribution.read_text(filename) or "").encode())
            direct = json.loads(distribution.read_text("direct_url.json") or "{}")
            if direct.get("dir_info", {}).get("editable") is True:
                parsed = urlparse(direct.get("url", ""))
                if parsed.scheme == "file":
                    root = Path(url2pathname(parsed.path))
                    modules = (distribution.read_text("top_level.txt") or name.replace("-", "_")).splitlines()
                    for module in sorted(modules):
                        for candidate in (root / module, root / "src" / module):
                            single = candidate.with_suffix(".py")
                            if single.is_file():
                                item.update(single.relative_to(root).as_posix().encode())
                                item.update(single.read_bytes())
                            for current, children, files in os.walk(candidate):
                                children[:] = sorted(
                                    child for child in children if child not in {"__pycache__", ".venv", ".git"}
                                )
                                for filename in sorted(files):
                                    path = Path(current) / filename
                                    if path.suffix == ".py":
                                        item.update(path.relative_to(root).as_posix().encode())
                                        item.update(path.read_bytes())
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise RuntimeError(f"Dependency identity could not be verified: {name}") from exc
        observations.append((name, distribution.version, item.hexdigest()))
    digest.update(json.dumps(sorted(observations), separators=(",", ":")).encode())
    return digest.hexdigest()
