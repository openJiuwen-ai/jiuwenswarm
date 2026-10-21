"""Portable native payloads shared by wheel publishing and runtime selection.

Only standard-library code belongs here: importing build metadata must not load
the SDK, Agent framework, platform APIs or compiler tooling.
"""

from __future__ import annotations

import hashlib
import json
import platform
from pathlib import Path, PurePosixPath
import stat
import sys
from zipfile import ZIP_DEFLATED, ZipFile


MAC_APP = "backends/macos/.build/ComputerUseDriver.app"
WINDOWS_DIR = "backends/windows/.build"
TARGETS = {
    "macos-arm64": (
        f"{MAC_APP}/Contents/MacOS/computer-use-driver",
        f"{MAC_APP}/Contents/Info.plist",
        f"{MAC_APP}/Contents/_CodeSignature/CodeResources",
    ),
    "windows-x64": (
        f"{WINDOWS_DIR}/computer-use-driver.exe",
        f"{WINDOWS_DIR}/computer-use-capture.exe",
        f"{WINDOWS_DIR}/Interop.NativeUIA.dll",
    ),
}


def source_files(root: Path) -> list[Path]:
    files = [root / "pyproject.toml", root / "backends/macos/Package.swift"]
    for name in ("src", "backends/macos/Sources", "backends/windows", "assets", "skills"):
        for path in (root / name).rglob("*"):
            parts = path.relative_to(root).parts
            if path.is_file() and not any(part.startswith(".") or part == "__pycache__" for part in parts):
                files.append(path)
    # Path ordering differs between Windows and POSIX; artifact identities must
    # be independent of the host used to check out the same source revision.
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def source_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in source_files(root):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        content = path.read_bytes()
        if path.suffix in (".py", ".pyi", ".swift", ".cs", ".toml", ".json", ".svg", ".md"):
            content = content.replace(b"\r\n", b"\n")
        digest.update(content)
    return digest.hexdigest()


def host_target() -> str:
    machine = platform.machine().lower()
    if sys.platform == "darwin" and machine in ("arm64", "aarch64"):
        version = platform.mac_ver()[0]
        if version and tuple(int(part) for part in version.split(".")[:2]) < (14, 0):
            raise RuntimeError("The bundled Computer Use driver requires macOS 14+.")
        return "macos-arm64"
    if sys.platform == "win32" and machine in ("amd64", "x86_64", "x64"):
        return "windows-x64"
    raise RuntimeError(f"No bundled Computer Use driver for {sys.platform}/{machine}; other services remain available.")


def _allowed_path(name: str, target: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or any(character in name for character in ("\\", ":")):
        return False
    if target == "macos-arm64":
        return name.startswith(MAC_APP + "/") and path.as_posix() == name
    return name in TARGETS[target]


def write_artifact(root: Path, destination: Path, target: str) -> Path:
    """Archive a completed build without changing its signature or file modes."""
    required = [root / name for name in TARGETS[target]]
    for path in required:
        if not path.is_file():
            raise RuntimeError(f"Native runtime is incomplete: {path}")
    files = required
    if target == "macos-arm64":
        files = sorted(path for path in (root / MAC_APP).rglob("*") if path.is_file())
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"{target}.zip"
    manifest = {}
    with ZipFile(archive, "w", compression=ZIP_DEFLATED, compresslevel=9) as output:
        for path in files:
            if path.is_symlink():
                raise RuntimeError(f"Native artifacts must contain regular files: {path}")
            name = path.relative_to(root).as_posix()
            output.write(path, name)
            manifest[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    metadata = dict(schema=1, target=target, source=source_digest(root),
                    sha256=hashlib.sha256(archive.read_bytes()).hexdigest(), files=manifest)
    (destination / f"{target}.json").write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
    return archive


def read_artifact(directory: Path, target: str, *, source: str | None = None) -> dict:
    """Validate the complete payload before it can enter a wheel or user cache."""
    metadata_file = directory / f"{target}.json"
    archive = directory / f"{target}.zip"
    if not metadata_file.is_file() or not archive.is_file():
        raise RuntimeError(f"Missing precompiled Computer Use artifact: {target}; collect both platform builds first.")
    metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    valid_header = isinstance(metadata, dict) and metadata.get("schema") == 1 and metadata.get("target") == target
    files = metadata.get("files") if valid_header else None
    if not valid_header or not isinstance(files, dict) or not set(TARGETS[target]).issubset(files):
        raise RuntimeError(f"Invalid Computer Use artifact metadata: {target}")
    if source is not None and metadata.get("source") != source:
        raise RuntimeError(f"Computer Use artifact {target} was built from different sources; rebuild both platforms.")
    if hashlib.sha256(archive.read_bytes()).hexdigest() != metadata.get("sha256"):
        raise RuntimeError(f"Computer Use artifact checksum mismatch: {target}")
    with ZipFile(archive) as payload:
        names = payload.namelist()
        if len(names) != len(set(names)) or set(names) != set(metadata["files"]):
            raise RuntimeError(f"Computer Use artifact file list mismatch: {target}")
        for entry in payload.infolist():
            mode = entry.external_attr >> 16
            if (not _allowed_path(entry.filename, target) or not stat.S_ISREG(mode)
                    or hashlib.sha256(payload.read(entry)).hexdigest() != metadata["files"][entry.filename]):
                raise RuntimeError(f"Invalid Computer Use artifact file: {entry.filename}")
    return metadata


def check_artifacts(directory: Path, source_root: Path) -> None:
    """A release wheel must contain both platforms from this exact SDK revision."""
    source = source_digest(source_root)
    for target in TARGETS:
        read_artifact(directory, target, source=source)


def extract_artifact(directory: Path, target: str, metadata: dict, destination: Path) -> None:
    # read_artifact has checked every member; never use unrestricted extractall.
    with ZipFile(directory / f"{target}.zip") as payload:
        for entry in payload.infolist():
            path = destination / entry.filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload.read(entry))
            path.chmod((entry.external_attr >> 16) & 0o777)
    if not runtime_matches(destination, metadata):
        raise RuntimeError(f"Extracted Computer Use driver failed verification: {target}")


def runtime_matches(root: Path, metadata: dict) -> bool:
    return all((root / name).is_file() and not (root / name).is_symlink()
               and hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
               for name, digest in metadata["files"].items())
