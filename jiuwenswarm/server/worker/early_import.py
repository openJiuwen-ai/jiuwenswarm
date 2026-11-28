# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Worker-owned SDK import started before the private identity pipe is read."""

from __future__ import annotations

from concurrent.futures import Future
import importlib.util
import logging
from pathlib import Path
import sys
import threading
import time

logger = logging.getLogger(__name__)


def _clean_description_layout() -> bool:
    if getattr(sys, "frozen", False):
        return True
    try:
        spec = importlib.util.find_spec("openjiuwen")
    except (ImportError, ValueError):
        return False
    if spec is None or not spec.origin:
        return False
    descriptions = Path(spec.origin).parent / "agent_teams/tools/locales/descs"
    try:
        if not descriptions.is_dir():
            return True
        for language in descriptions.iterdir():
            if not language.is_dir():
                continue
            flat = {file.stem for file in language.glob("*.md")}
            if any(file.parent != language and "fragments" not in file.relative_to(language).parts
                   and file.stem in flat for file in language.rglob("*.md")):
                return False
    except OSError:
        return False
    return True


class SDKImportTicket:
    def __init__(self) -> None:
        self._result: Future[None] = Future()
        self._thread = threading.Thread(target=self._load, name="worker-early-sdk", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _load(self) -> None:
        began = time.monotonic()
        try:
            # The default SDK logger otherwise creates files during import.
            # This public bootstrap config is replaced after startup acceptance.
            from openjiuwen.core.common.logging.log_config import configure_log_config

            configure_log_config({"backend": "default", "level": "INFO", "output": ["console"],
                                  "interface_output": ["console"], "performance_output": ["console"]})
            from jiuwenswarm.server.worker.runtime_backend import _preload_sdk_execution_modules

            _preload_sdk_execution_modules()
        except BaseException as error:
            self._result.set_exception(error)
        else:
            self._result.set_result(None)
        finally:
            logger.info("[Worker] preparation stage=early_sdk_import duration_ms=%.3f",
                        (time.monotonic() - began) * 1000)

    def wait(self) -> None:
        self._result.result()


_TICKET: SDKImportTicket | None = None


def start_early_import() -> SDKImportTicket | None:
    global _TICKET
    if _TICKET is None and _clean_description_layout():
        _TICKET = SDKImportTicket()
        _TICKET.start()
    return _TICKET


def current_import_ticket() -> SDKImportTicket | None:
    return _TICKET
