# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""上传日志校验与落盘（设计 §4.7）。

端点 multipart 收到 log_files[] 后，经本模块校验（类型白名单 + 大小上限），
落盘 uploads/，返回路径列表供证据收集器读取。随 keep_evidence_days 清理。
上传日志是原始文件，从未过服务端 filter——收集时强制过 _sanitize_log_text。
"""

from __future__ import annotations

import logging
import shutil
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

_ALLOWED_SUFFIXES = {".log", ".txt", ".zip", ".json"}
_CHUNK_SIZE = 64 * 1024  # 流式读写，内存无压力

# 资源边界：防止多 part / 多 zip 成员绕过单文件限制耗尽磁盘。
# 单次诊断上传允许的文件 part 数上限；总字节预算 = max_mb × 该值。
MAX_UPLOAD_FILES = 20
# 单个 zip 允许解出的成员数上限。
_MAX_ZIP_MEMBERS = 500


class UploadValidationError(ValueError):
    """上传校验失败（类型/大小/解包）。"""


async def save_uploaded_logs(
    files: list,  # list[UploadFile]
    session_id: str,
    diagnosis_dir: Path,
    *,
    max_mb: int = 50,
    allow: bool = True,
    max_files: int = MAX_UPLOAD_FILES,
) -> list[Path]:
    """校验并落盘上传日志文件，返回落盘路径列表。

    zip 解包后逐文件同规则校验（防 zip 套 zip 穿透）。
    ``UploadFile.read`` 是协程，必须 await——本函数为 async。

    资源边界：单文件上限 ``max_mb`` 之外，额外约束**总量**——所有 part 与
    zip 成员的实际落盘字节之和不得超过 ``max_mb × max_files`` 预算，且单个
    zip 成员数不超过 ``_MAX_ZIP_MEMBERS``。任意一项越界即整批拒绝并清理已落盘
    文件，避免多 part / 多 zip 成员绕过单文件限制耗尽磁盘。
    """
    if not allow:
        raise UploadValidationError("diagnosis.allow_log_upload=false，不允许上传日志")
    if not files:
        return []

    session_upload_dir = diagnosis_dir / session_id / "uploads"
    session_upload_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = max_mb * 1024 * 1024
    total_budget = max_bytes * max_files
    # 累计实际落盘字节（zip 自身仅中转会被删除，不计入；仅最终保留文件计入）。
    state: dict[str, int] = {"total": 0}
    saved: list[Path] = []
    try:
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
                # zip 自身仅临时中转、删除且不计入总预算；提取出的成员才计入。
                _unzip_in_place(dest, session_upload_dir, max_bytes, saved, state, total_budget)
                dest.unlink(missing_ok=True)
            else:
                state["total"] += written
                if state["total"] > total_budget:
                    dest.unlink(missing_ok=True)
                    raise UploadValidationError("上传总大小超过上限")
                saved.append(dest)
            logger.info(
                "[diagnosis] saved uploaded log %s (%d bytes) for session %s",
                name, written, session_id,
            )
    except UploadValidationError:
        # 整批拒绝：清理本次已落盘的全部上传，避免磁盘残留 / inode 泄漏。
        shutil.rmtree(session_upload_dir, ignore_errors=True)
        saved.clear()
        raise
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


def _unzip_in_place(  # pylint: disable=huawei-too-many-arguments
    zip_path: Path,
    dest_dir: Path,
    max_bytes: int,
    saved: list[Path],
    state: dict[str, int],
    total_budget: int,
) -> None:
    """解包 zip，逐文件校验后落盘。防 zip 套 zip 与路径穿越。

    资源边界（与设计 §4.7 / CR-3 对齐）：
      - 成员数上限 ``_MAX_ZIP_MEMBERS``，避免海量小文件耗尽 inode；
      - 单成员除声明 ``file_size`` 预检外，按**实际解压字节**再判一次上限，
        防压缩比 zip bomb 绕过声明大小；
      - 所有成员实际解压字节累加进 ``state["total"]``，超过 ``total_budget``
        （= max_mb × max_files）即整批拒绝。
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = [i for i in zf.infolist() if not i.is_dir()]
            if len(members) > _MAX_ZIP_MEMBERS:
                raise UploadValidationError(f"zip 成员数超过上限（{_MAX_ZIP_MEMBERS}）")
            for info in members:
                name = Path(info.filename).name  # 防路径穿越，只取文件名
                if not name or Path(name).suffix.lower() not in _ALLOWED_SUFFIXES:
                    continue
                if info.file_size > max_bytes:
                    raise UploadValidationError(f"zip 内文件 {name} 超过单文件大小上限")
                dest = dest_dir / name
                written = 0
                # 二次校验：声明大小可能伪造，必须按实际解压字节累加并判上限。
                with zf.open(info) as src, dest.open("wb") as dst:
                    while True:
                        chunk = src.read(_CHUNK_SIZE)
                        if not chunk:
                            break
                        dst.write(chunk)
                        written += len(chunk)
                        state["total"] += len(chunk)
                        if written > max_bytes:
                            dest.unlink(missing_ok=True)
                            raise UploadValidationError(f"zip 内文件 {name} 解压后超过大小上限")
                        if state["total"] > total_budget:
                            dest.unlink(missing_ok=True)
                            raise UploadValidationError("上传总大小超过上限")
                saved.append(dest)
    except zipfile.BadZipFile as exc:
        raise UploadValidationError(f"无效的 zip 文件: {exc}") from exc
