# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""将 SDK 日志与逐进程完整 stderr 日志保存在服务日志根下。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

logger = logging.getLogger(__name__)

LOG_DIR_CONFIG_KEY = "OFFICE_CLAW_DEEPRESEARCH_LOG_DIR"
_PREPARE_CLEANUPS: set[asyncio.Task] = set()
_CLOSE_TASKS: set[asyncio.Task] = set()
_WRITE_TASKS: set[asyncio.Task] = set()
_CLOSE_TIMEOUT_SECONDS = 1.0
_WRITE_TIMEOUT_SECONDS = 1.0


def trusted_fallback_log_config(environ: Mapping[str, str]) -> dict[str, str]:
    """只从宿主进程环境选择备用数据根，不接受租户模型配置中的路径。"""
    roots = {}
    for key in ("OFFICEACE_USER_DATA_ROOT", "OFFICE_CLAW_DATA_DIR"):
        raw = str(environ.get(key) or "").strip()
        if not raw:
            continue
        try:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                raise ValueError("数据根必须是绝对路径")
            roots[key] = str(path.resolve())
        except Exception as exc:  # 日志路径解析失败时继续检查其他可信路径。
            logger.warning("DeepResearch 备用日志数据根无效 key=%s type=%s", key, type(exc).__name__)
    host_home = str(environ.get("OFFICE_CLAW_HOST_HOME") or "").strip()
    if not roots and host_home:
        try:
            path = Path(host_home).expanduser()
            if not path.is_absolute():
                raise ValueError("宿主 home 必须是绝对路径")
            roots["OFFICE_CLAW_DATA_DIR"] = str((path / ".office-claw").resolve())
        except Exception as exc:  # 日志路径解析失败时继续检查其他可信路径。
            logger.warning("DeepResearch 宿主备用日志 home 无效 type=%s", type(exc).__name__)
    return roots


def _safe_id(value: str) -> str:
    text = str(value or "")
    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", text):
        return text
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


@dataclass
class RunnerDiagnostics:
    """独占一个二进制日志文件；文件 I/O 由调用方放在线程中执行。"""

    sdk_log_dir: Path
    conversation_id: str
    request_id: str
    attempt_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    cwd: Path | None = None
    stderr_path: Path | None = None
    pid: int | None = None
    bytes_written: int = 0
    failed: bool = False
    _stream: BinaryIO | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def prepare(self) -> None:
        """创建工作根与本次日志文件，不替换此前尝试的文件。"""
        with self._lock:
            try:
                if (
                    not self.sdk_log_dir.is_absolute()
                    or self.sdk_log_dir.name != "logs"
                    or self.sdk_log_dir.parent.name != "output"
                ):
                    raise ValueError("日志目录必须是绝对路径且以 output/logs 结尾")
                base = self.sdk_log_dir.parent.parent
                base.mkdir(parents=True, exist_ok=True)
                self.cwd = base
                journal_root = base / "runner"
                journal_root.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                self.stderr_path = journal_root / (
                    f"{_safe_id(self.conversation_id)}.{stamp}.{self.attempt_id}.stderr.log"
                )
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND
                flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(self.stderr_path, flags, 0o600)
                try:
                    self._stream = os.fdopen(descriptor, "wb", buffering=0)
                except BaseException:
                    os.close(descriptor)
                    raise
            except (OSError, ValueError) as exc:
                self.failed = True
                logger.warning(
                    "DeepResearch 诊断准备失败 sdk_log_dir=%s stderr_log=%s "
                    "attempt=%s conversation_id=%s request_id=%s type=%s; "
                    "保留 cwd=%s 并继续排空 stderr 管道",
                    self.sdk_log_dir, self.stderr_path, self.attempt_id,
                    _safe_id(self.conversation_id), _safe_id(self.request_id),
                    type(exc).__name__, self.cwd or "inherited",
                )

    def started(self, pid: int | None) -> None:
        self.pid = pid
        logger.info(
            "DeepResearch runner 诊断开始 sdk_log_dir=%s stderr_log=%s "
            "pid=%s attempt=%s conversation_id=%s request_id=%s "
            "stderr_enabled=%s prepare_failed=%s",
            self.sdk_log_dir, self.stderr_path, pid, self.attempt_id,
            _safe_id(self.conversation_id), _safe_id(self.request_id),
            self._stream is not None and not self.failed, self.failed,
        )

    def write(self, chunk: bytes) -> None:
        """逐字节追加，保留非 UTF-8 字节；磁盘失败后继续由宿主排空管道。"""
        with self._lock:
            if self._stream is None or self.failed:
                return
            try:
                remaining = memoryview(chunk)
                while remaining:
                    written = self._stream.write(remaining)
                    if not written:
                        raise OSError("stderr 日志写入未取得进展")
                    self.bytes_written += written
                    remaining = remaining[written:]
            except (OSError, ValueError) as exc:
                self.failed = True
                logger.warning(
                    "DeepResearch stderr 日志写入失败 stderr_log=%s pid=%s "
                    "attempt=%s bytes=%s type=%s; 继续排空 stderr 管道",
                    self.stderr_path, self.pid, self.attempt_id,
                    self.bytes_written, type(exc).__name__,
                )

    def incomplete(self, reason: str) -> None:
        self.failed = True
        logger.warning(
            "DeepResearch stderr 采集不完整 stderr_log=%s pid=%s attempt=%s reason=%s",
            self.stderr_path, self.pid, self.attempt_id, reason,
        )

    def close(self) -> None:
        """在 stderr 排空后关闭；不删除仍被 Windows 进程打开的文件。"""
        with self._lock:
            stream, self._stream = self._stream, None
            closed = stream is None
            if stream is not None:
                try:
                    stream.flush()
                    stream.close()
                    closed = True
                except (OSError, ValueError) as exc:
                    self.failed = True
                    logger.warning(
                        "DeepResearch stderr 日志关闭失败 stderr_log=%s "
                        "pid=%s attempt=%s type=%s",
                        self.stderr_path, self.pid, self.attempt_id, type(exc).__name__,
                    )
                    try:
                        stream.close()
                        closed = True
                    except (OSError, ValueError):
                        pass
            logger.info(
                "DeepResearch runner 诊断关闭 stderr_log=%s pid=%s attempt=%s "
                "conversation_id=%s request_id=%s bytes=%s closed=%s complete=%s",
                self.stderr_path, self.pid, self.attempt_id,
                _safe_id(self.conversation_id), _safe_id(self.request_id),
                self.bytes_written, closed, not self.failed,
            )


