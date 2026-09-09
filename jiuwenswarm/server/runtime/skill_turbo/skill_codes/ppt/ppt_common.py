from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError

logger = logging.getLogger(__name__)

_JSON_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_CAT_N_PREFIX_RE = re.compile(r"^[ \t]*\d+[ \t]", re.MULTILINE)
_OUTLINE_PAGE_HEADING_RE = re.compile(r"^### P(\d+):", re.MULTILINE)
_EMPTY_FILE_WARNING_PREFIX = "Warning: the file exists but the contents are empty."

# ──────────────────────── 节点显示名映射 ────────────────────────
# 将内部 plan_name（如 p0_pipeline_init）映射为界面上展示的中文名称。
# 排序遵循 ppt_gen_root 节点 sub_plans 的执行顺序（Stage 1 → Stage N）。
# 仅影响前端展示，不改变内部 plan_name 标识。
NODE_DISPLAY_NAMES: dict[str, str] = {
    "p0_pipeline_init": "Stage 1: 流水线初始化",
    "p1_intent_classify": "Stage 2: 意图分类",
    "p3_document_parse": "Stage 3: 文档解析",
    "p2_requirement_collect": "Stage 4: 需求收集",
    "p3_5_template_context": "Stage 5: 模板上下文预处理",
    "p4_content_plan": "Stage 6: 内容策划",
    "p5_outline_review": "Stage 7: 大纲审阅",
    "p6_deep_research": "Stage 8: 深度研究",
    "p7_style_prepare": "Stage 9: 风格准备",
    "p6_5_image_prepare": "Stage 10: 图片准备",
    "p8_ppt_page_gen": "Stage 11: 幻灯片生成",
    "p9_ppt_export": "Stage 12: PPTX导出",
    "p11_speaker_notes": "Stage 13: 演讲备注",
    "p10_delivery": "Stage 14: 交付",
    "ppt_gen_root": "PPT生成",
}


def _strip_line_numbers(text: str) -> str:
    return _CAT_N_PREFIX_RE.sub("", text)


def _strip_tool_text(value: Any) -> str:
    if isinstance(value, str):
        return _strip_line_numbers(value.strip())
    return _strip_line_numbers(str(value or "").strip())


def _payload_text(result: Any) -> str:
    if isinstance(result, str):
        return _strip_tool_text(result)
    data = getattr(result, "data", result)
    if isinstance(data, dict):
        return _strip_tool_text(data.get("content", ""))
    if isinstance(data, str):
        return _strip_tool_text(data)
    return _strip_tool_text(result)


