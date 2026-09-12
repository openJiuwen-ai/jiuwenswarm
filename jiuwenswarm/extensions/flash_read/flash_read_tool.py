# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashReadFileTool — Enhanced ReadFileTool with parallel multi-file support.

Subclass of stock ``ReadFileTool``:
- ``file_path`` (string): single file, identical to stock (full backward compat)
- ``file_paths`` (array): multiple files, parallel reading via asyncio.gather
- Each file independently typed (text/image/PDF/notebook/office) and read
- Per-file errors isolated (one failure does not kill the batch)
- Read state registry updated per text file for EditFileTool/WriteFileTool
- offset/limit/pages only apply to single-file mode

The extension patches ``openjiuwen.harness.tools.filesystem.ReadFileTool``
so that SlimSysOperationRail / SysOperationRail pick up this subclass.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from openjiuwen.harness.tools.base_tool import ToolOutput
from openjiuwen.harness.tools.filesystem import (
    ReadFileTool,
    _ReadStateRecord,
    _resolve_tool_file_path,
)

logger = logging.getLogger(__name__)

MAX_FILES_PER_CALL = 10


FLASH_READ_DESCRIPTION: Dict[str, str] = {
    "cn": (
        "增强版文件读取工具，支持文本、图片、PDF、Jupyter Notebook "
        "及 Office 文档（.docx/.xlsx/.pptx）。"
        "支持通过 file_paths 参数传入多个文件路径并行读取，"
        "适用于批量查看源码、配置文件等场景（最多 10 个文件）。"
        "工具被限制在工作空间沙箱内，无法访问工作区外文件，"
        "属于只读文件操作工具。"
    ),
    "en": (
        "Enhanced file reader for text, images, PDFs, Jupyter notebooks, "
        "and Office documents (.docx/.xlsx/.pptx). "
        "Supports parallel reading of multiple files via the file_paths parameter "
        "(up to 10 files per call). "
        "Restricted to the workspace sandbox; cannot access files outside the workspace. "
        "Read-only file operation."
    ),
}


def get_flash_read_input_params(language: str = "cn") -> Dict[str, Any]:
    if language == "en":
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path of a single file to read",
                },
                "file_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Multiple file paths to read in parallel (up to 10). "
                        "Use this instead of file_path for batch reading. "
                        "offset/limit/pages do not apply in multi-file mode."
                    ),
                },
                "offset": {
                    "type": "integer",
                    "description": "Lines to skip before reading (single-file mode only)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum lines to read (single-file mode only)",
                },
                "pages": {
                    "type": "string",
                    "description": "PDF page range, e.g. '1-5' (single-file mode only)",
                },
            },
        }
    return {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "单个文件的绝对路径",
            },
            "file_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "多个文件路径列表，并行读取（最多 10 个）。"
                    "传入此参数时 offset/limit/pages 不生效"
                ),
            },
            "offset": {
                "type": "integer",
                "description": "要跳过的行数（仅单文件模式生效）",
            },
            "limit": {
                "type": "integer",
                "description": "最多读取的行数（仅单文件模式生效）",
            },
            "pages": {
                "type": "string",
                "description": "PDF 页码范围，如 '1-5'（仅单文件模式生效）",
            },
        },
    }


