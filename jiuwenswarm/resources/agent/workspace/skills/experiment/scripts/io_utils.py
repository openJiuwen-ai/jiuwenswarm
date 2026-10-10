"""Filesystem and serialization helpers used by the experiment runtime."""

from __future__ import annotations

import csv
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping


_SLUG_RE = re.compile(r"[^\w.-]+", flags=re.UNICODE)

_SAFE_CHILD_ENV_NAMES = {
    "APPDATA",
    "COMSPEC",
    "CONDA_PREFIX",
    "CUDA_DEVICE_ORDER",
    "CUDA_VISIBLE_DEVICES",
    "DYLD_LIBRARY_PATH",
    "HOME",
    # 下载源 / 镜像 / 超时 / 重试配置（见 _network.py）。预处理子进程也可能联网
    # 取数据，不透传的话子进程会退回默认单源，镜像配置在那一层静默失效。
    "JIUWENSWARM_HF_BASES",
    "JIUWENSWARM_HF_PARQUET_BASES",
    "JIUWENSWARM_SEARCH_TIMEOUT_S",
    "JIUWENSWARM_DOWNLOAD_TIMEOUT_S",
    "JIUWENSWARM_DOWNLOAD_RETRIES",
    # 跨运行数据缓存位置。仅包含路径，不包含认证信息；预处理子进程需要
    # 看见同一位置，避免再次下载或把缓存误判为缺失。
    "JIUWENSWARM_DATASET_CACHE",
    "LANG",
    "LC_ALL",
    "LD_LIBRARY_PATH",
    "LOGNAME",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "NVIDIA_DRIVER_CAPABILITIES",
    "NVIDIA_VISIBLE_DEVICES",
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "REQUESTS_CA_BUNDLE",
    "SHELL",
    "SSL_CERT_FILE",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "USER",
    "VIRTUAL_ENV",
    "WINDIR",
}


def slugify(value: str) -> str:
    slug = _SLUG_RE.sub("-", value.strip()).strip("-._")
    return slug[:120] or "item"


def resolve_run_dir(run_dir: str, *, base_dir: Path | None = None) -> Path:
    path = Path(run_dir)
    if not path.is_absolute():
        path = (base_dir or Path.cwd()) / path
    return path.resolve()


def safe_join(base: Path, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise ValueError(f"expected a relative path, got: {relative_path}")
    resolved_base = base.resolve()
    resolved = (resolved_base / candidate).resolve()
    if resolved != resolved_base and resolved_base not in resolved.parents:
        raise ValueError(f"path escapes run directory: {relative_path}")
    return resolved


def relative_to_run(path: Path, run_dir: Path) -> str:
    resolved_run = run_dir.resolve()
    resolved_path = path.resolve()
    if resolved_path != resolved_run and resolved_run not in resolved_path.parents:
        raise ValueError(f"artifact is outside run directory: {resolved_path}")
    return resolved_path.relative_to(resolved_run).as_posix()


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            file.write(content)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_csv_atomic(
    path: Path,
    fieldnames: list[str],
    rows: Iterable[Mapping[str, Any]],
) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    row_count = 0
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
                row_count += 1
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return row_count


def expand_placeholders(value: str, variables: Mapping[str, Any]) -> str:
    class StrictVariables(dict[str, str]):
        def __missing__(self, key: str) -> str:
            raise KeyError(f"unknown command placeholder: {key}")

    normalized = StrictVariables({key: str(item) for key, item in variables.items()})
    return value.format_map(normalized)


def build_subprocess_environment(
    configured: Mapping[str, str],
    required_names: Iterable[str],
) -> dict[str, str]:
    """Build a fail-closed child environment without leaking unrelated secrets."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _SAFE_CHILD_ENV_NAMES
    }
    missing: list[str] = []
    for name in required_names:
        if name not in os.environ:
            missing.append(name)
        else:
            environment[name] = os.environ[name]
    if missing:
        raise ValueError(
            "required environment variables are missing: " + ", ".join(missing)
        )
    environment.update(configured)
    return environment


def redact_command(arguments: list[str]) -> str:
    return " ".join(_quote_for_display(item) for item in redact_arguments(arguments))


def portable_arguments(
    arguments: list[str],
    *,
    run_dir: Path | None = None,
) -> list[str]:
    """Redact secrets and replace machine-specific path prefixes."""

    replacements: list[tuple[str, str]] = []
    if run_dir is not None:
        replacements.append((str(run_dir.resolve()), "{run_dir}"))
    replacements.extend(
        [
            (str(Path(sys.executable).resolve()), "{python}"),
            (str(Path.home().resolve()), "{user_home}"),
        ]
    )
    replacements.sort(key=lambda item: len(item[0]), reverse=True)
    portable: list[str] = []
    for argument in redact_arguments(arguments):
        value = argument
        for source, target in replacements:
            value = value.replace(source, target)
            value = value.replace(source.replace("\\", "/"), target)
        portable.append(value)
    return portable


def portable_command(arguments: list[str], *, run_dir: Path | None = None) -> str:
    return " ".join(
        _quote_for_display(item)
        for item in portable_arguments(arguments, run_dir=run_dir)
    )


def redact_arguments(arguments: list[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    for argument in arguments:
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue
        option = argument.split("=", 1)[0]
        if _is_sensitive_argument_name(option):
            if "=" in argument:
                redacted.append(f"{argument.split('=', 1)[0]}=<redacted>")
            else:
                redacted.append(argument)
                redact_next = True
            continue
        redacted.append(argument)
    return redacted


def _is_sensitive_argument_name(value: str) -> bool:
    normalized = value.lower().replace("-", "_").lstrip("_")
    names = {
        "token",
        "access_token",
        "auth_token",
        "secret",
        "client_secret",
        "password",
        "api_key",
        "private_key",
    }
    return normalized in names or any(
        marker in normalized
        for marker in (
            "token",
            "secret",
            "password",
            "api_key",
            "apikey",
            "private_key",
            "access_key",
            "credential",
        )
    )


def _quote_for_display(argument: str) -> str:
    if not argument or any(character.isspace() for character in argument):
        return json.dumps(argument, ensure_ascii=False)
    return argument
