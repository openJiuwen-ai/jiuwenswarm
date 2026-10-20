"""Build provenance shipped with the application and installed dependency identity."""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import sys
import time
import uuid
from importlib import metadata
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)
_BUILD_INFO_PATH = Path(__file__).resolve().parents[1] / "resources" / "build-info.json"
_SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")


def _packaged_build_info() -> dict[str, Any]:
    try:
        payload = json.loads(_BUILD_INFO_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("Cannot read packaged build information: %s", exc)
        return {}
    source = payload.get("jiuwenswarm") if isinstance(payload, dict) else None
    if not isinstance(source, dict):
        return {}
    branch = source.get("branch")
    commit = source.get("commit_id")
    if not isinstance(branch, str) or not isinstance(commit, str) or not _SHA_PATTERN.fullmatch(commit):
        return {}
    return {
        **payload,
        "jiuwenswarm": {key: value for key, value in source.items() if key != "repository"},
    }


def _installed_agent_core(packaged: dict[str, Any]) -> dict[str, str]:
    try:
        distribution = metadata.distribution("openjiuwen")
    except metadata.PackageNotFoundError:
        return {"source": "unknown"}

    version = distribution.version
    try:
        direct_url_text = distribution.read_text("direct_url.json")
        direct_url = json.loads(direct_url_text) if direct_url_text else {}
    except (OSError, ValueError):
        direct_url = {}
    vcs = direct_url.get("vcs_info") if isinstance(direct_url, dict) else None
    if isinstance(vcs, dict) and vcs.get("vcs") == "git":
        commit = vcs.get("commit_id")
        if isinstance(commit, str) and _SHA_PATTERN.fullmatch(commit):
            info = {"source": "git", "version": version, "commit_id": commit}
            built_core = packaged.get("agent_core")
            if isinstance(built_core, dict) and built_core.get("commit_id") == commit:
                branch = built_core.get("branch")
                if isinstance(branch, str) and branch:
                    info["branch"] = branch
            return info
    return {"source": "package", "version": version}


def get_runtime_build_info() -> dict[str, Any]:
    """Return packaged upstream identity plus the currently installed SDK identity."""
    packaged = _packaged_build_info()
    if not packaged:
        return {}
    return {
        "jiuwenswarm": packaged["jiuwenswarm"],
        "agent_core": _installed_agent_core(packaged),
    }


def _prepare_private_copy(source: Path, temporary: Path) -> None:
    """Create a replacement with the config's permissions before writing secrets."""
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    if os.name == "nt":
        # Apply the existing DACL before any YAML content is written.
        import ctypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        get_security = advapi32.GetFileSecurityW
        get_security.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_void_p,
                                 ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
        get_security.restype = ctypes.c_int
        set_security = advapi32.SetFileSecurityW
        set_security.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_void_p]
        set_security.restype = ctypes.c_int
        dacl_info = 0x00000004
        required = ctypes.c_uint32()
        get_security(str(source), dacl_info, None, 0, ctypes.byref(required))
        if required.value == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        descriptor = ctypes.create_string_buffer(required.value)
        if not get_security(str(source), dacl_info, descriptor, required.value,
                            ctypes.byref(required)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not set_security(str(temporary), dacl_info, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    elif sys.platform == "darwin":
        # fcopyfile copies POSIX mode and ACL metadata without copying secrets.
        import ctypes

        copy_security = ctypes.CDLL("libSystem.B.dylib", use_errno=True).fcopyfile
        copy_security.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        copy_security.restype = ctypes.c_int
        with source.open("rb") as original, temporary.open("r+b") as replacement:
            if copy_security(original.fileno(), replacement.fileno(), None, 0x3) != 0:
                raise OSError(ctypes.get_errno(), "Cannot preserve config permissions")


def _replace_config_file(temporary: Path, config_path: Path) -> None:
    if os.name != "nt":
        os.replace(temporary, config_path)
        return

    import ctypes

    replace_file = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
    replace_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                             ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
    replace_file.restype = ctypes.c_int
    if not replace_file(str(config_path), str(temporary), None, 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


def sync_build_info_to_config(config_path: Path) -> bool:
    """Keep the local config's diagnostic identity aligned with this installation."""
    from jiuwenswarm.common.utils import is_package_installation

    if not is_package_installation():
        return False
    info = get_runtime_build_info()
    if not info or not config_path.is_file():
        return False

    import portalocker
    from ruamel.yaml import YAML
    from ruamel.yaml.error import YAMLError
    from ruamel.yaml.util import load_yaml_guess_indent

    lock_path = config_path.with_name(f"{config_path.stem}.lock")
    try:
        with portalocker.Lock(str(lock_path), timeout=10):
            yaml = YAML()
            yaml.preserve_quotes = True
            with config_path.open("r", encoding="utf-8") as handle:
                data, _, _ = load_yaml_guess_indent(handle, yaml=yaml)
            if not isinstance(data, dict) or data.get("build_info") == info:
                return False
            if "build_info" not in data and hasattr(data, "insert"):
                data.insert(1, "build_info", info)
            else:
                data["build_info"] = info
            yaml.default_flow_style = False
            yaml.indent(mapping=2, sequence=4, offset=2)
            yaml.width = 4096
            temporary = config_path.with_name(f"{config_path.stem}.{uuid.uuid4().hex}.yaml.tmp")
            try:
                _prepare_private_copy(config_path, temporary)
                with temporary.open("w", encoding="utf-8") as handle:
                    yaml.dump(data, handle)
                if os.name != "nt" and sys.platform != "darwin":
                    os.chmod(temporary, stat.S_IMODE(config_path.stat().st_mode))
                for attempt in range(10):
                    try:
                        _replace_config_file(temporary, config_path)
                        break
                    except OSError as exc:
                        if attempt == 9 or (exc.errno != 13 and getattr(exc, "winerror", None) not in (5, 32)):
                            raise
                        time.sleep(0.05 * (attempt + 1))
            finally:
                temporary.unlink(missing_ok=True)
    except (OSError, portalocker.exceptions.LockException, YAMLError) as exc:
        logger.warning("Cannot update config build information at %s: %s", config_path, exc)
        return False
    return True
