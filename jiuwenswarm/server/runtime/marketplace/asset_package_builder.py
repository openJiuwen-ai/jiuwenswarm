# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Bounded, deterministic publishing snapshots; never mutate installed assets."""

from dataclasses import dataclass
from contextlib import ExitStack, contextmanager
import hashlib
import os
import re
from pathlib import Path
import shutil
import stat
import tempfile
import zipfile
from urllib.parse import urlsplit, parse_qsl

import yaml

from .asset_publish_models import PublishIdentity


class PackageBuildError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PackageLimits:
    zip_bytes: int = 50 * 1024 * 1024
    file_bytes: int = 64 * 1024 * 1024
    total_bytes: int = 512 * 1024 * 1024
    max_files: int = 10000
    max_depth: int = 32


@dataclass(frozen=True)
class PreparedPackage:
    artifact_path: Path
    artifact_sha256: str
    size_bytes: int
    source_digest: str
    files: tuple[str, ...]
    excluded: tuple[str, ...]
    normalizations: tuple[str, ...]
    dependencies: tuple[dict, ...]


_EXCLUDED_DIRS = {
    ".git",
    ".archive",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    "out",
    ".venv",
}
_EXCLUDED_FILES = {
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "token.json",
    "tokens.json",
    ".DS_Store",
}


def collect_publish_files(
    root: Path,
    *,
    limits: PackageLimits = PackageLimits(),
    exclude_paths: tuple[Path, ...] = (),
):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise PackageBuildError("UNSAFE_PATH")
    excluded_absolute = {Path(path).absolute() for path in exclude_paths}
    files, excluded = [], []
    total = 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs.sort()
        names.sort()
        for name in dirs[:]:
            path = Path(directory) / name
            rel = path.relative_to(root).as_posix()
            if name in _EXCLUDED_DIRS or path.absolute() in excluded_absolute:
                dirs.remove(name)
                excluded.append(rel + "/")
            elif path.is_symlink():
                raise PackageBuildError("UNSAFE_PATH")
        for name in names:
            path = Path(directory) / name
            rel = path.relative_to(root)
            excluded_environment = name.startswith(".env") and name not in {
                ".env.example", ".env.template"
            }
            excluded_name = name in _EXCLUDED_FILES or name.endswith((".key", ".p12", ".pfx"))
            if (
                path.absolute() in excluded_absolute
                or excluded_name
                or excluded_environment
            ):
                excluded.append(rel.as_posix())
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or "\\" in rel.as_posix():
                raise PackageBuildError("UNSAFE_PATH")
            total += info.st_size
            files.append(path)
            file_limit_exceeded = len(files) > limits.max_files or info.st_size > limits.file_bytes
            if (
                file_limit_exceeded
                or total > limits.total_bytes
                or len(rel.parts) > limits.max_depth
            ):
                raise PackageBuildError("LIMIT_EXCEEDED")
    return sorted(files), sorted(excluded)


@contextmanager
def _open_directory(path: str, flags: int, *, dir_fd: int | None = None):
    """Keep each traversal directory descriptor paired with its close."""
    descriptor = os.open(path, flags, mode=0o600, dir_fd=dir_fd)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _open_source_file(root: Path, relative: Path) -> int:
    if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
        absolute = root.absolute()
        with ExitStack() as descriptors:
            directory_fd = descriptors.enter_context(
                _open_directory(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
            )
            for component in (*absolute.parts[1:], *relative.parts[:-1]):
                directory_fd = descriptors.enter_context(
                    _open_directory(
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory_fd,
                    )
                )
            return os.open(
                relative.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                mode=0o600,
                dir_fd=directory_fd,
            )
    # Platforms without openat still reject static symlinks at every component.
    candidate = root
    for component in relative.parts:
        candidate /= component
        if candidate.is_symlink():
            raise PackageBuildError("UNSAFE_PATH")
    return os.open(candidate, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0), 0o600)


