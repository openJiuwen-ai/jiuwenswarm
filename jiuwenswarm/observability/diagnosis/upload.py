# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""上传日志校验与落盘（设计 §4.7）。

端点 multipart 收到 log_files[] 后，经本模块校验（类型白名单 + 大小上限），
落盘 uploads/，返回路径列表供证据收集器读取。随 keep_evidence_days 清理。
上传日志是原始文件，从未过服务端 filter——收集时强制过 _sanitize_log_text。
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

_ALLOWED_SUFFIXES = {".log", ".txt", ".zip", ".json"}
_CHUNK_SIZE = 64 * 1024  # 流式读写，内存无压力


class UploadValidationError(ValueError):
    """上传校验失败（类型/大小/解包）。"""


async def save_uploaded_logs(
    files: list,  # list[UploadFile]
    session_id: str,
    diagnosis_dir: Path,
    *,
    max_mb: int = 50,
    allow: bool = True,
) -> list[Path]:
    """校验并落盘上传日志文件，返回落盘路径列表。

    zip 解包后逐文件同规则校验（防 zip 套 zip 穿透）。
    ``UploadFile.read`` 是协程，必须 await——本函数为 async。
    """
    if not allow:
        raise UploadValidationError("diagnosis.allow_log_upload=false，不允许上传日志")
    if not files:
        return []

    session_upload_dir = diagnosis_dir / session_id / "uploads"
    session_upload_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = max_mb * 1024 * 1024
    saved: list[Path] = []

    for upload in files:
        if upload.filename is None:
            continue
        name = Path(upload.filename).name  # 防路径穿越
        suffix = Path(name).suffix.lower()
        if suffix not in _ALLOWED_SUFFIXES:
            raise UploadValidationError(f"不支持的文件类型: {name}（仅 .log/.txt/.zip/.json）")
        dest = session_upload_dir / name
        written = await _stream_save(upload, dest, max_bytes)
        if suffix == ".zip":
            _unzip_in_place(dest, session_upload_dir, max_bytes, saved)
            dest.unlink(missing_ok=True)
        else:
            saved.append(dest)
        logger.info(
            "[diagnosis] saved uploaded log %s (%d bytes) for session %s",
            name, written, session_id,
        )
    return saved


async def _stream_save(upload, dest: Path, max_bytes: int) -> int:
    """流式落盘单文件，校验大小上限。返回写入字节数。

    ``upload.read`` 是 async（FastAPI UploadFile / Starlette UploadFile 均
    为协程）；同步调用会拿到 coroutine 而非 bytes，导致 TypeError。
    """
    written = 0
    with dest.open("wb") as fp:
        while True:
            chunk = await upload.read(_CHUNK_SIZE)
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            written += len(chunk)
            if written > max_bytes:
                fp.close()
                dest.unlink(missing_ok=True)
                raise UploadValidationError(f"文件 {dest.name} 超过大小上限")
            fp.write(chunk)
    return written


def _unzip_in_place(zip_path: Path, dest_dir: Path, max_bytes: int, saved: list[Path]) -> None:
    """解包 zip，逐文件校验后落盘。防 zip 套 zip 与路径穿越。"""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = Path(info.filename).name  # 防路径穿越，只取文件名
                if not name or Path(name).suffix.lower() not in _ALLOWED_SUFFIXES:
                    continue
                if info.file_size > max_bytes:
                    raise UploadValidationError(f"zip 内文件 {name} 超过大小上限")
                dest = dest_dir / name
                with zf.open(info) as src, dest.open("wb") as dst:
                    while True:
                        chunk = src.read(_CHUNK_SIZE)
                        if not chunk:
                            break
                        dst.write(chunk)
                saved.append(dest)
    except zipfile.BadZipFile as exc:
        raise UploadValidationError(f"无效的 zip 文件: {exc}") from exc
