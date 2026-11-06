# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read package manifests."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from openjiuwen.core.extension.errors import ExtensionFailure
from openjiuwen.core.extension.manifest import MANIFEST_NAME, parse_manifest_mapping

DOMAIN_AGENT = "agent"
DOMAIN_GATEWAY = "gateway"


def has_manifest(root: Path) -> bool:
    """Return True when the directory holds a manifest.json."""

    return (Path(root) / MANIFEST_NAME).is_file()


def package_domain(document: Mapping[str, Any]) -> str:
    """Return the manifest ``domain``; a missing or unknown value means agent."""

    raw = document.get("domain")
    if isinstance(raw, str) and raw.strip().lower() == DOMAIN_GATEWAY:
        return DOMAIN_GATEWAY
    return DOMAIN_AGENT


def read_manifest_document(root: Path) -> dict[str, Any]:
    """Return the raw manifest mapping after the Core rules accept it."""

    path = Path(root) / MANIFEST_NAME
    if not path.is_file():
        raise ValueError(f"{MANIFEST_NAME} is missing under {Path(root)}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ValueError(f"{path.name} top level must be a mapping")
    try:
        parse_manifest_mapping(raw, Path(root))
    except ExtensionFailure as exc:
        raise ValueError(f"{path.name} is invalid: {exc.message}") from exc
    return dict(raw)
