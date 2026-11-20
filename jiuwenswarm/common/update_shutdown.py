# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Per-update shutdown receipts shared by the desktop and detached installers."""
from __future__ import annotations

import os
import secrets
import tempfile
from pathlib import Path


def create_shutdown_receipt(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"shutdown-{secrets.token_hex(16)}.result"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write("pending\n")
    return path


def complete_shutdown_receipt(path: Path, *, ok: bool) -> None:
    """Only the desktop owner completes an attempt; failure cannot become success."""
    previous = path.read_text(encoding="utf-8")
    if previous not in {"pending\n", "ok\n", "failed\n"} or ok and previous == "failed\n":
        raise RuntimeError("Update shutdown receipt cannot be marked successful")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", prefix=path.name, dir=path.parent, delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write("ok\n" if ok else "failed\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def require_clean_shutdown(path: str | Path | None) -> None:
    """An absent Worker is insufficient without this update attempt's success."""
    try:
        if path and Path(path).read_text(encoding="utf-8") == "ok\n":
            return
    except OSError as exc:
        raise RuntimeError("Update refused: successful shutdown receipt is unavailable") from exc
    raise RuntimeError("Update refused: desktop shutdown did not complete successfully")
