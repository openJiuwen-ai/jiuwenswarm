# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Iterable, MutableMapping
from pathlib import Path, PureWindowsPath

RUNTIME_DIR_VARS: tuple[tuple[str, str], ...] = (
    ("DEPLOY_DIR", "deploys"),
    ("DIST_DIR", "dist"),
)
_RUNTIME_SUBDIR = "openjiuwen-runtime"

_logger = logging.getLogger("jiuwenswarm.early")


def _default_candidate_roots(environ: MutableMapping[str, str]) -> list[Path]:
    roots: list[Path] = []
    try:
        roots.append(Path(tempfile.gettempdir()) / _RUNTIME_SUBDIR)
    except OSError:  # gettempdir raises FileNotFoundError when no temp dir is usable
        pass
    data_dir = (
        environ.get("JIUWENSWARM_DATA_DIR", "").strip()
        or environ.get("JIUWENCLAW_DATA_DIR", "").strip()
    )
    try:
        data_root = Path(data_dir) if data_dir else Path.home() / ".jiuwenswarm"
    except RuntimeError:  # Path.home() raises when no home directory can be determined
        return roots
    roots.append(data_root / ".runtime-tmp")
    return roots


def _needs_default(value: str) -> bool:
    value = value.strip()
    if not value:
        return True
    path = PureWindowsPath(value)
    return not path.is_absolute() and bool(path.drive or path.root)


def _try_create(root: Path, subdirs: Iterable[str]) -> bool:
    try:
        for sub in subdirs:
            (root / sub).mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return True


def ensure_runtime_work_dirs(
    environ: MutableMapping[str, str] | None = None,
    *,
    is_windows: bool | None = None,
    candidate_roots: Iterable[Path] | None = None,
) -> dict[str, str]:
    env = os.environ if environ is None else environ
    if is_windows is None:
        is_windows = os.name == "nt"
    if not is_windows:
        # On POSIX ``/tmp/...`` is a real absolute path. OHOS (read-only ``/``)
        # gets these variables from its launcher.
        return {}

    missing = [(key, sub) for key, sub in RUNTIME_DIR_VARS if _needs_default(env.get(key, ""))]
    if not missing:
        return {}

    roots = list(candidate_roots) if candidate_roots is not None else _default_candidate_roots(env)
    for root in roots:
        if _try_create(root, (sub for _, sub in missing)):
            applied = {key: str(root / sub) for key, sub in missing}
            for key, value in applied.items():
                original = env.get(key, "").strip()
                if original:
                    _logger.warning(
                        "[runtime_work_dirs] %s=%s 在 Windows 上不是绝对路径，会被解析到安装盘根目录，已改用 %s",
                        key, original, value,
                    )
            env.update(applied)
            return applied

    keys = " / ".join(key for key, _ in missing)
    tried = "；".join(str(r) for r in roots) or "（无可用候选）"
    _logger.error(
        "[runtime_work_dirs] 运行时工作目录不可写，智能体后端可能启动失败。已尝试：%s。"
        "请在环境变量中将 %s 设置为当前用户可写的绝对路径（例如 %%LOCALAPPDATA%%\\Temp\\%s\\deploys），"
        "或检查上述目录的磁盘权限后重启。",
        tried,
        keys,
        _RUNTIME_SUBDIR,
    )
    return {}
