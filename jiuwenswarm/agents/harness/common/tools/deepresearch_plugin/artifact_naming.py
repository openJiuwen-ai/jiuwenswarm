"""Pure versioned filename allocation for DeepResearch artifacts.

Allocation returns a path that appears available in one directory snapshot;
it does not reserve that path.  Callers must serialize allocation through
publication and use no-replace writes.  Artifact revision identity belongs to
provenance, so this module neither writes nor publishes artifact payloads.
"""

from __future__ import annotations

import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .conversion_utils import make_safe_filename_component


_ERROR_CODE = "ARTIFACT_NAMING_INVALID"
_DEFAULT_BASE_STEM = "深度研究报告"
MAX_FILENAME_BYTES = 240
# This conservative base budget leaves more than 100 bytes for every bounded
# version suffix and the longest sidecar suffix.
_MAX_BASE_STEM_BYTES = 120
MAX_VERSION_NUMBER = 1_000_000
MAX_PROVENANCE_BYTES = 4 * 1024 * 1024
# Legacy Markdown is read only for title inference, so one MiB is sufficient.
MAX_LEGACY_MARKDOWN_BYTES = 1024 * 1024
MAX_SIDECARS_SCANNED = 512
MAX_DIRECTORY_ENTRIES = 1024
MAX_ALLOCATION_ATTEMPTS = 512
_VERSION_SUFFIX_RE = re.compile(r"-v[1-9]\d*$")
_ATX_H1_RE = re.compile(r"^[ \t]{0,3}#[ \t]+(?P<text>.*?)[ \t]*$")
_SETEXT_H1_RE = re.compile(r"^[ \t]{0,3}=+[ \t]*$")


class ArtifactNamingError(ValueError):
    """Raised when artifact version metadata cannot be interpreted safely."""

    code = _ERROR_CODE

    def __init__(self, message: str):
        super().__init__(message)


class _UnidentifiableArtifactError(ArtifactNamingError):
    """Raised when bounded safe reading cannot identify an untrusted artifact."""


@dataclass(frozen=True, slots=True)
class ArtifactVersion:
    base_stem: str
    version_number: int


@dataclass(frozen=True, slots=True)
class ArtifactPaths:
    version: ArtifactVersion
    markdown_path: Path
    provenance_path: Path
    final_result_path: Path


@dataclass(frozen=True, slots=True)
class _DirectorySnapshot:
    exact_names: frozenset[str]
    occupied_keys: frozenset[str]


def _conservative_name_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _invalid(message: str) -> ArtifactNamingError:
    return ArtifactNamingError(message)


def _unidentifiable(message: str) -> _UnidentifiableArtifactError:
    return _UnidentifiableArtifactError(message)


def _strip_terminal_version(value: str) -> str:
    return _VERSION_SUFFIX_RE.sub("", value)


def _truncate_utf8(value: str, byte_limit: int) -> str:
    return value.encode("utf-8")[:byte_limit].decode("utf-8", "ignore")


def _cap_base_stem(value: str, byte_limit: int = _MAX_BASE_STEM_BYTES) -> str:
    return _truncate_utf8(value, byte_limit).rstrip("._-")


def _safe_base_stem_or_empty(value: str) -> str:
    if not isinstance(value, str):
        raise _invalid("artifact base stem must be a string")
    normalized = make_safe_filename_component(
        _strip_terminal_version(value), default=""
    )
    return _cap_base_stem(normalized)


def _normalize_base_stem(value: str) -> str:
    return _safe_base_stem_or_empty(value) or _DEFAULT_BASE_STEM


