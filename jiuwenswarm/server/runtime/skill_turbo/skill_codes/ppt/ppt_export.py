from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError, PlanNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_common import PptCommon
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.utils.bash_utils import (
    BashExecError,
    cli_path,
    quote_path,
    run_bash,
)

logger = logging.getLogger(__name__)

# 与 CLI convert native composer 同一判据（pptx-template-canvas-plan-v2）。
_NATIVE_PLAN_SCHEMA_VERSION = "pptx-template-canvas-plan-v2"


_ILLEGAL_FILENAME_RE = re.compile(r'[<>:"/\\|?*]')
_WINDOWS_RESERVED = frozenset(
    {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


def _sanitize_filename(topic: str, *, max_length: int = 50) -> str:
    name = topic.strip()
    name = _ILLEGAL_FILENAME_RE.sub("_", name)
    name = re.sub(r"\s+", "_", name)
    name = name.strip(". ")
    if name.upper() in _WINDOWS_RESERVED:
        name = f"ppt_{name}"
    if len(name) > max_length:
        name = name[:max_length].rstrip(". ")
    if not name:
        name = "presentation"
    return name


@dataclass
class ExportPaths:
    """导出路径相关参数封装。"""

    output_dir: str
    pages_dir: str
    pptx_path: str
    pptx_filename: str
    pptx_root: str


_CONVERT_MAX_ATTEMPTS = 2


class PPTExportNode(PlanNode):
    """P9 — PPTX 导出（Phase 4.3）。"""

    def __init__(self) -> None:
        super().__init__(
            plan_name="p9_ppt_export",
            instruction=(
                "## P9 PPTX 导出\n"
                "\n"
                "### 节点职责\n"
                "1. sanitize topic → 生成 PPTX 文件名\n"
                "2. 普通分支：cli.js convert → validate-pptx-artifact\n"
                "3. `template_canvas`：normalize-template-chrome → check-template-canvas → "
                "check-template-render → convert（条件 --native-plan）→ validate-pptx-artifact\n"
                "4. 验证 PPTX 产物（文件存在 + 大小 > 10KB）\n"
            ),
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        output_dir = str(inputs.get("output_dir") or "").strip()
        pages_dir = str(inputs.get("pages_dir") or "").strip()
        topic = str(inputs.get("topic") or "").strip()

        if not output_dir or not pages_dir:
            logger.error(
                "[P9] 必填字段缺失 output_dir=%s pages_dir=%s",
                bool(output_dir), bool(pages_dir),
            )
            return {
                "pptx_path": "",
                "pptx_filename": "",
                "export_status": "failed",
                "validate_pptx_status": "skipped",
            }

        sanitized = _sanitize_filename(topic or "presentation")
        pptx_filename = f"{sanitized}.pptx"
        pptx_path = f"{output_dir}/{pptx_filename}"
        pptx_root = str(inputs.get("pptx_root") or "").strip()

        missing_pages = inputs.get("missing_pages") or []
        if isinstance(missing_pages, list) and missing_pages:
            logger.error("[P9] 上游页面缺失 missing=%s，拒绝导出", missing_pages)
            return {
                "pptx_path": "",
                "pptx_filename": pptx_filename,
                "export_status": "failed",
                "validate_pptx_status": "skipped",
            }
        layout_warning_pages = inputs.get("layout_warning_pages") or []
        if isinstance(layout_warning_pages, list) and layout_warning_pages:
            logger.warning(
                "[P9] 上游 layout 警告页 layout_warning=%s，仍继续导出",
                layout_warning_pages,
            )
        ppt_gen_status = str(inputs.get("ppt_gen_status") or "").strip()
        if ppt_gen_status == "failed":
            logger.error("[P9] 上游 ppt_gen_status=failed，拒绝导出")
            return {
                "pptx_path": "",
                "pptx_filename": pptx_filename,
                "export_status": "failed",
                "validate_pptx_status": "skipped",
            }

        style_mode = str(inputs.get("style_mode") or "").strip()
        if style_mode == "template_canvas":
            paths = ExportPaths(
                output_dir=output_dir,
                pages_dir=pages_dir,
                pptx_path=pptx_path,
                pptx_filename=pptx_filename,
                pptx_root=pptx_root,
            )
            return await self._execute_template_export(inputs, paths)

        export_status = await self._run_convert(pages_dir, pptx_path, pptx_root)
        validate_status = "skipped"
        if export_status != "failed":
            validate_ok = await self._run_validate_pptx_artifact(
                pages_dir, pptx_path, pptx_root,
            )
            validate_status = "passed" if validate_ok else "failed"
            if not validate_ok:
                export_status = "failed"
        return {
            "pptx_path": pptx_path if Path(pptx_path).is_file() else "",
            "pptx_filename": pptx_filename,
            "export_status": export_status,
            "validate_pptx_status": validate_status,
        }

    async def _execute_template_export(
        self,
        inputs: dict[str, Any],
        paths: ExportPaths,
    ) -> dict[str, Any]:
        """模板包：normalize-chrome → canvas/render → convert → validate。"""
        output_dir = paths.output_dir
        pages_dir = paths.pages_dir
        pptx_path = paths.pptx_path
        pptx_filename = paths.pptx_filename
        pptx_root = paths.pptx_root
        pack_dir = str(inputs.get("pack_dir") or "").strip()
        if not pack_dir:
            logger.error("[P9-T] pack_dir 为空，无法执行模板导出")
            return {
                "pptx_path": "",
                "pptx_filename": pptx_filename,
                "export_status": "failed",
                "validate_pptx_status": "skipped",
            }

        plan_path = str(
            inputs.get("template_canvas_plan_path")
            or f"{output_dir}/template-canvas-plan.json"
        ).strip()

        for step_name, builder in (
            (
                "normalize-template-chrome",
                lambda: (
                    f"{cli_path('normalize-template-chrome', pptx_root)} "
                    f"--plan {quote_path(plan_path)} "
                    f"--pages-dir {quote_path(pages_dir)}"
                ),
            ),
            (
                "check-template-canvas",
                lambda: (
                    f"{cli_path('check-template-canvas', pptx_root)} "
                    f"--plan {quote_path(plan_path)} "
                    f"--pages-dir {quote_path(pages_dir)}"
                ),
            ),
            (
                "check-template-render",
                lambda: (
                    f"{cli_path('check-template-render', pptx_root)} "
                    f"--plan {quote_path(plan_path)} "
                    f"--pages-dir {quote_path(pages_dir)}"
                ),
            ),
        ):
            try:
                result = await run_bash(
                    self,
                    builder(),
                    timeout_seconds=300,
                    required=False,
                    workdir=pptx_root,
                )
                if result.exit_code != 0:
                    logger.error(
                        "[P9-T] %s 失败 exit=%d: %s",
                        step_name,
                        result.exit_code,
                        ((result.stderr or result.stdout) or "")[:500],
                    )
                    return {
                        "pptx_path": "",
                        "pptx_filename": pptx_filename,
                        "export_status": "failed",
                        "validate_pptx_status": "skipped",
                    }
                logger.info("[P9-T] %s 通过", step_name)
            except BashExecError as e:
                logger.error("[P9-T] %s 异常: %s", step_name, e)
                return {
                    "pptx_path": "",
                    "pptx_filename": pptx_filename,
                    "export_status": "failed",
                    "validate_pptx_status": "skipped",
                }

        native_plan = await self._resolve_native_plan_arg(plan_path, output_dir)
        export_status = await self._run_convert(
            pages_dir, pptx_path, pptx_root, native_plan=native_plan,
        )
        if export_status == "failed":
            return {
                "pptx_path": "",
                "pptx_filename": pptx_filename,
                "export_status": "failed",
                "validate_pptx_status": "skipped",
            }

        validate_ok = await self._run_validate_pptx_artifact(
            pages_dir, pptx_path, pptx_root,
        )
        validate_status = "passed" if validate_ok else "failed"
        if not validate_ok:
            export_status = "failed"

        logger.info(
            "[P9-T] 模板导出完成: %s status=%s validate=%s",
            pptx_path, export_status, validate_status,
        )
        return {
            "pptx_path": pptx_path if Path(pptx_path).is_file() else "",
            "pptx_filename": pptx_filename,
            "export_status": export_status,
            "validate_pptx_status": validate_status,
        }

    async def _read_file(self, path: str) -> str:
        if not path:
            return ""
        if not self.has_tool("read_file"):
            logger.warning("[P9] read_file 工具不可用，无法读取文件 %s", path)
            return ""
        try:
            result = await self.call_tool("read_file", file_path=path)
            return PptCommon.parse_tool_file_content(result)
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P9] 读取文件失败 %s: %s", path, e)
            return ""

    async def _resolve_native_plan_arg(self, plan_path: str, output_dir: str) -> str:
        """v2 且 nativeTemplate.available=true 时返回 plan 路径，否则空串。

        判据对齐 CLI convert：schema_version == pptx-template-canvas-plan-v2
        且 nativeTemplate.available is True。禁止文本 token 猜测。
        """
        candidates = [
            Path(plan_path),
            Path(output_dir) / "template-canvas-plan.json",
        ]
        seen: set[str] = set()
        for path in candidates:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            if not path.is_file():
                continue
            text = await self._read_file(str(path))
            if not text:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                logger.warning("[P9-T] native-plan 候选非 JSON，跳过 path=%s", path)
                continue
            if not isinstance(data, dict):
                continue
            native = data.get("nativeTemplate")
            if (
                data.get("schema_version") == _NATIVE_PLAN_SCHEMA_VERSION
                and isinstance(native, dict)
                and native.get("available") is True
            ):
                return str(path)
        return ""

    async def _run_validate_pptx_artifact(
        self,
        pages_dir: str,
        pptx_path: str,
        pptx_root: str,
    ) -> bool:
        try:
            cmd = (
                f"{cli_path('validate-pptx-artifact', pptx_root)} "
                f"--pages-dir {quote_path(pages_dir)} "
                f"--pptx {quote_path(pptx_path)}"
            )
            result = await run_bash(
                self, cmd,
                timeout_seconds=120, required=False, workdir=pptx_root,
            )
            if result.exit_code != 0:
                logger.error(
                    "[P9] validate-pptx-artifact 失败 exit=%d: %s",
                    result.exit_code,
                    ((result.stderr or result.stdout) or "")[:500],
                )
                return False
            logger.info("[P9] validate-pptx-artifact 通过")
            return True
        except BashExecError as e:
            logger.error("[P9] validate-pptx-artifact 异常: %s", e)
            return False
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.error("[P9] validate-pptx-artifact 异常: %s", e)
            return False

    async def _attempt_convert(
        self,
        pages_dir: str,
        pptx_path: str,
        pptx_root: str,
        *,
        native_plan: str = "",
    ) -> tuple[str, str | None]:
        convert_cmd = (
            f"{cli_path('convert', pptx_root)} "
            f"{quote_path(pages_dir + '/')} {quote_path(pptx_path)}"
        )
        if native_plan:
            convert_cmd += f" --native-plan {quote_path(native_plan)}"
        try:
            await run_bash(
                self, convert_cmd,
                timeout_seconds=600, required=True, workdir=pptx_root,
            )
            export_status = await self._validate_pptx(pptx_path, pptx_root)
            if export_status == "failed":
                return "failed", f"PPTX 未生成或无效: {pptx_path}"
            return export_status, None
        except BashExecError as e:
            return "failed", str(e)
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            return "failed", str(e)

    async def _run_convert(
        self,
        pages_dir: str,
        pptx_path: str,
        pptx_root: str,
        *,
        native_plan: str = "",
    ) -> str:
        last_error: str | None = None
        for attempt in range(1, _CONVERT_MAX_ATTEMPTS + 1):
            export_status, error_detail = await self._attempt_convert(
                pages_dir, pptx_path, pptx_root, native_plan=native_plan,
            )
            if export_status != "failed":
                if attempt > 1:
                    logger.info(
                        "[P9] cli.js convert 第 %d 次重试成功 status=%s",
                        attempt,
                        export_status,
                    )
                else:
                    logger.info("[P9] cli.js convert 完成 status=%s", export_status)
                return export_status

            last_error = error_detail
            if attempt < _CONVERT_MAX_ATTEMPTS:
                logger.warning(
                    "[P9] cli.js convert 第 %d 次失败，准备重试: %s",
                    attempt,
                    (error_detail or "")[:500],
                )
            else:
                logger.error(
                    "[P9] cli.js convert 重试后仍失败（共 %d 次）"
                    " pages_dir=%s pptx_path=%s error=%s",
                    _CONVERT_MAX_ATTEMPTS,
                    pages_dir,
                    pptx_path,
                    (last_error or "")[:2000],
                )
        return "failed"

    async def _validate_pptx(self, pptx_path: str, pptx_root: str) -> str:
        try:
            escaped_path = pptx_path.replace('\\', '\\\\').replace("'", "\\'").replace('"', '\\"')
            stat_cmd = (
                f"node -e \"const fs=require('fs');const s=fs.statSync('{escaped_path}');"
                f'process.stdout.write(String(s.size))"'
            )
            result = await run_bash(
                self, stat_cmd,
                timeout_seconds=30, required=False, workdir=pptx_root,
            )
            if result.exit_code != 0:
                detail = (result.stderr or result.stdout or "").strip()
                logger.error("[P9] PPTX 不存在或 stat 失败，导出判定 failed: %s", detail[:500])
                return "failed"
            size_text = (result.stdout or "").strip()
            try:
                size = int(size_text or "0")
            except ValueError:
                logger.error("[P9] PPTX 大小解析失败 stdout=%r", size_text[:200])
                return "failed"
            if size < 10 * 1024:
                logger.warning("[P9] PPTX 文件过小 size=%d < 10KB", size)
                return "partial"
            logger.info("[P9] PPTX 验证通过 size=%d", size)
            return "ok"
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.error("[P9] PPTX 验证失败: %s", e)
            return "failed"

    async def _execute_stream(self, inputs: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        result = await self._execute(inputs)
        export_status = str(result.get("export_status") or "failed")
        status_map = {"ok": "ok", "partial": "warning", "failed": "error"}
        if export_status == "failed":
            message = (
                "PPTX 导出失败：未生成有效文件或 validate-pptx-artifact 未通过"
            )
        elif export_status == "partial":
            message = (
                f"PPTX 导出部分成功（文件偏小）file={result.get('pptx_filename')}"
            )
        else:
            message = f"PPTX 导出成功 file={result.get('pptx_filename')}"
        yield {
            **result,
            "node": self.plan_name,
            "status": status_map.get(export_status, "error"),
            "message": message,
        }
