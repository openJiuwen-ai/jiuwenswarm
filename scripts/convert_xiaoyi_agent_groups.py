# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Convert XiaoYi Work AgentGroup deliveries into WorkSwarm packages.

This is an offline structural migration tool. Platform-specific Skill
compatibility replacement is a separate, reviewable step; this converter
normalizes package structure and manifest fields only.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable

from PIL import Image, ImageOps, UnidentifiedImageError


DEFAULT_VERSION = "1.0.0"
_PACKAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", "__MACOSX"}
_FIXED_ZIP_TIMESTAMP = (2026, 1, 1, 0, 0, 0)
_MAX_DISCOVERY_ARCHIVE_COUNT = 256
_MAX_ARCHIVE_NESTING_DEPTH = 4
_MAX_ARCHIVE_FILE_COUNT = 10_000
_MAX_ARCHIVE_SINGLE_FILE_BYTES = 64 * 1024 * 1024
_MAX_ARCHIVE_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_ARCHIVE_PATH_DEPTH = 32
_EXTRACT_CHUNK_BYTES = 1024 * 1024
_AVATAR_MAX_EDGE = 256
_AVATAR_MAX_PIXELS = 16 * 1024 * 1024
_AVATAR_MAX_SVG_BYTES = 512 * 1024
_AVATAR_WEBP_QUALITY = 82
_PLATFORM_TEXT_REPLACEMENTS = {
    "小艺 Work": "WorkSwarm",
}


class ConversionError(ValueError):
    """Raised when a source delivery cannot be converted safely."""


@dataclass(frozen=True)
class SourceGroup:
    """One discovered source AgentGroup."""

    path: Path
    source: str


