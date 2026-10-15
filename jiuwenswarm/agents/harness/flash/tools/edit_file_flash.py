# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""EditFileFlashTool — EditFileTool 的 flash 定制版（工具名 edit_file_flash）。

工具命名对齐 web_flash / cron_flash 规范：flash 模式替换 stock 工具时
带 ``_flash`` 后缀（interface_flash 的 eager 清单同步移除 stock 名、
追加本名，与 web_search → web_flash 同款机制）。

在不修改 agent-core 的前提下，对 stock EditFileTool 的三处 I/O 浪费做
"语义不变、路径变短"的覆写（设计论证见仓库根
``flash-editfile-io-analysis-and-optimization.md``）：

1. **缓存命中免读盘**：read_file 注册快照时（FlashReadFileTool 覆写）已
   把全文存进 stock ``_FILE_READ_REGISTRY``、原始字节元数据存进侧车
   （sha256/EOL/编码，快照版本绑定 mtime+size）。本工具在校验通过且
   本地模式下直接用缓存内容匹配，消除每次编辑固定的全量读；写回仍传
   ``expected_content_sha256``，fs 层 CAS（仅 LOCAL 后端实现）保证竞态
   防护与 stock 等价。
2. **history 纯追加**：stock 每次编辑整读+整写全部历史（二次方恶化）；
   flash 写独立 JSONL（``file_ops_flash_*.jsonl``），条目 schema 与
   stock 一致，diff_service 合并读取后轮次 diff / /rewind 回滚 / 重做 /
   删除探测全链路可见。
3. **hunks 批量编辑**：一次调用提交同一文件的多处修改，统一对同一份
   内容快照匹配、拒绝重叠、全部命中才一次写回（all-or-nothing），
   省 N-1 次完整链路与工具往返。

边界：沙箱（jiuwenbox）后端不执行 CAS 且本地 os.stat 退化，缓存路径仅
LOCAL 模式启用；沙箱与 Office 文件走 stock 全量读路径，行为不变。
依赖 agent-core 私有 API（``_FILE_READ_REGISTRY`` 等）——与
FlashReadFileTool 同款升级漂移风险，agent-core 升级时必须重跑
"flash read → flash edit 同文件"回归。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.session import get_current_session
from openjiuwen.core.sys_operation import OperationMode
from openjiuwen.harness.tools.base_tool import ToolOutput
from openjiuwen.harness.tools.filesystem import (
    EditFileTool,
    _FILE_READ_REGISTRY,
    _FileReadState,
    _OFFICE_WRITE_EXTENSIONS,
    _is_office_doc_path,
    _is_unc_path,
    _office_write_unsupported_error,
    _read_office_plain_text_async,
    _resolve_tool_file_path,
    _write_office_document_async,
)

from .flash_file_history import append_flash_history_entry, build_flash_history_path
from .flash_file_meta import get_file_meta, pop_file_meta, record_written_meta

EDIT_FILE_FLASH_DESCRIPTION: Dict[str, str] = {
    "cn": (
        "文件精确编辑工具。用 old_string 在文件中定位要修改的片段并替换为 "
        "new_string，非 replace_all 时 old_string 必须在文件中恰好出现一次"
        "（多匹配时补充上下文使其唯一）。编辑前必须先 read_file 读过该文件。"
        "同一文件有多处修改时，推荐用 hunks 参数一次性提交全部修改："
        "所有修改统一匹配、互相不得重叠，全部成功才一次写回，"
        "任一失败则整个调用失败且文件保持不变。"
    ),
    "en": (
        "Precise file edit tool. Replaces old_string with new_string in a "
        "file; old_string must occur exactly once unless replace_all is set "
        "(add surrounding context to disambiguate). The file must be read "
        "with read_file before editing. For multiple changes in one file, "
        "prefer the hunks parameter: all edits are matched together, "
        "overlaps are rejected, and the file is written once only if every "
        "hunk matches (all-or-nothing)."
    ),
}