class FlashReadFileTool(ReadFileTool):
    """ReadFileTool with parallel multi-file reading support."""

    def __init__(
        self,
        operation: Any,
        language: str = "cn",
        agent_id: Optional[str] = None,
        enable_image_multimodal: bool = True,
    ) -> None:
        super().__init__(
            operation,
            language,
            agent_id=agent_id,
            enable_image_multimodal=enable_image_multimodal,
        )
        lang = language or "cn"
        self.card.description = FLASH_READ_DESCRIPTION.get(
            lang, FLASH_READ_DESCRIPTION["cn"]
        )
        self.card.input_params = get_flash_read_input_params(lang)

    # ------------------------------------------------------------------
    # Single-file reader (reused in both single and multi modes)
    # ------------------------------------------------------------------

    async def _read_one_file(
        self, raw_path: str, model_name: str
    ) -> Dict[str, Any]:
        """Read a single file and return a structured result dict.

        Encapsulates the per-file logic from stock ``invoke()`` so it can be
        called in parallel for multi-file mode.  Returns a dict instead of
        ToolOutput so the caller can aggregate results.
        """
        try:
            file_path = _resolve_tool_file_path(self.operation, raw_path)
        except ValueError as exc:
            return self._error_result(raw_path, str(exc))

        if self._is_blocked_device(file_path):
            return self._error_result(
                file_path, f"Reading device file '{file_path}' is not allowed."
            )

        if (
            not self._is_binary_check_exempt(file_path)
            and not self._is_plain_text_candidate(file_path)
        ):
            return self._error_result(
                file_path,
                f"Binary files cannot be read as text: '{os.path.basename(file_path)}'.",
            )

        mtime_ns = 0
        size_bytes = 0
        try:
            _st = os.stat(file_path)
            mtime_ns = _st.st_mtime_ns
            size_bytes = _st.st_size
        except OSError:
            pass

        if (
            self._is_plain_text_candidate(file_path)
            and size_bytes > self.MAX_SIZE_BYTES
        ):
            return self._error_result(
                file_path,
                f"File content ({size_bytes / 1024:.1f}KB) exceeds maximum "
                f"({self.MAX_SIZE_BYTES // 1024}KB). "
                "Use single-file mode with offset/limit to read portions.",
            )

        try:
            if self._is_pdf(file_path):
                rendered = await self._read_pdf(file_path, None)
                file_type = "pdf"
            elif self._is_notebook(file_path):
                rendered = await self._read_notebook(file_path)
                file_type = "notebook"
            elif self._is_image(file_path):
                rendered = await self._read_image(file_path, model_name)
                file_type = "image"
            elif self._is_office_doc(file_path):
                rendered = await self._read_office_doc(file_path)
                file_type = "office"
            else:
                rendered = await self._read_text(
                    file_path, 0, self.MAX_LINES_TO_READ, apply_size_cap=False
                )
                file_type = "text"
        except Exception as exc:
            return self._error_result(file_path, str(exc))

        if isinstance(rendered, dict):
            content = str(rendered.get("content", ""))
            multimodal = list(rendered.get("multimodal", []))
        else:
            content = str(rendered)
            multimodal = []

        line_count = len(content.splitlines()) if content else 0

        if self._is_text_read_for_edit(file_path):
            try:
                await self._record_read_state(
                    file_path=file_path,
                    record=_ReadStateRecord(
                        mtime_ns=mtime_ns,
                        size_bytes=size_bytes,
                        is_partial=False,
                        offset=0,
                        line_count=line_count,
                    ),
                )
            except Exception as exc:
                logger.debug(
                    "[FlashRead] _record_read_state failed for %s: %s",
                    file_path,
                    exc,
                )

        return {
            "file_path": file_path,
            "content": content,
            "type": file_type,
            "multimodal": multimodal,
            "line_count": line_count,
            "error": None,
        }

    @staticmethod
    def _error_result(file_path: str, error: str) -> Dict[str, Any]:
        return {
            "file_path": file_path,
            "content": "",
            "type": "error",
            "multimodal": [],
            "line_count": 0,
            "error": error,
        }

    # ------------------------------------------------------------------
    # Multi-file parallel reader
    # ------------------------------------------------------------------

    async def _invoke_multi(
        self, file_paths: List[str], model_name: str
    ) -> ToolOutput:
        """Read multiple files in parallel and return aggregated result."""
        if len(file_paths) > MAX_FILES_PER_CALL:
            return ToolOutput(
                success=False,
                error=(
                    f"Cannot read more than {MAX_FILES_PER_CALL} files at once. "
                    f"Got {len(file_paths)} paths."
                ),
            )

        tasks = [self._read_one_file(fp, model_name) for fp in file_paths]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        files_data: List[Dict[str, Any]] = []
        multimodal_all: List[Any] = []
        succeeded = 0
        failed = 0

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                files_data.append(
                    self._error_result(file_paths[i], str(result))
                )
                failed += 1
            elif isinstance(result, dict):
                files_data.append(result)
                if result.get("error"):
                    failed += 1
                else:
                    succeeded += 1
                if result.get("multimodal"):
                    multimodal_all.extend(result["multimodal"])

        return ToolOutput(
            success=succeeded > 0,
            data={
                "files": files_data,
                "total": len(file_paths),
                "succeeded": succeeded,
                "failed": failed,
                "parallel_read": True,
                "multimodal": multimodal_all,
            },
        )

    # ------------------------------------------------------------------
    # invoke / stream
    # ------------------------------------------------------------------

    async def invoke(self, inputs: Dict[str, Any], **kwargs) -> ToolOutput:
        file_paths = inputs.get("file_paths")

        if file_paths and isinstance(file_paths, list):
            paths = [str(p) for p in file_paths if p]
            if not paths:
                return ToolOutput(
                    success=False, error="file_paths is empty after filtering."
                )
            model_name = self._resolve_model_name(kwargs)
            return await self._invoke_multi(paths, model_name)

        return await super().invoke(inputs, **kwargs)

    async def stream(self, inputs: Dict[str, Any], **kwargs) -> Any:
        pass


__all__ = ["FlashReadFileTool"]