async def prepare_runner_diagnostics(
    raw_sdk_log_dir: str, conversation_id: str, request_id: str,
) -> RunnerDiagnostics | None:
    """缺省字段保留旧 runner 适配器的兼容路径。"""
    if not raw_sdk_log_dir:
        return None
    capture = RunnerDiagnostics(Path(raw_sdk_log_dir), conversation_id, request_id)
    prepare = asyncio.create_task(asyncio.to_thread(capture.prepare))
    try:
        await asyncio.shield(prepare)
    except asyncio.CancelledError:
        async def close_abandoned_preparation():
            try:
                await prepare
            except Exception as exc:
                capture.incomplete(f"journal_prepare_{type(exc).__name__}")
            finally:
                await close_runner_diagnostics(capture)

        def finished(task: asyncio.Task) -> None:
            _PREPARE_CLEANUPS.discard(task)
            if not task.cancelled():
                task.exception()

        cleanup = asyncio.create_task(close_abandoned_preparation())
        _PREPARE_CLEANUPS.add(cleanup)
        cleanup.add_done_callback(finished)
        raise
    return capture


async def close_runner_diagnostics(capture: RunnerDiagnostics) -> None:
    """磁盘停顿不能无限阻塞子进程取消；超时后持有后台关闭任务。"""
    close = asyncio.create_task(asyncio.to_thread(capture.close))
    _CLOSE_TASKS.add(close)

    def finished(task: asyncio.Task) -> None:
        _CLOSE_TASKS.discard(task)
        if not task.cancelled():
            task.exception()  # 取回超时后后台任务的异常，避免未处理任务警告。

    close.add_done_callback(finished)
    try:
        await asyncio.wait_for(asyncio.shield(close), timeout=_CLOSE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        capture.incomplete("journal_close_timeout")
    except Exception as exc:  # 诊断故障不得覆盖研究结果。
        capture.incomplete(f"journal_close_{type(exc).__name__}")


async def write_runner_diagnostics(capture: RunnerDiagnostics, chunk: bytes) -> None:
    """慢磁盘超过期限后关闭采集，继续排空管道，后台保留正在完成的写入。"""
    if capture.failed:
        return
    write = asyncio.create_task(asyncio.to_thread(capture.write, chunk))
    _WRITE_TASKS.add(write)

    def finished(task: asyncio.Task) -> None:
        _WRITE_TASKS.discard(task)
        if not task.cancelled():
            task.exception()

    write.add_done_callback(finished)
    try:
        await asyncio.wait_for(asyncio.shield(write), timeout=_WRITE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        capture.incomplete("journal_write_timeout")
    except Exception as exc:
        capture.incomplete(f"journal_write_{type(exc).__name__}")