def get_edit_file_flash_input_params(language: str = "cn") -> Dict[str, Any]:
    if language == "en":
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path of the file to edit",
                },
                "old_string": {
                    "type": "string",
                    "description": "Text to replace (single-edit mode)",
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text (single-edit mode)",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence (default false)",
                },
                "hunks": {
                    "type": "array",
                    "description": (
                        "Batch mode: all edits for this file in one call. "
                        "Matched together against the same snapshot; overlaps "
                        "rejected; written in one pass only if every hunk "
                        "succeeds. Takes precedence over old_string/new_string."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_string": {"type": "string"},
                            "new_string": {"type": "string"},
                            "replace_all": {"type": "boolean"},
                        },
                        "required": ["old_string", "new_string"],
                    },
                },
            },
            "required": ["file_path"],
        }
    return {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "要编辑文件的绝对路径",
            },
            "old_string": {
                "type": "string",
                "description": "要替换的文本片段（单处修改模式）",
            },
            "new_string": {
                "type": "string",
                "description": "替换后的文本（单处修改模式）",
            },
            "replace_all": {
                "type": "boolean",
                "description": "替换所有匹配项（默认 false）",
            },
            "hunks": {
                "type": "array",
                "description": (
                    "批量修改模式：一次提交同一文件的全部修改，优先级高于 "
                    "old_string/new_string。所有 hunk 对同一份内容快照匹配、"
                    "互相不得重叠，全部成功才一次写回"
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                        "replace_all": {"type": "boolean"},
                    },
                    "required": ["old_string", "new_string"],
                },
            },
        },
        "required": ["file_path"],
    }


