# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashReadFileTool — Enhanced ReadFileTool with parallel multi-file support.

Subclass of stock ``ReadFileTool``:
- ``file_path`` (string): single file, identical to stock (full backward compat)
- ``file_paths`` (array): multiple files, parallel reading via asyncio.gather
- Each file independently typed (text/image/PDF/notebook/office) and read
- Per-file errors isolated (one failure does not kill the batch)
- Read state registry updated per text file for EditFileTool/WriteFileTool
- offset/limit/pages only apply to single-file mode

Instantiated directly by :class:`SlimSysOperationRail
<jiuwenswarm.agents.harness.flash.slim_sys_operation_rail.SlimSysOperationRail>`
(flash filesystem rail), replacing the stock read_file registration.

Relies on stock private helpers (``_resolve_tool_file_path`` / ``_read_text`` /
``_record_read_state`` / ``_ReadStateRecord`` ...); re-run the flash
read → edit same-file regression when upgrading agent-core.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
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
                if file_path.lower().endswith(".xlsx"):
                    rendered = self._annotate_empty_xlsx(rendered)
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

    @staticmethod
    def _annotate_empty_xlsx(rendered: str | dict) -> str | dict:
        """Prepend a warning banner if xlsx output has only empty default sheet.

        The _read_office_doc / _read_xlsx_text renderer outputs one ``## SheetName``
        header per worksheet plus markdown table rows for data, with line-number
        prefixes (``     1\\t``).  When no data rows exist and only one sheet header
        (``## Sheet1``) appears, the workbook is effectively empty — inject a
        visible banner so the model cannot ignore it.
        """
        content = rendered.get("content", "") if isinstance(rendered, dict) else str(rendered)
        stripped = content.strip()
        if not stripped:
            return rendered
        # Strip line-number prefix ("     1\t...") used by the renderer's _cat_n.
        _prefix_re = re.compile(r"^\s*\d+\t", re.M)
        stripped = _prefix_re.sub("", stripped)
        # Detect: exactly one sheet header, zero data rows
        header_count = 0
        data_lines = 0
        for line in stripped.splitlines():
            line_stripped = line.strip()
            if line_stripped.startswith("## "):
                header_count += 1
            elif line_stripped.startswith("|"):
                data_lines += 1
        if header_count <= 1 and data_lines == 0:
            banner = (
                "⚠️ [WARNING] 该工作簿内容为空（仅含默认空白 Sheet1），不含任何数据。"
                "请重新检查构建环节是否正确。\n\n"
            )
            if isinstance(rendered, dict):
                rendered["content"] = banner + content
                return rendered
            return banner + content
        return rendered

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

        # 全部失败时补汇总 error：ToolOutput 约定 success=False 时 error 非空，
        # 逐文件详情仍在 data.files 里。
        error_msg = (
            f"All {len(file_paths)} files failed to read (see per-file errors in data.files)"
            if succeeded == 0
            else None
        )
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
            error=error_msg,
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

        result = await super().invoke(inputs, **kwargs)
        return self._annotate_single_xlsx(inputs, result)

    def _annotate_single_xlsx(
        self, inputs: Dict[str, Any], result: ToolOutput
    ) -> ToolOutput:
        """Apply the empty-xlsx banner on the single-file path too.

        Single-file reads go through stock ``ReadFileTool.invoke`` directly,
        bypassing ``_read_one_file``; without this the banner never shows for
        the most common ``read_file(file_path=...)`` form.
        """
        file_path = str(inputs.get("file_path") or "")
        if not file_path.lower().endswith(".xlsx"):
            return result
        if result is None or not isinstance(result.data, dict):
            return result
        content = result.data.get("content")
        if not isinstance(content, str):
            return result
        annotated = self._annotate_empty_xlsx({"content": content})
        new_content = annotated.get("content") if isinstance(annotated, dict) else None
        if new_content is None or new_content == content:
            return result
        new_data = dict(result.data)
        new_data["content"] = new_content
        return ToolOutput(
            success=result.success,
            data=new_data,
            error=result.error,
        )

    async def stream(self, inputs: Dict[str, Any], **kwargs) -> Any:
        # 与 UnifiedTodoTool 同约定：flash 工具不走增量流式，整包一次性产出。
        yield await self.invoke(inputs, **kwargs)


__all__ = ["FlashReadFileTool"]