@dataclass
class _DiscoveryBudget:
    """Cumulative limits shared by every archive reached in one discovery."""

    archive_count: int = 0
    file_count: int = 0
    declared_bytes: int = 0
    extracted_bytes: int = 0


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConversionError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ConversionError(f"{label} must contain a JSON object: {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _is_ignored_relative(path: PurePosixPath) -> bool:
    return any(part in _IGNORED_NAMES or part.startswith("._") for part in path.parts)


def _safe_extract_zip(
    source: Path,
    destination: Path,
    *,
    budget: _DiscoveryBudget | None = None,
    nesting_depth: int = 1,
) -> None:
    """Extract a bounded ZIP without links, traversal, or metadata junk."""
    if nesting_depth < 1 or nesting_depth > _MAX_ARCHIVE_NESTING_DEPTH:
        raise ConversionError("archive nesting depth exceeds limit")
    active_budget = budget or _DiscoveryBudget()
    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ConversionError(f"invalid ZIP archive: {source}") from exc
    try:
        with archive:
            infos = archive.infolist()
            active_budget.archive_count += 1
            if active_budget.archive_count > _MAX_DISCOVERY_ARCHIVE_COUNT:
                raise ConversionError("discovery contains too many archives")
            active_budget.file_count += len(infos)
            if active_budget.file_count > _MAX_ARCHIVE_FILE_COUNT:
                raise ConversionError("archive contains too many files")

            validated: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
            for info in infos:
                raw_name = (info.filename or "").replace("\\", "/")
                if not raw_name:
                    continue
                if "\0" in raw_name:
                    raise ConversionError(
                        f"archive member contains illegal path: {info.filename}"
                    )
                relative = PurePosixPath(raw_name.rstrip("/"))
                windows_path = PureWindowsPath(raw_name)
                if (
                    relative.is_absolute()
                    or bool(windows_path.drive)
                    or windows_path.is_absolute()
                    or ".." in relative.parts
                ):
                    raise ConversionError(
                        f"archive member escapes extraction root: {info.filename}"
                    )
                if len(relative.parts) > _MAX_ARCHIVE_PATH_DEPTH:
                    raise ConversionError(
                        f"archive member path is too deep: {info.filename}"
                    )
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ConversionError(
                        f"archive symlink is not supported: {info.filename}"
                    )
                if not info.is_dir():
                    if info.file_size > _MAX_ARCHIVE_SINGLE_FILE_BYTES:
                        raise ConversionError(
                            f"archive member is too large: {info.filename}"
                        )
                    active_budget.declared_bytes += info.file_size
                    if active_budget.declared_bytes > _MAX_ARCHIVE_TOTAL_BYTES:
                        raise ConversionError("archive uncompressed size is too large")
                validated.append((info, relative))

            for info, relative in validated:
                if _is_ignored_relative(relative):
                    continue
                target = destination.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                extracted_file = 0
                with (
                    archive.open(info, "r") as input_file,
                    target.open("wb") as output_file,
                ):
                    while True:
                        chunk = input_file.read(_EXTRACT_CHUNK_BYTES)
                        if not chunk:
                            break
                        extracted_file += len(chunk)
                        active_budget.extracted_bytes += len(chunk)
                        if extracted_file > _MAX_ARCHIVE_SINGLE_FILE_BYTES:
                            raise ConversionError(
                                f"archive member is too large: {info.filename}"
                            )
                        if active_budget.extracted_bytes > _MAX_ARCHIVE_TOTAL_BYTES:
                            raise ConversionError(
                                "archive uncompressed size is too large"
                            )
                        output_file.write(chunk)
    except ConversionError:
        raise
    except (OSError, RuntimeError, EOFError, zipfile.BadZipFile) as exc:
        raise ConversionError(f"invalid ZIP archive: {source}") from exc


def _is_group_root(path: Path) -> bool:
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        manifest = _read_json(manifest_path, label="source manifest")
    except ConversionError:
        return False
    return manifest.get("package_type") == "agent_group"


def _discover_path(
    path: Path,
    *,
    workspace: Path,
    source_label: str,
    counter: list[int],
    budget: _DiscoveryBudget,
    archive_depth: int,
) -> list[SourceGroup]:
    if path.is_symlink():
        raise ConversionError(f"source symlink is not supported: {path}")
    if path.is_file():
        if path.suffix.lower() != ".zip":
            return []
        counter[0] += 1
        extracted = workspace / f"archive-{counter[0]:04d}"
        extracted.mkdir()
        nested_depth = archive_depth + 1
        _safe_extract_zip(
            path,
            extracted,
            budget=budget,
            nesting_depth=nested_depth,
        )
        return _discover_path(
            extracted,
            workspace=workspace,
            source_label=f"{source_label}!{path.name}",
            counter=counter,
            budget=budget,
            archive_depth=nested_depth,
        )
    if not path.is_dir():
        raise ConversionError(f"input path does not exist: {path}")
    if _is_group_root(path):
        return [SourceGroup(path=path, source=source_label)]

    groups: list[SourceGroup] = []
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        if child.name in _IGNORED_NAMES or child.name.startswith("._"):
            continue
        if child.is_dir() or child.suffix.lower() == ".zip":
            groups.extend(
                _discover_path(
                    child,
                    workspace=workspace,
                    source_label=source_label,
                    counter=counter,
                    budget=budget,
                    archive_depth=archive_depth,
                )
            )
    return groups


def discover_source_groups(
    inputs: Iterable[Path],
    *,
    workspace: Path,
) -> list[SourceGroup]:
    """Discover team roots from delivery ZIPs, nested ZIPs, or directories."""
    workspace.mkdir(parents=True, exist_ok=True)
    groups: list[SourceGroup] = []
    counter = [0]
    budget = _DiscoveryBudget()
    for raw_input in inputs:
        source = raw_input.expanduser().resolve()
        groups.extend(
            _discover_path(
                source,
                workspace=workspace,
                source_label=str(source),
                counter=counter,
                budget=budget,
                archive_depth=0,
            )
        )
    by_id: dict[str, SourceGroup] = {}
    for group in groups:
        manifest = _read_json(group.path / "manifest.json", label="source manifest")
        package_id = manifest.get("name")
        if not isinstance(package_id, str) or not _PACKAGE_ID.fullmatch(package_id):
            raise ConversionError(f"invalid AgentGroup name: {package_id!r}")
        if package_id in by_id:
            raise ConversionError(f"duplicate AgentGroup name: {package_id}")
        by_id[package_id] = group
    return [by_id[name] for name in sorted(by_id)]


def _copy_tree_filtered(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    for item in sorted(source.rglob("*"), key=lambda path: path.as_posix()):
        relative = item.relative_to(source)
        posix_relative = PurePosixPath(relative.as_posix())
        if _is_ignored_relative(posix_relative):
            continue
        if item.is_symlink():
            raise ConversionError(f"package symlink is not supported: {item}")
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, target)


def _normalize_platform_value(value: Any) -> Any:
    """Normalize product wording only in explicitly rewritten metadata files."""
    if isinstance(value, str):
        for source, target in _PLATFORM_TEXT_REPLACEMENTS.items():
            value = value.replace(source, target)
        return value
    if isinstance(value, list):
        return [_normalize_platform_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_platform_value(item) for key, item in value.items()}
    return value


def _localized(value: Any, fallback: str = "") -> dict[str, str]:
    if isinstance(value, dict):
        zh = value.get("zh") or value.get("en") or fallback
        en = value.get("en") or value.get("zh") or fallback
    else:
        text = value.strip() if isinstance(value, str) else fallback
        zh = text
        en = text
    return {"zh": str(zh or ""), "en": str(en or "")}


def _localized_list(values: Any) -> list[dict[str, str]]:
    if not isinstance(values, list):
        return []
    result: list[dict[str, str]] = []
    for value in values:
        localized = _localized(value)
        if localized["zh"] or localized["en"]:
            result.append(localized)
    return result


def _canonical_shared_skills(values: Any) -> list[Any]:
    if not isinstance(values, list):
        raise ConversionError("AgentGroup skills must be a list")
    result: list[Any] = []
    seen_paths: set[str] = set()
    for value in values:
        if isinstance(value, str):
            name = value.strip()
            if not _PACKAGE_ID.fullmatch(name):
                raise ConversionError(f"invalid shared Skill name: {name!r}")
            canonical: Any = name
            path_key = (PurePosixPath("skills") / name).as_posix()
        elif isinstance(value, dict) and isinstance(value.get("dir"), str):
            raw_dir = value["dir"].strip().replace("\\", "/")
            relative = PurePosixPath(raw_dir)
            windows_path = PureWindowsPath(raw_dir)
            if (
                not raw_dir
                or relative.is_absolute()
                or bool(windows_path.drive)
                or windows_path.is_absolute()
                or ".." in relative.parts
            ):
                raise ConversionError(f"invalid shared Skill dir: {raw_dir!r}")
            path_key = relative.as_posix()
            canonical = dict(value)
            canonical["dir"] = path_key
        else:
            raise ConversionError(f"invalid shared Skill reference: {value!r}")
        if path_key not in seen_paths:
            seen_paths.add(path_key)
            result.append(canonical)
    return result


def _optimized_avatar(
    source: Path,
    *,
    destination: Path,
) -> dict[str, Any]:
    """Create a small WebP avatar suitable for list RPCs.

    AgentGroup list cards inline avatar bytes as data URLs.  Source deliveries
    commonly contain 512-1254 px PNGs, so returning them unchanged can turn one
    ten-team list response into tens of megabytes and exceed the Web transport
    budget.  Normalize referenced avatars at migration time while retaining the
    original artwork elsewhere in the package.
    """
    source_bytes = source.stat().st_size
    write_target = destination
    if source.resolve() == destination.resolve():
        write_target = destination.with_name(f".{destination.name}.optimized.tmp")
    try:
        with Image.open(source) as opened:
            source_width, source_height = opened.size
            if (
                source_width <= 0
                or source_height <= 0
                or source_width * source_height > _AVATAR_MAX_PIXELS
            ):
                raise ConversionError(
                    f"avatar image exceeds pixel budget: {source} "
                    f"({source_width}x{source_height})"
                )
            has_alpha = "A" in opened.getbands() or "transparency" in opened.info
            image = ImageOps.exif_transpose(opened)
            image.thumbnail(
                (_AVATAR_MAX_EDGE, _AVATAR_MAX_EDGE),
                Image.Resampling.LANCZOS,
            )
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA" if has_alpha else "RGB")
            destination.parent.mkdir(parents=True, exist_ok=True)
            image.save(
                write_target,
                format="WEBP",
                quality=_AVATAR_WEBP_QUALITY,
                method=6,
            )
            width, height = image.size
        if write_target != destination:
            write_target.replace(destination)
    except ConversionError:
        write_target.unlink(missing_ok=True)
        raise
    except (
        Image.DecompressionBombError,
        OSError,
        UnidentifiedImageError,
        ValueError,
    ) as exc:
        write_target.unlink(missing_ok=True)
        raise ConversionError(f"invalid avatar image: {source}") from exc
    return {
        "sourceBytes": source_bytes,
        "outputBytes": destination.stat().st_size,
        "width": width,
        "height": height,
        "format": "WEBP",
    }


def _avatar_source(base: Path, raw: Any) -> Path | None:
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().replace("\\", "/")
    relative = PurePosixPath(normalized)
    windows_path = PureWindowsPath(normalized)
    if (
        not normalized
        or relative.is_absolute()
        or bool(windows_path.drive)
        or windows_path.is_absolute()
        or ".." in relative.parts
    ):
        return None
    source = base.joinpath(*relative.parts)
    return source if source.is_file() else None


def _materialize_avatar(
    source: Path,
    *,
    destination_dir: Path,
) -> tuple[str, dict[str, Any]]:
    if source.suffix.lower() == ".svg":
        target = destination_dir / "avatar.svg"
        source_bytes = source.stat().st_size
        if source_bytes > _AVATAR_MAX_SVG_BYTES:
            raise ConversionError(
                f"SVG avatar exceeds byte budget: {source} ({source_bytes} bytes)"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
        return target.name, {
            "sourceBytes": source_bytes,
            "outputBytes": target.stat().st_size,
            "width": None,
            "height": None,
            "format": "SVG",
            "preserved": True,
        }
    target = destination_dir / "avatar.webp"
    return target.name, _optimized_avatar(source, destination=target)


def _member_avatar(
    stage: Path,
    *,
    member_id: str,
    member_dir: Path,
    manifest: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    metadata = manifest.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    agent_card = manifest.get("agentCard")
    agent_card = agent_card if isinstance(agent_card, dict) else {}
    candidates: list[Path] = []
    for raw in (
        manifest.get("avatar"),
        metadata.get("avatar"),
        agent_card.get("avatar"),
    ):
        source = _avatar_source(member_dir, raw)
        if source is not None:
            candidates.append(source)
    candidates.extend(
        [
            stage / "avatars" / f"{member_id}.png",
            stage / "avatars" / f"{member_id}.jpg",
            stage / "avatars" / f"{member_id}.jpeg",
            stage / "avatars" / f"{member_id}.webp",
            stage / "avatars" / f"{member_id}.svg",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return _materialize_avatar(candidate, destination_dir=member_dir)
    return "", None


def _root_avatar(
    stage: Path,
    manifest: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    metadata = manifest.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    candidates = [manifest.get("avatar"), metadata.get("avatar")]
    candidates.extend(
        [
            "avatars/expert.png",
            "avatars/expert.svg",
            "avatars/leader.png",
            "avatars/leader.svg",
        ]
    )
    for raw in candidates:
        source = _avatar_source(stage, raw)
        if source is not None:
            return _materialize_avatar(source, destination_dir=stage)
    return "", None


def _canonical_member_manifest(
    manifest: dict[str, Any],
    *,
    version: str,
    avatar: str,
) -> tuple[dict[str, Any], list[str], list[str]]:
    result = dict(manifest)
    metadata = result.pop("metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    agent_card = result.pop("agentCard", None)
    agent_card = agent_card if isinstance(agent_card, dict) else {}
    removed = []
    for key in ("schemaVersion", "packageType"):
        if key in result:
            result.pop(key)
            removed.append(key)
    if metadata:
        removed.append("metadata")
    if agent_card:
        removed.append("agentCard")

    display_name = (
        result.get("display_name") or agent_card.get("name") or result.get("name") or ""
    )
    description = (
        result.get("description")
        or agent_card.get("description")
        or metadata.get("profession")
        or ""
    )
    result["version"] = version
    result["package_type"] = "agent_template"
    result["name"] = str(result.get("name") or display_name)
    result["description"] = str(description)
    result["display_name"] = _localized(display_name, result["name"])
    result["display_description"] = _localized(description)
    if metadata.get("categoryId") and "category" not in result:
        result["category"] = str(metadata["categoryId"])
    tags = _localized_list(metadata.get("tags"))
    if tags and "tags" not in result:
        result["tags"] = tags
    quick_inputs = _localized_list(metadata.get("quickPrompts"))
    if quick_inputs and "quick_inputs" not in result:
        result["quick_inputs"] = quick_inputs
    if avatar:
        result["avatar"] = avatar
    elif not isinstance(result.get("avatar"), str):
        result.pop("avatar", None)
    return result, sorted(removed), sorted(metadata)


def _canonical_group_manifest(
    manifest: dict[str, Any],
    *,
    version: str,
    avatar: str,
) -> tuple[dict[str, Any], list[str], list[str]]:
    result = dict(manifest)
    metadata = result.pop("metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    removed = []
    for key in ("schemaVersion", "packageType"):
        if key in result:
            result.pop(key)
            removed.append(key)
    if metadata:
        removed.append("metadata")

    description = str(result.get("description") or metadata.get("description") or "")
    display_name = (
        result.get("display_name")
        or metadata.get("displayName")
        or result.get("name")
        or ""
    )
    display_description = (
        result.get("display_description")
        or metadata.get("displayDescription")
        or description
    )
    result["version"] = version
    result["package_type"] = "agent_group"
    result["description"] = description
    result["display_name"] = _localized(display_name, str(result.get("name") or ""))
    result["display_description"] = _localized(display_description, description)
    result["skills"] = _canonical_shared_skills(result.get("skills", []))
    if metadata.get("categoryId") and "category" not in result:
        result["category"] = str(metadata["categoryId"])
    tags = _localized_list(result.get("tags") or metadata.get("tags"))
    if tags:
        result["tags"] = tags
    else:
        result.pop("tags", None)
    quick_inputs = _localized_list(
        result.get("quick_inputs") or metadata.get("quickPrompts")
    )
    if quick_inputs:
        result["quick_inputs"] = quick_inputs
    else:
        result.pop("quick_inputs", None)
    if avatar:
        result["avatar"] = avatar
    else:
        result.pop("avatar", None)
    return result, sorted(removed), sorted(metadata)


def _render_readme(
    manifest: dict[str, Any],
    members: list[tuple[str, dict[str, Any]]],
    *,
    original: str,
) -> str:
    display_name = manifest["display_name"]["zh"] or manifest["name"]
    lines = [f"# {display_name}", "", manifest.get("description", "").strip(), ""]
    lines.extend(["## 成员", ""])
    for member_id, member in members:
        member_name = member["display_name"]["zh"] or member["name"]
        description = member.get("description", "").strip()
        suffix = f" — {description}" if description else ""
        lines.append(f"- `{member_id}`：{member_name}{suffix}")
    quick_inputs = manifest.get("quick_inputs", [])
    if quick_inputs:
        lines.extend(["", "## 示例提问", ""])
        for item in quick_inputs:
            prompt = item.get("zh") or item.get("en") or ""
            lines.append(f"- {prompt}")
    lines.extend(
        [
            "",
            "## 包信息",
            "",
            "- 类型：WorkSwarm AgentGroup",
            f"- 版本：{manifest['version']}",
            "",
        ]
    )
    if original:
        lines.extend(
            [
                "## 来源包原始说明",
                "",
                original.rstrip("\n"),
                "",
            ]
        )
    return "\n".join(lines)


def _validate_group(stage: Path) -> None:
    from jiuwenswarm.agents.swarm.agent_group import (
        load_agent_group_package_bundle,
    )

    load_agent_group_package_bundle(stage)


def _write_deterministic_zip(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_symlink():
                raise ConversionError(f"output symlink is not supported: {path}")
            if not path.is_file():
                continue
            relative = Path(source.name) / path.relative_to(source)
            info = zipfile.ZipInfo(relative.as_posix(), _FIXED_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())


def convert_group(
    source: SourceGroup,
    *,
    stage_root: Path,
    output_dir: Path,
    version: str,
    overwrite: bool,
    validate: bool,
) -> dict[str, Any]:
    source_manifest = _normalize_platform_value(
        _read_json(
            source.path / "manifest.json",
            label="source AgentGroup manifest",
        )
    )
    package_id = source_manifest.get("name")
    if not isinstance(package_id, str) or not _PACKAGE_ID.fullmatch(package_id):
        raise ConversionError(f"invalid AgentGroup name: {package_id!r}")
    stage = stage_root / package_id
    _copy_tree_filtered(source.path, stage)
    readme_path = stage / "README.md"
    original_readme = (
        readme_path.read_text(encoding="utf-8") if readme_path.is_file() else ""
    )

    agents = source_manifest.get("agents")
    if (
        not isinstance(agents, list)
        or not agents
        or "leader" not in agents
        or any(not isinstance(item, str) for item in agents)
        or len(set(agents)) != len(agents)
    ):
        raise ConversionError(f"invalid AgentGroup roster: {package_id}")

    member_reports: list[dict[str, Any]] = []
    canonical_members: list[tuple[str, dict[str, Any]]] = []
    for member_id in agents:
        if not _PACKAGE_ID.fullmatch(member_id):
            raise ConversionError(f"invalid member id in {package_id}: {member_id!r}")
        member_dir = stage / "agents" / member_id
        member_manifest_path = member_dir / "manifest.json"
        member_manifest = _normalize_platform_value(
            _read_json(
                member_manifest_path,
                label=f"member manifest {package_id}/{member_id}",
            )
        )
        avatar, avatar_optimization = _member_avatar(
            stage,
            member_id=member_id,
            member_dir=member_dir,
            manifest=member_manifest,
        )
        canonical, removed, metadata_removed = _canonical_member_manifest(
            member_manifest,
            version=version,
            avatar=avatar,
        )
        _write_json(member_manifest_path, canonical)
        canonical_members.append((member_id, canonical))
        member_reports.append(
            {
                "id": member_id,
                "avatar": avatar,
                "avatarOptimization": avatar_optimization,
                "removedKeys": removed,
                "removedMetadataKeys": metadata_removed,
            }
        )

    root_avatar, root_avatar_optimization = _root_avatar(stage, source_manifest)
    canonical_group, removed, metadata_removed = _canonical_group_manifest(
        source_manifest,
        version=version,
        avatar=root_avatar,
    )
    _write_json(stage / "manifest.json", canonical_group)
    (stage / "README.md").write_text(
        _render_readme(
            canonical_group,
            canonical_members,
            original=original_readme,
        ),
        encoding="utf-8",
    )

    if validate:
        _validate_group(stage)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{package_id}.zip"
    if output_path.exists() and not overwrite:
        raise ConversionError(f"output already exists: {output_path}")
    temporary_output = output_path.with_suffix(".zip.tmp")
    temporary_output.unlink(missing_ok=True)
    _write_deterministic_zip(stage, temporary_output)
    temporary_output.replace(output_path)
    return {
        "id": package_id,
        "source": source.source,
        "output": str(output_path),
        "version": version,
        "avatar": root_avatar,
        "avatarOptimization": root_avatar_optimization,
        "memberCount": len(agents),
        "removedKeys": removed,
        "removedMetadataKeys": metadata_removed,
        "members": member_reports,
        "skillsModified": False,
    }


def convert_sources(
    inputs: Iterable[Path],
    *,
    output_dir: Path,
    version: str = DEFAULT_VERSION,
    overwrite: bool = False,
    validate: bool = True,
) -> dict[str, Any]:
    """Convert all AgentGroups found under inputs and return a report."""
    resolved_inputs = [Path(item) for item in inputs]
    if not resolved_inputs:
        raise ConversionError("at least one input path is required")
    if not _PACKAGE_ID.fullmatch(version.replace("+", ".")):
        raise ConversionError(f"invalid version: {version!r}")
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="workswarm_agent_group_convert_"
    ) as raw_tmp:
        workspace = Path(raw_tmp)
        groups = discover_source_groups(
            resolved_inputs,
            workspace=workspace / "sources",
        )
        if not groups:
            raise ConversionError("no AgentGroup package found in inputs")
        stage_root = workspace / "staged"
        stage_root.mkdir()
        converted = [
            convert_group(
                group,
                stage_root=stage_root,
                output_dir=output_dir,
                version=version,
                overwrite=overwrite,
                validate=validate,
            )
            for group in groups
        ]
    report = {
        "schemaVersion": "workswarm.agent-group-conversion.v1",
        "version": version,
        "inputCount": len(resolved_inputs),
        "groupCount": len(converted),
        "skillsModified": False,
        "groups": converted,
    }
    _write_json(output_dir / "conversion-report.json", report)
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert XiaoYi Work AgentGroup deliveries to WorkSwarm ZIPs.",
        epilog=(
            "Run from the JiuwenSwarm source checkout with: "
            "uv run python -m scripts.convert_xiaoyi_agent_groups "
            "--input DELIVERY.zip --output-dir OUTPUT_DIR"
        ),
    )
    parser.add_argument(
        "--input",
        action="append",
        required=True,
        type=Path,
        dest="inputs",
        help="Delivery ZIP, nested team ZIP, team directory, or delivery directory.",
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--skip-runtime-validation",
        action="store_true",
        help="Skip the develop AgentGroup loader check (not recommended).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = convert_sources(
        args.inputs,
        output_dir=args.output_dir,
        version=args.version,
        overwrite=args.overwrite,
        validate=not args.skip_runtime_validation,
    )
    print(
        json.dumps(
            {
                "groupCount": report["groupCount"],
                "outputDir": str(args.output_dir.expanduser().resolve()),
                "report": str(
                    args.output_dir.expanduser().resolve() / "conversion-report.json"
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