def _copy_digest(
    root: Path,
    files: list[Path],
    limits: PackageLimits,
    destination: Path | None = None,
):
    digest = hashlib.sha256()
    total = 0
    for path in files:
        rel = path.relative_to(root)
        digest.update(rel.as_posix().encode() + b"\0")
        before = path.lstat()
        try:
            fd = _open_source_file(root, rel)
        except OSError:
            raise PackageBuildError("UNSAFE_PATH") from None
        with os.fdopen(fd, "rb") as stream:
            current = os.fstat(stream.fileno())
            if not stat.S_ISREG(current.st_mode) or (before.st_dev, before.st_ino) != (
                current.st_dev,
                current.st_ino,
            ):
                raise PackageBuildError("UNSAFE_PATH")
            target = None
            if destination is not None:
                output = destination / rel
                output.parent.mkdir(parents=True, exist_ok=True)
                target = output.open("xb")
            count = 0
            file_hash = hashlib.sha256()
            try:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    count += len(chunk)
                    total += len(chunk)
                    if count > limits.file_bytes or total > limits.total_bytes:
                        raise PackageBuildError("LIMIT_EXCEEDED")
                    file_hash.update(chunk)
                    if target:
                        target.write(chunk)
            finally:
                if target:
                    target.close()
                    output.chmod(0o755 if current.st_mode & 0o111 else 0o644)
            after = os.fstat(stream.fileno())
            if (current.st_size, current.st_mtime_ns, current.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise PackageBuildError("SOURCE_CHANGED")
            digest.update(file_hash.digest())
    return digest.hexdigest()


def _validate_environment_template(path: Path) -> None:
    with path.open(encoding="utf-8") as stream:
        while True:
            line = stream.readline(65537)
            if not line:
                break
            if len(line) > 65536:
                raise PackageBuildError("LIMIT_EXCEEDED")
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            value = value.strip().strip("\"'")
            if "://" in value:
                try:
                    parsed = urlsplit(value)
                    if parsed.username or parsed.password:
                        raise PackageBuildError("CREDENTIAL_VALUE")
                    if any(
                        re.search(
                            r"(token|key|password|secret|credential|authorization)",
                            k,
                            re.I,
                        )
                        and v
                        and not re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", v)
                        for k, v in parse_qsl(parsed.query)
                    ):
                        raise PackageBuildError("CREDENTIAL_VALUE")
                except ValueError:
                    raise PackageBuildError("CREDENTIAL_VALUE") from None
            if re.search(
                r"(token|key|password|secret|credential|authorization)", key, re.I
            ):
                if value and not re.fullmatch(
                    r"(?:Bearer )?\$\{[A-Za-z_][A-Za-z0-9_]*\}", value
                ):
                    raise PackageBuildError("CREDENTIAL_VALUE")


def build_asset_package(
    source: Path,
    identity: PublishIdentity,
    metadata: dict,
    output_dir: Path,
    *,
    limits: PackageLimits = PackageLimits(),
    exclude_paths: tuple[Path, ...] = (),
) -> PreparedPackage:
    from . import asset_publish_adapters

    source, output_dir = Path(source), Path(output_dir)
    if source.is_symlink():
        raise PackageBuildError("UNSAFE_PATH")
    # Resolve stable OS ancestors (e.g. macOS /var -> /private/var) once.
    # The subsequent descriptor walk rejects any new symlink at that canonical path.
    source = source.resolve()
    exclude_paths = tuple(Path(path).resolve() for path in exclude_paths)
    if output_dir.resolve().is_relative_to(source.resolve()):
        raise PackageBuildError("OUTPUT_INSIDE_SOURCE")
    name = identity.package_name
    invalid_separator = "/" in name or "\\" in name
    if (
        name in {".", ".."}
        or invalid_separator
        or any(ord(c) < 32 for c in name)
    ):
        raise PackageBuildError("UNSAFE_PATH")
    files, excluded = collect_publish_files(
        source, limits=limits, exclude_paths=exclude_paths
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="publish-", dir=output_dir))
    snapshot = stage / "snapshot" / name
    snapshot.mkdir(parents=True)
    try:
        source_digest = _copy_digest(source, files, limits, snapshot)
        for template in snapshot.rglob("*"):
            if (
                template.name in {".env.example", ".env.template"}
                and template.is_file()
            ):
                _validate_environment_template(template)
        details = asset_publish_adapters.normalize_and_validate(
            snapshot, identity, metadata
        )
        current_files, _ = collect_publish_files(
            source, limits=limits, exclude_paths=exclude_paths
        )
        if source_digest != _copy_digest(source, current_files, limits):
            raise PackageBuildError("SOURCE_CHANGED")
        normalized, _ = collect_publish_files(snapshot, limits=limits)
        entries = {}
        for path in normalized:
            rel = path.relative_to(snapshot).as_posix()
            entries[f"{name}/{name}/{rel}" if identity.kind == "skill" else rel] = path
        wrapper = details.get("wrapper")
        if wrapper:
            entries[f"{name}/plugin.yaml"] = yaml.safe_dump(
                wrapper, sort_keys=True, allow_unicode=True
            ).encode()
        if identity.kind == "skill" and (snapshot / "README.md").is_file():
            entries[f"{name}/README.md"] = snapshot / "README.md"
        if len(entries) > limits.max_files or any(
            len(Path(k).parts) > limits.max_depth for k in entries
        ):
            raise PackageBuildError("LIMIT_EXCEEDED")
        artifact = stage / "artifact.zip"
        expanded = 0
        with zipfile.ZipFile(
            artifact, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for arcname, value in sorted(entries.items()):
                size = len(value) if isinstance(value, bytes) else value.stat().st_size
                expanded += size
                if size > limits.file_bytes or expanded > limits.total_bytes:
                    raise PackageBuildError("LIMIT_EXCEEDED")
                info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                mode = (
                    0o755
                    if isinstance(value, Path) and value.stat().st_mode & 0o111
                    else 0o644
                )
                info.external_attr = (stat.S_IFREG | mode) << 16
                with archive.open(info, "w") as dest:
                    if isinstance(value, bytes):
                        dest.write(value)
                    else:
                        with value.open("rb") as src:
                            shutil.copyfileobj(src, dest, 65536)
                if artifact.stat().st_size > limits.zip_bytes:
                    raise PackageBuildError("LIMIT_EXCEEDED")
        if artifact.stat().st_size > limits.zip_bytes:
            raise PackageBuildError("LIMIT_EXCEEDED")
        digest = hashlib.sha256()
        with artifact.open("rb") as stream:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                digest.update(chunk)
        return PreparedPackage(
            artifact,
            digest.hexdigest(),
            artifact.stat().st_size,
            source_digest,
            tuple(sorted(entries)),
            tuple(excluded),
            tuple(details.get("normalizations", [])),
            tuple(details.get("dependencies", [])),
        )
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(stage / "snapshot", ignore_errors=True)
