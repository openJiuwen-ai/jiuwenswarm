# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Desktop 本地的"上次选择目录"记忆（M0-b 内联自 web file_picker 的两个纯函数）。

与 gateway 侧 file_picker 读写同一状态文件（``<workspace>/last_file_picker_dir.txt``），
行为等价；desktop 不依赖 gateway 仓侧模块。
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_LAST_DIR_FILENAME = "last_file_picker_dir.txt"


def _last_dir_state_path() -> Path:
    try:
        from jiuwenswarm.common.utils import get_user_workspace_dir

        base = get_user_workspace_dir()
    except Exception:  # noqa: BLE001
        base = Path.home() / ".jiuwenswarm"
    return base / _LAST_DIR_FILENAME


def get_last_file_picker_dir() -> str | None:
    """Return the last remembered picker directory if it still exists."""
    try:
        raw = _last_dir_state_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_dir():
        return None
    try:
        return str(candidate.resolve())
    except Exception:  # noqa: BLE001
        return str(candidate)


def remember_file_picker_dir(path: str | Path | None) -> None:
    """Remember a directory (or the parent of a selected file) for the next open."""
    if path is None:
        return
    try:
        candidate = Path(path).expanduser()
        directory = candidate if candidate.is_dir() else candidate.parent
        if not directory.is_dir():
            return
        try:
            stored = str(directory.resolve())
        except Exception:  # noqa: BLE001
            stored = str(directory)
        state_path = _last_dir_state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(stored + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[picker_dir_state] failed to remember directory: %s", exc)


def resolve_file_picker_initial_dir(initial_dir: str | None = None) -> str:
    """Resolve dialog start directory: explicit → last remembered → home."""
    for raw in (initial_dir, get_last_file_picker_dir()):
        if not raw:
            continue
        candidate = Path(raw).expanduser()
        if candidate.is_dir():
            try:
                return str(candidate.resolve())
            except Exception:  # noqa: BLE001
                return str(candidate)
    return str(Path.home())


# 与 gateway 侧 file_picker._IMAGE_MIME_BY_EXTENSION 逐键对齐（含 .jfif——
# mimetypes 常猜不到它，缺失会让 desktop 选中的 jfif 图片被 server 侧静默丢弃）。
# 两份副本的等价性由 test_desktop_local_file_picker 的漂移守护断言保障。
_IMAGE_MIME_BY_EXTENSION = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jfif": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def attachment_mime_type(filename: str) -> str:
    """先用 mimetypes.guess_type，没有有效结果时再用已知图片扩展名补齐。"""
    import mimetypes

    guessed = mimetypes.guess_type(filename)[0]
    if isinstance(guessed, str):
        guessed = guessed.split(";", 1)[0].strip().lower()
    else:
        guessed = ""
    if guessed and guessed != "application/octet-stream":
        return guessed
    mapped = _IMAGE_MIME_BY_EXTENSION.get(Path(filename).suffix.lower())
    if mapped:
        return mapped
    return guessed or "application/octet-stream"