def _validate_explicit_base_stem(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid("version_base_stem must be a non-empty string")
    if _VERSION_SUFFIX_RE.search(value):
        raise _invalid("version_base_stem must not end in a version suffix")
    if make_safe_filename_component(value) != value:
        raise _invalid("version_base_stem is not a safe filename component")
    if _cap_base_stem(value) != value:
        raise _invalid("version_base_stem exceeds the filename allocation limit")
    return value


def _validate_version_number(value: object) -> int:
    if type(value) is not int or value < 1 or value > MAX_VERSION_NUMBER:
        raise _invalid("version_number must be a bounded positive integer")
    return value


def _first_h1(markdown: str) -> str | None:
    if not isinstance(markdown, str):
        raise _invalid("markdown must be a string")

    lines = markdown.splitlines()
    in_fence = False
    for index, line in enumerate(lines):
        stripped = line.lstrip(" \t")
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        atx_match = _ATX_H1_RE.match(line)
        if atx_match:
            heading = re.sub(r"[ \t]+#+[ \t]*$", "", atx_match.group("text"))
            if heading:
                return heading
        if index and _SETEXT_H1_RE.match(line):
            heading = lines[index - 1].strip()
            if heading:
                return heading
    return None


def _legacy_base_stem(report_path: Path, markdown: str) -> str:
    heading = _first_h1(markdown)
    if heading:
        heading_base_stem = _safe_base_stem_or_empty(heading)
        if heading_base_stem:
            return heading_base_stem

    fallback_stem = _strip_terminal_version(report_path.stem)
    if fallback_stem:
        fallback_base_stem = _safe_base_stem_or_empty(fallback_stem)
        if fallback_base_stem:
            return fallback_base_stem
    return _DEFAULT_BASE_STEM


def initial_version(requested_name: str) -> ArtifactVersion:
    """Return the logical initial version for a user-requested report name."""

    if not isinstance(requested_name, str):
        raise _invalid("requested_name must be a string")
    requested_stem = (
        requested_name[:-3] if requested_name.lower().endswith(".md") else requested_name
    )
    return ArtifactVersion(_normalize_base_stem(requested_stem), 1)


def resolve_artifact_version(
    provenance: dict, report_path: Path, markdown: str
) -> ArtifactVersion:
    """Resolve explicit provenance metadata or a safe legacy logical version."""

    if not isinstance(provenance, dict):
        raise _invalid("provenance must be a dictionary")
    if not isinstance(report_path, Path):
        raise _invalid("report_path must be a Path")

    has_number = "version_number" in provenance
    has_base = "version_base_stem" in provenance
    if has_number != has_base:
        raise _invalid("version metadata must include both fields")
    if has_number:
        version_number = _validate_version_number(provenance["version_number"])
        return ArtifactVersion(
            _validate_explicit_base_stem(provenance["version_base_stem"]), version_number
        )

    history = provenance.get("rewrite_history", [])
    if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
        raise _invalid("legacy rewrite_history must be a list of mappings")
    return ArtifactVersion(
        _legacy_base_stem(report_path, markdown), _validate_version_number(len(history) + 1)
    )


def _paths(output_dir: Path, version: ArtifactVersion) -> ArtifactPaths:
    _validate_version_number(version.version_number)
    markdown_path = output_dir / f"{version.base_stem}-v{version.version_number}.md"
    paths = ArtifactPaths(
        version=version,
        markdown_path=markdown_path,
        provenance_path=markdown_path.with_suffix(".provenance.json"),
        final_result_path=markdown_path.with_suffix(".final-result.json"),
    )
    if any(
        len(path.name.encode("utf-8")) > MAX_FILENAME_BYTES
        for path in (paths.markdown_path, paths.provenance_path, paths.final_result_path)
    ):
        raise _invalid("artifact filename exceeds the byte limit")
    return paths


def _snapshot_directory_names(
    directory: Path, *, directory_fd: int | None = None
) -> _DirectorySnapshot:
    exact_names: set[str] = set()
    occupied_keys: set[str] = set()
    try:
        if directory_fd is not None:
            names = os.listdir(directory_fd)
        else:
            with os.scandir(directory) as entries:
                names = [entry.name for entry in entries]
        for count, name in enumerate(names, start=1):
            if count > MAX_DIRECTORY_ENTRIES:
                raise _invalid("artifact directory entry limit exceeded")
            exact_names.add(name)
            occupied_keys.add(_conservative_name_key(name))
            if name.startswith("."):
                components = name[1:].split(".")
                occupied_keys.update(
                    _conservative_name_key(".".join(components[:end]))
                    for end in range(1, len(components))
                )
    except OSError as exc:
        raise _invalid("artifact directory cannot be scanned") from exc
    return _DirectorySnapshot(frozenset(exact_names), frozenset(occupied_keys))


def _is_available(paths: ArtifactPaths, snapshot: _DirectorySnapshot) -> bool:
    for path in (paths.markdown_path, paths.provenance_path, paths.final_result_path):
        if _conservative_name_key(path.name) in snapshot.occupied_keys:
            return False
    return True


def _base_stem_with_ordinal(base_stem: str, ordinal: int) -> str:
    if ordinal == 1:
        return base_stem
    suffix = f"-{ordinal}"
    return f"{_cap_base_stem(base_stem, _MAX_BASE_STEM_BYTES - len(suffix.encode('utf-8')))}{suffix}"


def allocate_initial_paths(output_dir: Path, requested_name: str) -> ArtifactPaths:
    """Return a currently available initial path; this does not reserve it."""

    if not isinstance(output_dir, Path):
        raise _invalid("output_dir must be a Path")
    version = initial_version(requested_name)
    snapshot = _snapshot_directory_names(output_dir)
    for suffix in range(1, MAX_ALLOCATION_ATTEMPTS + 1):
        base_stem = _base_stem_with_ordinal(version.base_stem, suffix)
        candidate = _paths(output_dir, ArtifactVersion(base_stem, 1))
        if _is_available(candidate, snapshot):
            return candidate
    raise _invalid("artifact allocation attempts exhausted")


def allocate_initial_html_path(output_dir: Path, requested_name: str) -> Path:
    """Allocate one visible HTML path without reserving Markdown sidecars."""
    if not isinstance(output_dir, Path):
        raise _invalid("output_dir must be a Path")
    if not isinstance(requested_name, str):
        raise _invalid("requested_name must be a string")
    requested_stem = requested_name
    for suffix in (".html", ".md"):
        if requested_stem.lower().endswith(suffix):
            requested_stem = requested_stem[: -len(suffix)]
            break
    base_stem = _normalize_base_stem(requested_stem)
    snapshot = _snapshot_directory_names(output_dir)
    for ordinal in range(1, MAX_ALLOCATION_ATTEMPTS + 1):
        candidate_stem = _base_stem_with_ordinal(base_stem, ordinal)
        candidate = output_dir / f"{candidate_stem}-v1.html"
        if len(candidate.name.encode("utf-8")) > MAX_FILENAME_BYTES:
            raise _invalid("artifact filename exceeds the byte limit")
        if _conservative_name_key(candidate.name) not in snapshot.occupied_keys:
            return candidate
    raise _invalid("artifact allocation attempts exhausted")


def _require_document_id(provenance: dict) -> str:
    if not isinstance(provenance, dict):
        raise _invalid("provenance must be a dictionary")
    document_id = provenance.get("document_id")
    if not isinstance(document_id, str) or not document_id:
        raise _invalid("document_id must be a non-empty string")
    return document_id


def _sidecar_markdown_path(sidecar: Path) -> Path:
    suffix = ".provenance.json"
    return sidecar.with_name(f"{sidecar.name.removesuffix(suffix)}.md")


def _read_file_bytes(
    path: Path,
    limit: int,
    label: str,
    *,
    directory_fd: int | None = None,
) -> bytes:
    try:
        metadata = (
            os.lstat(path)
            if directory_fd is None
            else os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        )
    except OSError as exc:
        raise _unidentifiable(f"{label} cannot be inspected") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise _unidentifiable(f"{label} must be a regular file")
    if metadata.st_size > limit:
        raise _unidentifiable(f"{label} exceeds the read limit")
    # O_NOFOLLOW is unavailable on Windows (os.O_NOFOLLOW is None). The
    # os.lstat symlink/regular-file guard directly above already refuses
    # symlink targets, so a plain O_RDONLY open is a safe fallback there.
    nofollow = getattr(os, "O_NOFOLLOW", None)
    open_flags = os.O_RDONLY | nofollow if nofollow is not None else os.O_RDONLY
    try:
        descriptor = (
            os.open(path, open_flags, mode=0o600)
            if directory_fd is None
            else os.open(
                path.name,
                open_flags,
                mode=0o600,
                dir_fd=directory_fd,
            )
        )
        with os.fdopen(descriptor, "rb") as stream:
            content = stream.read(limit + 1)
            opened = os.fstat(stream.fileno())
        if directory_fd is not None:
            after = os.stat(
                path.name, dir_fd=directory_fd, follow_symlinks=False
            )
            changed_during_read = (
                (metadata.st_dev, metadata.st_ino)
                != (opened.st_dev, opened.st_ino)
                or (opened.st_dev, opened.st_ino)
                != (after.st_dev, after.st_ino)
                or not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(after.st_mode)
                or metadata.st_size != opened.st_size
                or opened.st_size != after.st_size
            )
            if changed_during_read:
                raise _unidentifiable(f"{label} changed during safe read")
    except OSError as exc:
        raise _unidentifiable(f"{label} cannot be read safely") from exc
    if len(content) > limit:
        raise _unidentifiable(f"{label} exceeds the read limit")
    return content


def _is_symlink(path: Path, *, directory_fd: int | None = None) -> bool:
    try:
        metadata = (
            os.lstat(path)
            if directory_fd is None
            else os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        )
        return stat.S_ISLNK(metadata.st_mode)
    except OSError as exc:
        raise _invalid("artifact directory changed during allocation") from exc


def _sibling_markdown(
    path: Path,
    snapshot: _DirectorySnapshot,
    *,
    directory_fd: int | None = None,
) -> str:
    """Read at most ``MAX_LEGACY_MARKDOWN_BYTES`` for legacy title inference."""

    if path.name not in snapshot.exact_names:
        return ""
    content = _read_file_bytes(
        path,
        MAX_LEGACY_MARKDOWN_BYTES,
        "legacy markdown",
        directory_fd=directory_fd,
    )
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _invalid("legacy markdown is not UTF-8") from exc


def _sibling_versions(
    parent_path: Path,
    document_id: str,
    snapshot: _DirectorySnapshot,
    *,
    directory_fd: int | None = None,
) -> list[ArtifactVersion]:
    versions: list[ArtifactVersion] = []
    sidecars_scanned = 0
    for name in sorted(snapshot.exact_names):
        if not name.endswith(".provenance.json"):
            continue
        sidecars_scanned += 1
        if sidecars_scanned > MAX_SIDECARS_SCANNED:
            raise _invalid("too many provenance sidecars to scan")
        sidecar = parent_path.parent / name
        if _is_symlink(sidecar, directory_fd=directory_fd):
            continue
        try:
            payload = json.loads(
                _read_file_bytes(
                    sidecar,
                    MAX_PROVENANCE_BYTES,
                    "provenance sidecar",
                    directory_fd=directory_fd,
                )
            )
        except (
            _UnidentifiableArtifactError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            continue
        if not isinstance(payload, dict) or payload.get("document_id") != document_id:
            continue
        markdown_path = _sidecar_markdown_path(sidecar)
        has_number = "version_number" in payload
        has_base = "version_base_stem" in payload
        markdown = (
            ""
            if has_number or has_base
            else _sibling_markdown(
                markdown_path, snapshot, directory_fd=directory_fd
            )
        )
        versions.append(resolve_artifact_version(payload, markdown_path, markdown))
    return versions


def allocate_next_paths(
    parent_path: Path,
    parent_provenance: dict,
    parent_markdown: str,
    *,
    directory_fd: int | None = None,
) -> ArtifactPaths:
    """Return a currently available next path; this does not reserve it."""

    if not isinstance(parent_path, Path):
        raise _invalid("parent_path must be a Path")
    document_id = _require_document_id(parent_provenance)
    parent_version = resolve_artifact_version(
        parent_provenance, parent_path, parent_markdown
    )
    snapshot = _snapshot_directory_names(
        parent_path.parent, directory_fd=directory_fd
    )
    version_numbers = [parent_version.version_number]
    for version in _sibling_versions(
        parent_path,
        document_id,
        snapshot,
        directory_fd=directory_fd,
    ):
        version_numbers.append(version.version_number)
    max_version = max(version_numbers)
    next_number = max_version + 1
    for offset in range(MAX_ALLOCATION_ATTEMPTS):
        candidate_number = next_number + offset
        _validate_version_number(candidate_number)
        candidate = _paths(
            parent_path.parent,
            ArtifactVersion(parent_version.base_stem, candidate_number),
        )
        if _is_available(candidate, snapshot):
            return candidate
    raise _invalid("artifact allocation attempts exhausted")
