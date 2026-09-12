# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Normalize historical standalone expert packages into the beta3 dialect."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Mapping

from jiuwenswarm.server.runtime.expert.expert_store import validate_expert_package


class ExpertNormalizationError(ValueError):
    """The source cannot be normalized without guessing required fields."""


def normalize_standalone_expert(
    source: Path,
    *,
    destination_root: Path,
    collaboration: Mapping[str, Any] | None = None,
) -> Path:
    """Create a beta3 AgentTemplate package without mutating *source*.

    Both the current camelCase dialect and the historical snake_case expert
    pilot dialect are accepted.  Existing destination packages are protected.
    """

    source = Path(source).expanduser().resolve(strict=True)
    manifest = _read_manifest(source)
    if manifest.get("package_type") == "agent_group":
        raise ExpertNormalizationError("agent_group cannot be normalized as an expert")

    expert_id = _expert_id(source, manifest)
    destination_root = Path(destination_root).expanduser().resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / expert_id
    if destination.exists() or destination.is_symlink():
        raise ExpertNormalizationError(f"expert package already exists: {expert_id}")

    staging_root = destination_root / f".{expert_id}.staging-{uuid.uuid4().hex}"
    staging = staging_root / expert_id
    staging.mkdir(parents=True)
    try:
        _copy_package_files(source, staging)
        normalized = _normalized_manifest(manifest, expert_id=expert_id)
        if collaboration is not None:
            metadata = dict(normalized.get("metadata") or {})
            metadata["collaboration"] = dict(collaboration)
            normalized["metadata"] = metadata
        (staging / "manifest.json").write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        validate_expert_package(staging)
        os.replace(staging, destination)
        staging_root.rmdir()
    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise
    return destination


def _normalized_manifest(
    manifest: Mapping[str, Any], *, expert_id: str
) -> dict[str, Any]:
    if manifest.get("packageType") == "agent_template":
        result = dict(manifest)
        card = dict(result.get("agentCard") or {})
        card["id"] = expert_id
        result["agentCard"] = card
        result.pop("model", None)
        return result
    if manifest.get("package_type") != "agent_template":
        raise ExpertNormalizationError("package type must be agent_template")

    name = _localized(manifest.get("display_name")) or str(
        manifest.get("name") or expert_id
    )
    description = _localized(manifest.get("display_description")) or str(
        manifest.get("description") or ""
    )
    persona = manifest.get("persona")
    if not isinstance(persona, Mapping) or not persona.get("dir"):
        raise ExpertNormalizationError("persona.dir is required")
    skills = []
    for raw in manifest.get("skills") or []:
        if not isinstance(raw, Mapping) or not raw.get("dir"):
            raise ExpertNormalizationError("every skills entry must contain dir")
        skills.append(
            {
                "dir": _clean_relative(raw.get("dir")),
                "mode": str(raw.get("mode") or "all"),
            }
        )
    tags = [_localized(item) for item in manifest.get("tags") or []]
    prompts = [_localized(item) for item in manifest.get("quick_inputs") or []]
    metadata: dict[str, Any] = {
        "version": str(manifest.get("version") or "1.0.0-beta3-normalized"),
        "tags": [item for item in tags if item],
        "quickPrompts": [item for item in prompts if item],
        "profession": name,
        "categoryId": str(manifest.get("category") or "IndustryConsultant"),
        "normalizedFrom": "agent_template.v1",
    }
    return {
        "packageType": "agent_template",
        "agentCard": {"id": expert_id, "name": name, "description": description},
        "persona": {"dir": _clean_relative(persona.get("dir"))},
        "skills": skills,
        "metadata": metadata,
    }


def _expert_id(source: Path, manifest: Mapping[str, Any]) -> str:
    card = manifest.get("agentCard")
    value = card.get("id") if isinstance(card, Mapping) else manifest.get("name")
    expert_id = str(value or source.name).strip()
    if expert_id != source.name:
        raise ExpertNormalizationError(
            f"expert id must match source directory: {expert_id!r} != {source.name!r}"
        )
    if (
        not expert_id
        or "/" in expert_id
        or "\\" in expert_id
        or expert_id in {".", ".."}
    ):
        raise ExpertNormalizationError(f"unsafe expert id: {expert_id!r}")
    return expert_id


def _clean_relative(value: Any) -> str:
    clean = str(value or "").strip().replace("\\", "/")
    while clean.startswith("./"):
        clean = clean[2:]
    path = Path(clean)
    if not clean or path.is_absolute() or ".." in path.parts:
        raise ExpertNormalizationError(f"unsafe package path: {value!r}")
    return clean


def _localized(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("zh", "cn", "en"):
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                return text.strip()
    return ""


def _read_manifest(source: Path) -> dict[str, Any]:
    try:
        value = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExpertNormalizationError("manifest.json is invalid") from exc
    if not isinstance(value, dict):
        raise ExpertNormalizationError("manifest.json must be an object")
    return value


def _copy_package_files(source: Path, destination: Path) -> None:
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if relative == Path("manifest.json"):
            continue
        if path.is_symlink():
            raise ExpertNormalizationError(f"symlinks are not supported: {relative}")
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


__all__ = ["ExpertNormalizationError", "normalize_standalone_expert"]