class EditFileFlashTool(EditFileTool):
    """EditFileTool with snapshot-cache matching, JSONL history and hunks."""

    def __init__(self, operation, language: str = "cn", agent_id: Optional[str] = None):
        super().__init__(operation, language, agent_id)
        # 工具名对齐 web_flash / cron_flash 规范（_flash 后缀）。stock 基类经
        # build_tool_card 以 "edit_file" 建 card，而其名称注册表只认 agent-core
        # 内置工具名（edit_file_flash 会 KeyError），故走事后改写——与
        # FlashReadFileTool 覆写 card.description 同款方式，不重建 card；
        # id 前缀同步替换、后缀（agent_id/uuid）保留。interface_flash 的
        # eager 清单同步移除 stock 名、追加 edit_file_flash。
        self.card.name = "edit_file_flash"
        if isinstance(self.card.id, str) and self.card.id.startswith("EditFileTool_"):
            self.card.id = "EditFileFlashTool_" + self.card.id[len("EditFileTool_"):]
        lang = language or "cn"
        self.card.description = EDIT_FILE_FLASH_DESCRIPTION.get(
            lang, EDIT_FILE_FLASH_DESCRIPTION["cn"]
        )
        self.card.input_params = get_edit_file_flash_input_params(lang)

    # ------------------------------------------------------------------
    # Input normalization
    # ------------------------------------------------------------------

    def _parse_hunks(self, inputs: Dict[str, Any]) -> Tuple[Optional[List[Tuple[str, str, bool]]], Optional[str]]:
        """归一化为 [(old, new, replace_all)] 列表；出错返回 (None, error)。"""
        hunks_raw = inputs.get("hunks")
        if hunks_raw:
            if not isinstance(hunks_raw, list):
                return None, "hunks must be an array of {old_string, new_string} objects."
            if not hunks_raw:
                return None, "hunks must not be empty."
            hunks: List[Tuple[str, str, bool]] = []
            for idx, hunk in enumerate(hunks_raw):
                if not isinstance(hunk, dict):
                    return None, f"hunks[{idx}] must be an object."
                hunk_old = hunk.get("old_string", "")
                hunk_new = hunk.get("new_string")
                if hunk_new is None:
                    return None, f"hunks[{idx}].new_string is required."
                if not isinstance(hunk_old, str) or not isinstance(hunk_new, str):
                    return None, f"hunks[{idx}] old_string/new_string must be strings."
                hunks.append((hunk_old, hunk_new, bool(hunk.get("replace_all", False))))
            return hunks, None
        new_str = inputs.get("new_string")
        if new_str is None:
            return None, "new_string is required."
        return [(inputs.get("old_string", ""), new_str, bool(inputs.get("replace_all", False)))], None

    # ------------------------------------------------------------------
    # New-file creation (mirrors stock lines 1856-1904, incl. no-history)
    # ------------------------------------------------------------------

    async def _create_new_file(
        self,
        file_path: str,
        new_str: str,
        *,
        file_exists: bool,
        is_unc: bool,
        is_office: bool,
    ) -> ToolOutput:
        if file_exists and not is_unc:
            try:
                if is_office:
                    existing_content = await _read_office_plain_text_async(file_path)
                else:
                    existing_content, _ = await self._read_existing_text(file_path)
            except Exception as exc:
                return ToolOutput(success=False, error=str(exc))
            if existing_content.strip() != "":
                return ToolOutput(
                    success=False,
                    error="Cannot create new file - file already exists.",
                )
        if is_office:
            try:
                await _write_office_document_async(file_path, new_str)
            except Exception as exc:
                return ToolOutput(success=False, error=f"Create failed: {exc}")
        else:
            write_res = await self.operation.fs().write_file(
                file_path, new_str, prepend_newline=False, create_if_not_exist=True
            )
            if write_res.code != StatusCode.SUCCESS.code:
                return ToolOutput(success=False, error=f"Create failed: {write_res.message}")
        try:
            _st = os.stat(file_path)
            registered = new_str.replace("\r\n", "\n")
            line_count = len(registered.splitlines()) if registered else 0
            _FILE_READ_REGISTRY[file_path] = _FileReadState(
                mtime_ns=_st.st_mtime_ns,
                size_bytes=_st.st_size,
                is_partial=False,
                content=registered,
                total_lines=line_count,
                read_ranges=[(1, line_count)] if line_count > 0 else [],
            )
            record_written_meta(
                file_path, new_str, mtime_ns=_st.st_mtime_ns, size_bytes=_st.st_size
            )
        except OSError:
            _FILE_READ_REGISTRY.pop(file_path, None)
            pop_file_meta(file_path)
        return ToolOutput(
            success=True,
            data={"file_path": file_path, "replacements": 0, "created": True},
        )

    # ------------------------------------------------------------------
    # invoke
    # ------------------------------------------------------------------

    async def _invoke_unlocked(self, inputs: Dict[str, Any], **kwargs) -> ToolOutput:  # noqa: C901
        hunks, parse_error = self._parse_hunks(inputs)
        if parse_error:
            return ToolOutput(success=False, error=parse_error)

        file_path = inputs.get("file_path")
        if not file_path:
            return ToolOutput(success=False, error="file_path is required")
        try:
            file_path = _resolve_tool_file_path(self.operation, file_path)
        except ValueError as exc:
            return ToolOutput(success=False, error=str(exc))

        if file_path.lower().endswith(".ipynb"):
            return ToolOutput(
                success=False,
                error="Cannot edit .ipynb files with this tool. Use NotebookEdit instead.",
            )

        is_office = _is_office_doc_path(file_path)
        if is_office:
            _, office_ext = os.path.splitext(file_path.lower())
            if office_ext not in _OFFICE_WRITE_EXTENSIONS:
                return ToolOutput(
                    success=False,
                    error=_office_write_unsupported_error(file_path, "edit_file_flash"),
                )

        for idx, (hunk_old, hunk_new, _) in enumerate(hunks):
            if hunk_old == hunk_new:
                where = f"hunks[{idx}]" if len(hunks) > 1 else "old_string and new_string"
                return ToolOutput(
                    success=False,
                    error=f"{where} are identical; no changes would be made.",
                )

        is_unc = _is_unc_path(file_path)
        if is_unc:
            file_exists = True
        elif self.operation.mode == OperationMode.SANDBOX:
            # 沙箱模式下 os.path.exists 只能看到主容器文件系统，实际文件在
            # jiuwenbox sidecar 中，用沙箱 fs 探测真实存在性（stock 同款）。
            try:
                _probe = await self.operation.fs().read_file(file_path, mode="bytes")
                file_exists = _probe.code == StatusCode.SUCCESS.code
            except Exception:
                file_exists = False
        else:
            file_exists = os.path.exists(file_path)

        # ---- 新建文件：仅单 hunk 且 old 为空时保持 stock 语义 --------------
        empty_old_count = sum(1 for hunk_old, _, _ in hunks if hunk_old == "")
        if empty_old_count:
            if len(hunks) == 1:
                return await self._create_new_file(
                    file_path,
                    hunks[0][1],
                    file_exists=file_exists,
                    is_unc=is_unc,
                    is_office=is_office,
                )
            return ToolOutput(
                success=False,
                error=(
                    "empty old_string (file creation) cannot be mixed with "
                    "other hunks in one call."
                ),
            )

        if not file_exists:
            similar = self._find_similar_paths(file_path)
            hint = f" Similar paths: {similar}" if similar else ""
            return ToolOutput(
                success=False,
                error=f"File not found: '{file_path}'.{hint}",
            )

        # ---- 体积护栏 + stat（stock 1916-1939） ----------------------------
        if not is_unc:
            _stat = None
            try:
                _stat = os.stat(file_path)
            except OSError as exc:
                if self.operation.mode != OperationMode.SANDBOX:
                    return ToolOutput(success=False, error=str(exc))
            if _stat is not None and _stat.st_size > self.MAX_FILE_SIZE:
                return ToolOutput(
                    success=False,
                    error=(
                        f"File is too large ({_stat.st_size // (1024 ** 3)} GiB). "
                        "Maximum allowed size is 1 GiB."
                    ),
                )
            current_mtime = _stat.st_mtime_ns if _stat else 0
            current_size = _stat.st_size if _stat else 0
        else:
            current_mtime = 0
            current_size = 0

        # ---- 准入门槛（stock 1941-1957：必须读过，部分读也算数） ------------
        read_state = _FILE_READ_REGISTRY.get(file_path)
        if read_state is None:
            return ToolOutput(
                success=False,
                error=(
                    f"File must be read before editing. "
                    f"Call read_file on '{file_path}' first."
                ),
            )

        # ---- 内容获取：缓存命中 or 全量读（含外部修改检测折叠） --------------
        meta = get_file_meta(file_path)
        cache_ok = (
            not is_office
            and not is_unc
            and self.operation.mode == OperationMode.LOCAL
            and read_state.content is not None
            and read_state.mtime_ns == current_mtime
            and read_state.size_bytes == current_size
            and meta is not None
            and meta.snapshot_mtime_ns == current_mtime
            and meta.snapshot_size_bytes == current_size
        )

        mtime_matches = (
            read_state.mtime_ns == current_mtime
            and read_state.size_bytes == current_size
        )

        if cache_ok:
            # 零读盘：registry 内容快照 + 侧车元数据同代（mtime/size 双绑）。
            content = read_state.content
            eol = meta.eol
            expected_sha = meta.sha256
        else:
            try:
                if is_office:
                    content = await _read_office_plain_text_async(file_path)
                    eol = "\n"
                    expected_sha = None
                else:
                    content, raw = await self._read_existing_text(file_path)
                    eol = self._detect_eol(raw)
                    expected_sha = hashlib.sha256(raw).hexdigest()
            except Exception as exc:
                return ToolOutput(success=False, error=str(exc))

            if not is_unc and not mtime_matches:
                # 外部修改检测（stock 1959-1981 的折叠实现：这一次读同时供
                # 比对与编辑，比 stock 的"比对读一次 + 主读再一次"少一读）。
                content_lf_check = content.replace("\r\n", "\n")
                if read_state.content is None or content_lf_check != read_state.content:
                    _FILE_READ_REGISTRY.pop(file_path, None)
                    pop_file_meta(file_path)
                    return ToolOutput(
                        success=False,
                        error=(
                            f"'{file_path}' has been modified externally since it was last read. "
                            "Re-read the file before editing."
                        ),
                    )

        content_lf = content.replace("\r\n", "\n")

        # ---- 逐 hunk 匹配（stock 1998-2028 的清洗/容错/唯一性逐个套用） -----
        _, ext = os.path.splitext(file_path.lower())
        preserve_md = ext in self._MD_EXTENSIONS

        planned: List[Tuple[int, int, str]] = []  # (start, end, new_text)
        for idx, (hunk_old, hunk_new, hunk_replace_all) in enumerate(hunks):
            where = f"hunks[{idx}]" if len(hunks) > 1 else "old_string"
            old_clean = self._desanitize(hunk_old).replace("\r\n", "\n")
            new_clean = self._desanitize(hunk_new).replace("\r\n", "\n")
            if not preserve_md:
                new_clean = self._strip_trailing_whitespace(new_clean)

            match_str = old_clean
            if match_str not in content_lf:
                variant = self._try_quote_variants(content_lf, old_clean)
                if variant is None:
                    return ToolOutput(
                        success=False,
                        error=f"{where} not found in '{file_path}'.",
                    )
                match_str = variant
                new_clean = self._preserve_quote_style(old_clean, match_str, new_clean)

            count = content_lf.count(match_str)
            if not hunk_replace_all and count > 1:
                return ToolOutput(
                    success=False,
                    error=(
                        f"{where} matches {count} times in '{file_path}'. "
                        "Provide more surrounding context to make it unique, "
                        "or set replace_all=true to replace every occurrence."
                    ),
                )

            positions: List[int] = []
            if hunk_replace_all:
                cursor = 0
                while True:
                    pos = content_lf.find(match_str, cursor)
                    if pos == -1:
                        break
                    positions.append(pos)
                    cursor = pos + len(match_str)
            else:
                positions.append(content_lf.find(match_str))
            for pos in positions:
                planned.append((pos, pos + len(match_str), new_clean))

        # ---- 重叠拒绝 + 应用（从后往前，避免偏移失效） ----------------------
        planned.sort(key=lambda item: item[0])
        for prev, curr in zip(planned, planned[1:]):
            if curr[0] < prev[1]:
                return ToolOutput(
                    success=False,
                    error=(
                        "Overlapping edits are not allowed: two hunks match "
                        "overlapping regions of the file. Merge them into one hunk."
                    ),
                )

        new_content_lf = content_lf
        for start, end, text in reversed(planned):
            new_content_lf = new_content_lf[:start] + text + new_content_lf[end:]

        # 还原原 EOL；Office 写入器用 LF。
        new_content = (
            new_content_lf.replace("\n", eol) if eol == "\r\n" else new_content_lf
        )

        # ---- 写回（stock 2041-2055：CAS 乐观锁仅本地后端执行） -------------
        if is_office:
            try:
                await _write_office_document_async(file_path, new_content_lf)
            except Exception as exc:
                return ToolOutput(success=False, error=f"Write failed: {exc}")
        else:
            options = (
                {"expected_content_sha256": expected_sha}
                if expected_sha
                else None
            )
            write_res = await self.operation.fs().write_file(
                file_path,
                new_content,
                prepend_newline=False,
                options=options,
            )
            if write_res.code != StatusCode.SUCCESS.code:
                return ToolOutput(success=False, error=f"Write failed: {write_res.message}")

        # ---- registry 回填（stock 语义）+ 侧车更新 --------------------------
        try:
            _st2 = os.stat(file_path)
            line_count = len(new_content_lf.splitlines()) if new_content_lf else 0
            _FILE_READ_REGISTRY[file_path] = _FileReadState(
                mtime_ns=_st2.st_mtime_ns,
                size_bytes=_st2.st_size,
                is_partial=False,
                content=new_content_lf,
                total_lines=line_count,
                read_ranges=[(1, line_count)] if line_count > 0 else [],
            )
            if not is_office:
                record_written_meta(
                    file_path,
                    new_content,
                    mtime_ns=_st2.st_mtime_ns,
                    size_bytes=_st2.st_size,
                )
        except OSError:
            _FILE_READ_REGISTRY.pop(file_path, None)
            pop_file_meta(file_path)

        # ---- history 纯追加（schema 与 stock 一致，diff_service 合并读取） --
        # old/new 均按文件原 EOL 还原后入账：缓存路径的内容快照是 LF 归一化
        # 的，fresh 路径是原样解码的——统一还原保证与 stock 存储口径一致
        # （diff_service 的条目去重按 old/new 逐字比较）。
        _session = get_current_session()
        if _session is not None:
            _history_path = build_flash_history_path(_session)
            if _history_path:
                old_for_history = (
                    content_lf.replace("\n", eol) if eol == "\r\n" else content_lf
                )
                entry = {
                    "action": "edit",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "old_content": old_for_history,
                    "new_content": new_content,
                }
                try:
                    await asyncio.to_thread(
                        append_flash_history_entry, _history_path, file_path, entry
                    )
                except Exception as exc:  # noqa: BLE001
                    # history 是旁路账本：写失败不阻断编辑主流程（stock 同款
                    # 容忍度，只 warning）。
                    from openjiuwen.core.common.logging import logger

                    logger.warning(
                        "[EditFileFlashTool] Failed to append flash history: %s", exc
                    )

        return ToolOutput(
            success=True,
            data={
                "file_path": file_path,
                "replacements": len(planned),
                "hunks": len(hunks),
            },
        )

    async def stream(self, inputs: Dict[str, Any], **kwargs) -> Any:
        # 与 FlashReadFileTool 同约定：flash 工具不走增量流式，整包产出。
        yield await self.invoke(inputs, **kwargs)


__all__ = ["EditFileFlashTool"]
