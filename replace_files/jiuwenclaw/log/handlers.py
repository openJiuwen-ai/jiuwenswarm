# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Rotating file handlers for runtime logs."""

from __future__ import annotations

import datetime
import logging
import shutil
import sys
from logging.handlers import BaseRotatingHandler
from pathlib import Path

LOG_FILE_MAX_BYTES = 20 * 1024 * 1024
LOG_FILE_BACKUP_COUNT = 20


class SafeRotatingFileHandler(BaseRotatingHandler):
    """Safe rotating file handler."""

    def __init__(
        self,
        filename,
        maxBytes=0,
        backupCount=0,
        encoding=None,
        delay=False,
        errors=None,
    ):
        super().__init__(filename, "a", encoding, errors)
        self.max_bytes = maxBytes
        self.backup_count = backupCount
        self._current_filename = filename

        if delay:
            self.stream = None

    def shouldRollover(self, record):
        if self.stream is None:
            return False
        if self.max_bytes > 0:
            msg = "%s\n" % self.format(record)
            self.stream.seek(0, 2)
            if self.stream.tell() + len(msg) >= self.max_bytes:
                return True
        return False

    def doRollover(self):
        base_path = Path(self.baseFilename)

        timestamp = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_filename = base_path.parent / f"{base_path.stem}_{timestamp}{base_path.suffix}"

        try:
            if base_path.exists():
                shutil.copy2(base_path, backup_filename)
        except OSError as e:
            print(f"WARNING: Could not copy log file to backup: {e}", file=sys.stderr)

        self._cleanup_old_backups()

        try:
            if self.stream:
                self.stream.seek(0)
                self.stream.truncate(0)
        except OSError as e:
            print(f"WARNING: Could not truncate log file: {e}", file=sys.stderr)

    def _cleanup_old_backups(self):
        if self.backup_count <= 0:
            return

        try:
            base_path = Path(self.baseFilename)
            log_dir = base_path.parent

            backup_files = []
            for f in log_dir.glob(f"{base_path.stem}_*{base_path.suffix}"):
                if f.is_file() and f != base_path:
                    backup_files.append(f)

            backup_files.sort(key=lambda x: x.stat().st_mtime)

            files_to_delete = len(backup_files) - self.backup_count
            if files_to_delete > 0:
                for f in backup_files[:files_to_delete]:
                    try:
                        f.unlink()
                    except OSError as e:
                        print(f"WARNING: Could not delete old log file {f}: {e}", file=sys.stderr)
        except Exception as e:
            print(f"WARNING: Error during backup cleanup: {e}", file=sys.stderr)
