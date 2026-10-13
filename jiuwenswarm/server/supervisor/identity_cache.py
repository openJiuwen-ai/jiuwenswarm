# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Content identities cached only behind trusted file-change observations.

NTFS per-file USNs and file identities permit reuse of individual content
digests. Enumeration and metadata checks remain O(N). Other filesystems, open
writers and unsupported/error responses use full content hashing instead.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

_SCHEMA = 3
_CACHE_LOCK = threading.RLock()
_LAST_DIAGNOSTICS: dict = {}


def _canonical(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _bytes_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _encoded(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _cache_path(checkout: Path) -> Path:
    parent = os.environ.get("LOCALAPPDATA")
    base = Path(parent) if parent else Path.home() / ".cache"
    return base / "JiuwenSwarm" / "code-identities" / (_bytes_digest(_canonical(checkout).encode()) + ".json")


@contextmanager
def _file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if sys.platform == "win32":
            import msvcrt
            deadline = time.monotonic() + 10
            while True:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.005)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@lru_cache(maxsize=1)
def _windows_kernel():
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel.DeviceIoControl.restype = wintypes.BOOL
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def _ntfs_marker(path: Path):
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    kernel = _windows_kernel()
    # Share read/delete, but not write: an unclosed writer cannot supply a
    # reusable last-close USN. Sharing violations fall back to content hashing.
    handle = kernel.CreateFileW(str(path), 0x80, 5, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        return None
    try:
        buffer = ctypes.create_string_buffer(4096)
        returned = wintypes.DWORD()
        if not kernel.DeviceIoControl(handle, 0x000900EB, None, 0,
                buffer, len(buffer), ctypes.byref(returned), None):
            return None
        if returned.value < 60:
            return None
        length, major, minor, file_id, parent_id, usn = struct.unpack_from("<IHHQQq", buffer.raw)
        if major != 2 or minor != 0 or length > returned.value or usn <= 0:
            return None
        basic = ctypes.create_string_buffer(40)
        if not kernel.GetFileInformationByHandleEx(handle, 0, basic, len(basic)):
            return None
        created, _accessed, written, changed, attributes = struct.unpack_from("<qqqqI", basic.raw)
        stat = path.stat()
        return [stat.st_dev, file_id, parent_id, usn, created, written, changed, attributes, stat.st_size]
    except (OSError, ValueError, struct.error):
        return None
    finally:
        kernel.CloseHandle(handle)


def _source_paths(package: Path) -> list[Path]:
    # Frontend installation includes Python build tools inside node_modules.
    # They are not imported by the Worker; their dependency manifests retain
    # version identity below. Prune only this exact directory before scanning,
    # preserving backend and all other package Python, including frontend code.
    frontend = package / "channels" / "web" / "frontend"
    paths = []

    def scan_error(error: OSError) -> None:
        # An unreadable Runtime subtree cannot be silently omitted from identity.
        raise error

    for directory, children, filenames in os.walk(package, topdown=True, onerror=scan_error):
        current = Path(directory)
        if current == frontend:
            children[:] = [name for name in children if os.path.normcase(name) != os.path.normcase("node_modules")]
        paths.extend(current / name for name in filenames if os.path.normcase(name).endswith(".py"))
    return sorted(paths)


def _content_hashes(path: Path) -> dict[str, str]:
    value = path.read_bytes()
    normalized = value.replace(b"\r\n", b"\n") if b"\0" not in value else value
    return {"sha256": _bytes_digest(value), "git_blob": hashlib.sha1(
        f"blob {len(value)}\0".encode() + value, usedforsecurity=False).hexdigest(),
        "git_blob_lf": hashlib.sha1(f"blob {len(normalized)}\0".encode() + normalized,
                                  usedforsecurity=False).hexdigest()}


def _dependencies(checkout: Path) -> dict:
    return {name: _content_hashes(checkout / name) if (checkout / name).exists() else None
        for name in ("pyproject.toml", "uv.lock",
                     "jiuwenswarm/channels/web/frontend/package.json",
                     "jiuwenswarm/channels/web/frontend/package-lock.json")}


def _index_paths(value: bytes) -> dict[str, str] | None:
    if len(value) < 12 or value[:4] != b"DIRC":
        return None
    version, count = struct.unpack_from(">II", value, 4)
    if version not in (2, 3):
        return None
    entries = {}
    position = 12
    try:
        for _ in range(count):
            begin = position
            flags = struct.unpack_from(">H", value, position + 60)[0]
            digest = value[position + 40:position + 60].hex()
            position += 62 + (2 if version == 3 and flags & 0x4000 else 0)
            end = value.index(b"\0", position)
            name = value[position:end].decode("utf-8")
            if flags & 0x3000:
                return None  # Unmerged index: use the safe full-content path.
            entries[name] = digest
            position = begin + ((end + 1 - begin + 7) // 8) * 8
    except (ValueError, UnicodeError, struct.error):
        return None
    return entries


def _git_metadata(checkout: Path) -> tuple[dict, dict[str, str] | None]:
    try:
        return _read_git_metadata(checkout)
    except (OSError, ValueError, UnicodeError):
        # Git metadata is only an accelerator/classifier. Its absence must
        # select full content verification, not make source identity weaker.
        return {"unavailable": True}, None


def _read_git_metadata(checkout: Path) -> tuple[dict, dict[str, str] | None]:
    pointer = checkout / ".git"
    if not pointer.exists():
        return {}, {}
    git_dir = pointer
    if pointer.is_file():
        text = pointer.read_text(encoding="utf-8").strip()
        if not text.startswith("gitdir:"):
            return {"pointer": _bytes_digest(text.encode())}, None
        git_dir = (checkout / text.partition(":")[2].strip()).resolve()
    common = git_dir
    common_pointer = git_dir / "commondir"
    if common_pointer.exists():
        common = (git_dir / common_pointer.read_text(encoding="utf-8").strip()).resolve()
    head = (git_dir / "HEAD").read_bytes()
    global_config = Path(os.environ.get("GIT_CONFIG_GLOBAL") or Path.home() / ".gitconfig")
    system_config = os.environ.get("GIT_CONFIG_SYSTEM")
    system_paths = [Path(system_config)] if system_config else [Path("/etc/gitconfig")]
    if sys.platform == "win32" and not system_config:
        executable = shutil.which("git")
        if executable:
            binary = Path(executable)
            system_paths = [binary.parent.parent / "etc/gitconfig", binary.parent.parent.parent / "etc/gitconfig"]
    if os.environ.get("GIT_CONFIG_NOSYSTEM"):
        system_paths = []
    config_paths = [*system_paths, global_config, common / "config", git_dir / "config.worktree"]
    paths = [git_dir / "HEAD", git_dir / "index", git_dir / "index.lock", common / "packed-refs",
             checkout / ".gitattributes", common / "info/attributes", *config_paths]
    if head.startswith(b"ref:"):
        reference = head.partition(b":")[2].strip().decode("utf-8")
        paths.extend((git_dir / reference, common / reference))
    stamps = {_canonical(path): _bytes_digest(path.read_bytes()) if path.exists() else None for path in paths}
    import configparser
    config = configparser.RawConfigParser(strict=False)
    for path in config_paths:
        if path.exists():
            with path.open(encoding="utf-8") as stream:
                config.read_file(stream)
    stamps["text_normalization"] = config.get("core", "autocrlf", fallback="").strip().lower() in {"true", "input"}
    index = git_dir / "index"
    tracked = _index_paths(index.read_bytes()) if index.exists() else {}
    return stamps, tracked


def _load_cache(path: Path, checkout: str, version: dict) -> dict:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            return {}
        checksum = result.pop("checksum")
        if (result.get("schema") != _SCHEMA or result.get("checkout") != checkout
                or result.get("version") != version or checksum != _bytes_digest(_encoded(result))):
            return {}
        if not isinstance(result.get("files"), dict):
            return {}
        for entry in result["files"].values():
            if not isinstance(entry, dict):
                return {}
            if any(not isinstance(entry.get(key), str) or len(entry[key]) != size
                    or any(letter not in "0123456789abcdef" for letter in entry[key])
                    for key, size in (("sha256", 64), ("git_blob", 40), ("git_blob_lf", 40))):
                return {}
        return result
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def _save_cache(path: Path, payload: dict) -> None:
    value = {**payload, "checksum": _bytes_digest(_encoded(payload))}
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _fingerprint(checkout: str, files: dict, dependencies: dict, version: dict) -> str:
    # Git metadata controls validation and dirty classification, not identity:
    # committing documentation cannot change identical Runtime code's identity.
    return _bytes_digest(_encoded({"schema": _SCHEMA, "checkout": checkout,
        "sources": {name: entry["sha256"] for name, entry in files.items()},
        "dependencies": dependencies, "version": version}))


def _compute(package: Path, cache_path: Path | None) -> str:
    global _LAST_DIAGNOSTICS
    checkout = package.parent
    canonical = _canonical(checkout)
    version = {"implementation": sys.implementation.name, "python": list(sys.version_info[:3])}
    cached = _load_cache(cache_path, canonical, version) if cache_path is not None else {}
    for _attempt in range(3):
        paths = _source_paths(package)
        git, tracked = _git_metadata(checkout)
        dependencies = _dependencies(checkout)
        markers = {str(path.relative_to(checkout)): _ntfs_marker(path) for path in paths}
        reusable = tracked is not None and all(marker is not None for marker in markers.values())
        files, rehashed = {}, 0
        for path in paths:
            name = str(path.relative_to(checkout))
            old = cached.get("files", {}).get(name, {})
            if reusable and old.get("marker") == markers[name]:
                content = {key: old[key] for key in ("sha256", "git_blob", "git_blob_lf")}
            else:
                content = _content_hashes(path)
                rehashed += 1
            files[name] = {**content, "marker": markers[name]}
        current_paths = _source_paths(package)
        if paths != current_paths or git != _git_metadata(checkout)[0] or dependencies != _dependencies(checkout):
            continue
        if reusable and markers != {str(path.relative_to(checkout)): _ntfs_marker(path) for path in paths}:
            continue
        dirty = []
        if tracked is not None:
            for name, entry in {**files, **dependencies}.items():
                accepted = ({entry["git_blob"], entry["git_blob_lf"]}
                            if entry is not None and git.get("text_normalization") else
                            {entry["git_blob"]} if entry is not None else set())
                if entry is not None and tracked.get(name.replace(os.sep, "/")) not in accepted:
                    dirty.append(name)
            current = {name.replace(os.sep, "/") for name in files}
            current.update(name for name, entry in dependencies.items() if entry is not None)
            dirty.extend(name for name in tracked if (name.startswith(package.name + "/") and name.endswith(".py")
                or name in dependencies) and name not in current)
        identity = _fingerprint(canonical, files, dependencies, version)
        payload = {"schema": _SCHEMA, "checkout": canonical, "version": version,
            "files": files, "dependencies": dependencies, "git": git, "dirty_paths": sorted(set(dirty)),
            "fingerprint": identity}
        if cache_path is not None and reusable and payload != cached:
            _save_cache(cache_path, payload)
        _LAST_DIAGNOSTICS = {"strategy": "ntfs-usn" if reusable else "full-content",
            "source_files": len(paths), "rehashed_files": rehashed, "dirty_files": len(set(dirty))}
        return identity
    raise RuntimeError("Runtime source changed repeatedly while validating its code identity")


def source_fingerprint(package: Path) -> str:
    package = package.resolve()
    with _CACHE_LOCK:
        try:
            path = _cache_path(package.parent)
            with _file_lock(path):
                return _compute(package, path)
        except OSError:
            # Read-only home, unsupported locking or cache IO failure must not
            # weaken identity validation or prevent a legitimate Runtime start.
            return _compute(package, None)


def full_source_fingerprint(package: Path) -> str:
    """Build identity with the same Runtime scope, always reading its content."""
    with _CACHE_LOCK:
        return _compute(package.resolve(), None)