class PptCommon:
    """PPT skill_codes 公共工具：流水线 inputs 解析与 LLM JSON 提取。"""

    TEXT_SOURCE_KEYS = ("task", "user_request", "user_message", "query")
    QUERY_PREFIXES = (
        "你收到一条消息：\n",
        "You receive a new message:\n",
    )
    JSON_FENCE_PATTERN = _JSON_FENCE_PATTERN

    @classmethod
    def extract_plain_user_text(cls, raw: str) -> str:
        """从 build_user_prompt 包装或裸文本中提取用户原文 content。"""
        text = raw.strip()
        if not text:
            return ""

        for prefix in cls.QUERY_PREFIXES:
            if not text.startswith(prefix):
                continue
            json_part = text[len(prefix):]
            try:
                payload = json.loads(json_part)
            except json.JSONDecodeError:
                break
            if isinstance(payload, dict):
                content = payload.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
            break

        brace_index = text.find("{")
        if brace_index >= 0:
            try:
                payload = json.loads(text[brace_index:])
            except json.JSONDecodeError:
                return text
            if isinstance(payload, dict):
                content = payload.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()

        return text

    @classmethod
    def collect_user_text(cls, inputs: dict[str, Any]) -> str:
        """合并 task / user_request / user_message / query 中的用户可见原文。"""
        parts: list[str] = []
        seen: set[str] = set()
        for key in cls.TEXT_SOURCE_KEYS:
            value = inputs.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            normalized = cls.extract_plain_user_text(value)
            if not normalized:
                continue
            dedupe_key = normalized.casefold()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            parts.append(normalized)
        return "\n".join(parts)

    @classmethod
    def parse_json_payload(cls, raw: str) -> Any:
        """解析 LLM 返回的 JSON（支持 markdown fence 与正文中的 JSON 对象）。"""
        if not raw or not raw.strip():
            return None

        text = raw.strip()
        fence_match = cls.JSON_FENCE_PATTERN.search(text)
        if fence_match:
            text = fence_match.group(1).strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            object_match = re.search(r"\{[\s\S]*\}", text)
            if not object_match:
                return None
            try:
                return json.loads(object_match.group(0))
            except json.JSONDecodeError:
                return None

    @staticmethod
    def is_tool_failure_envelope(result: Any) -> bool:
        """read_file / write_file 工具级失败：success=False，不抛异常。"""
        if result is None:
            return False
        if getattr(result, "success", None) is False:
            return True
        if isinstance(result, dict) and result.get("success") is False:
            return True
        if not isinstance(result, str):
            return False
        stripped = result.strip()
        if stripped.startswith("success=False"):
            return True
        return stripped.startswith("success= False")

    @classmethod
    def tool_result_error(cls, result: Any) -> str | None:
        if not cls.is_tool_failure_envelope(result):
            return None
        err = getattr(result, "error", None)
        if err:
            return str(err)
        if isinstance(result, dict):
            mapped = result.get("error")
            if mapped:
                return str(mapped)
            return str(result)
        return str(result).strip() or "success=False"

    @staticmethod
    def _is_empty_file_warning(text: str) -> bool:
        return text.strip().startswith(_EMPTY_FILE_WARNING_PREFIX)

    @classmethod
    def parse_tool_file_content(cls, result: Any) -> str:
        """从 read_file / write_file 工具返回值中提取文本内容，并去掉 cat -n 行号前缀。

        失败信封与「文件存在但内容为空」的 warning 一律视为无正文，避免当大纲用。
        """
        if result is None or cls.is_tool_failure_envelope(result):
            return ""
        text = _payload_text(result)
        if cls._is_empty_file_warning(text):
            return ""
        return text

    @staticmethod
    def _log_read_outcome(path: Path, tool_error: str | None, text: str) -> None:
        if tool_error:
            logger.warning(
                "[PptCommon] read_file failed path=%s error=%s",
                path,
                tool_error,
            )
            return
        if not text.strip():
            logger.warning("[PptCommon] read_file returned empty path=%s", path)

    @classmethod
    async def _call_read_file_tool(
        cls, node: Any, path: Path
    ) -> tuple[str, str | None]:
        result = await node.call_tool("read_file", file_path=str(path))
        tool_error = cls.tool_result_error(result)
        text = cls.parse_tool_file_content(result)
        cls._log_read_outcome(path, tool_error, text)
        return text, tool_error

    @classmethod
    def _bash_cat_succeeded(cls, bash_res: Any) -> bool:
        if bash_res is None or bash_res.exit_code != 0:
            return False
        stdout = str(bash_res.stdout or "").strip()
        if not stdout:
            return False
        return not cls._is_empty_file_warning(str(bash_res.stdout))

    @classmethod
    async def _read_via_bash_cat(
        cls, node: Any, path: Path, tool_error: str | None
    ) -> str:
        from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.utils.bash_utils import (
            quote_path,
            run_bash,
        )

        try:
            bash_res = await run_bash(
                node,
                f"cat {quote_path(str(path))}",
                required=False,
                timeout_seconds=30,
            )
        except Exception as exc:
            if isinstance(exc, AbortError):
                raise
            return ""
        if not cls._bash_cat_succeeded(bash_res):
            return ""
        stdout = bash_res.stdout
        logger.warning(
            "[PptCommon] read_file tool returned empty, "
            "bash cat fallback path=%s bytes=%d reason=%s",
            path,
            len(stdout.encode("utf-8", errors="ignore")),
            tool_error or "empty",
        )
        return _strip_line_numbers(stdout.strip())

    @classmethod
    async def read_file(
        cls,
        node: Any,
        file_path: str | Path | None,
        *,
        max_chars: int | None = None,
        required: bool = False,
        label: str = "file",
        error_type: type[Exception] = RuntimeError,
    ) -> str:
        if not file_path:
            if required:
                raise error_type(f"缺少 {label} 路径")
            return ""
        path = Path(str(file_path)).expanduser().resolve()

        text = ""
        tool_error: str | None = None
        if node.has_tool("read_file"):
            try:
                text, tool_error = await cls._call_read_file_tool(node, path)
            except Exception as exc:
                if isinstance(exc, AbortError):
                    raise
                tool_error = str(exc)
                logger.warning(
                    "[PptCommon] read_file raised path=%s error=%s",
                    path,
                    tool_error,
                )
                if required and not node.has_tool("bash"):
                    raise error_type(f"读取 {label} 失败: {path}: {exc}") from exc
        elif required:
            raise error_type(f"read_file 工具不可用，无法读取 {label}")

        # skill_code 禁止 Path.read_text/open：工具读空/失败时用 bash cat 回退。
        if not text.strip() and node.has_tool("bash"):
            text = await cls._read_via_bash_cat(node, path, tool_error)
        if text:
            if max_chars is not None and len(text) > max_chars:
                return text[:max_chars] + "\n\n...(内容已截断)"
            return text
        if not required:
            return ""
        if tool_error:
            raise error_type(f"读取 {label} 失败: {path}: {tool_error}")
        raise error_type(f"{label} 为空或不存在: {path}")

    @classmethod
    async def write_file(
        cls,
        node: Any,
        file_path: str | Path,
        content: str,
        *,
        label: str = "file",
        error_type: type[Exception] = RuntimeError,
    ) -> Path:
        path = Path(str(file_path)).expanduser().resolve()
        normalized = content.strip() + "\n" if content.strip() else ""
        if not node.has_tool("write_file"):
            raise error_type(f"write_file 工具不可用，无法写入 {label}")
        try:
            result = await node.call_tool(
                "write_file",
                file_path=str(path),
                content=normalized,
            )
        except Exception as exc:
            if isinstance(exc, AbortError):
                raise
            raise error_type(f"写入 {label} 失败: {path}: {exc}") from exc
        write_error = cls.tool_result_error(result)
        if write_error:
            raise error_type(f"写入 {label} 失败: {path}: {write_error}")
        return path

    @staticmethod
    def resolve_total_pages(
        *,
        page_count: int = 0,
        total_pages: int | None = None,
        outline_text: str = "",
        outline_pages: dict[int, str] | None = None,
        default_structural_pages: int = 2,
    ) -> int:
        """从 outline 页码、上下文 total_pages 与 page_count 兜底推算总页数。

        含 agenda 等额外结构页时，``page_count + 2`` 会低估总页数；优先取 outline 最大页码。
        """
        candidates: list[int] = []
        if total_pages is not None:
            try:
                parsed = int(total_pages)
                if parsed > 0:
                    candidates.append(parsed)
            except (TypeError, ValueError):
                pass
        if outline_pages:
            candidates.append(max(outline_pages))
        if outline_text.strip():
            page_nums = [
                int(match.group(1))
                for match in _OUTLINE_PAGE_HEADING_RE.finditer(outline_text)
            ]
            if page_nums:
                candidates.append(max(page_nums))
        if page_count > 0:
            candidates.append(page_count + default_structural_pages)
        return max(candidates) if candidates else 0
