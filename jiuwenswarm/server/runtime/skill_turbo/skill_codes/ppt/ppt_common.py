from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError

logger = logging.getLogger(__name__)

# write_file 宿主协议：覆盖写前须完整 read；改盘后未再读会触发这些错误。
_WRITE_PROTOCOL_MARKERS = (
    "modified since read",
    "not been fully read",
    "File has not been fully read",
    "has been modified since read",
)

_PATH_LOCKS: dict[str, asyncio.Lock] = {}
_PATH_LOCKS_META: asyncio.Lock | None = None


def _path_locks_meta() -> asyncio.Lock:
    global _PATH_LOCKS_META
    if _PATH_LOCKS_META is None:
        _PATH_LOCKS_META = asyncio.Lock()
    return _PATH_LOCKS_META


def _normalize_page_path_key(path: str) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    try:
        return str(Path(raw).expanduser().resolve())
    except OSError:
        return raw


def is_write_protocol_error(exc: BaseException) -> bool:
    text = str(exc or "")
    return any(marker in text for marker in _WRITE_PROTOCOL_MARKERS)


async def get_page_path_lock(path: str) -> asyncio.Lock:
    """Process-wide per-path lock so P8.1 / P8.2 share the same serialization."""
    key = _normalize_page_path_key(path) or str(path or "")
    async with _path_locks_meta():
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _PATH_LOCKS[key] = lock
        return lock


@asynccontextmanager
async def page_path_lock_cm(path: str) -> AsyncIterator[None]:
    lock = await get_page_path_lock(path)
    async with lock:
        yield


async def safe_overwrite_file_impl(
    node: Any,
    path: str,
    content: str,
    *,
    already_locked: bool = False,
    log_prefix: str = "[ppt]",
) -> bool:
    """Read-then-write under path lock; retry once on host write-protocol errors."""
    if not path:
        return False
    if not node.has_tool("write_file"):
        logger.error("%s write_file 工具不可用 %s", log_prefix, path)
        return False

    async def _body() -> bool:
        last_exc: BaseException | None = None
        for attempt in range(2):
            try:
                if node.has_tool("read_file"):
                    try:
                        await node.call_tool("read_file", file_path=path)
                    except Exception as read_exc:
                        if isinstance(read_exc, AbortError):
                            raise
                        # 文件尚不存在时允许继续覆盖写
                        logger.debug(
                            "%s 覆盖写前 read 跳过 path=%s: %s",
                            log_prefix,
                            path,
                            read_exc,
                        )
                await node.call_tool("write_file", file_path=path, content=content)
                # 写盘成功后主动让出事件循环，避免 P8 多页集中收尾时饿死 WS ping/pong。
                await asyncio.sleep(0)
                return True
            except Exception as exc:
                if isinstance(exc, AbortError):
                    raise
                last_exc = exc
                if attempt == 0 and is_write_protocol_error(exc):
                    logger.warning(
                        "%s write_file 协议冲突，同锁重试 path=%s: %s",
                        log_prefix,
                        path,
                        exc,
                    )
                    continue
                logger.error("%s 写入文件失败 %s: %s", log_prefix, path, exc)
                return False
        if last_exc is not None:
            logger.error("%s 写入文件失败 %s: %s", log_prefix, path, last_exc)
        return False

    if already_locked:
        return await _body()
    async with page_path_lock_cm(path):
        return await _body()

_JSON_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_CAT_N_PREFIX_RE = re.compile(r"^[ \t]*\d+[ \t]", re.MULTILINE)
_OUTLINE_PAGE_HEADING_RE = re.compile(r"^### P(\d+):", re.MULTILINE)

# ──────────────────────── 节点显示名映射 ────────────────────────
# 将内部 plan_name（如 p0_pipeline_init）映射为界面展示名。
# UI Stage 1–14 = ppt_gen_root.sub_plans 顺序；≠ pptx-craft 契约「阶段 1–4」。
# 仅影响前端展示，不改变内部 plan_name / 执行顺序。
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
    "p8_0_5_template_seed": "Stage 11: 模板预铺",
    "p8_0_6_designer_tasks": "Stage 11: Designer 任务",
    "p9_ppt_export": "Stage 12: PPTX导出",
    "p11_speaker_notes": "Stage 13: 演讲备注",
    "p10_delivery": "Stage 14: 交付",
    "ppt_gen_root": "PPT生成",
}

# ──────────────────────── Dispatch 映射（Wave4 / accel 例外）────────────────────────
# 新 skill contracts/dispatch.md 权威；SkillTurbo 用 asyncio.gather 近似，差异如下：
# - skill 整轮派发     ≈ 全页 asyncio.gather 一次性拉起
# - skill 无数字并发上限 ≈ 页级不设 Semaphore；框架 LLM 全局信号量 ≠ 页派发上限
# - skill 按轮 retry_queue ≈ turbo 多为页内 inline 重试（accel 例外）；P11 更接近「首轮后按文件重试」
# - skill 验文件成功   ≈ 以落盘 research-P{N}.md / page-*.html / notes 分片为准，不以 LLM 口头完成
# - edit/maintain 路由：本期未实现；仅 intent 写入 edit_existing_ppt 检测字段
DISPATCH_ACCEL_NOTES = (
    "SkillTurbo maps dispatch.md whole-round fire to asyncio.gather; "
    "per-page inline retries are an accel exception vs retry_queue rounds."
)

CONTENT_BRANCH_MATERIAL = "material"
CONTENT_BRANCH_RESEARCH = "research"

# Phase4 交付态（对齐新 skill delivery-summary）
DELIVERY_STATUS_SENT = "sent"
DELIVERY_STATUS_UNAVAILABLE = "unavailable"
DELIVERY_STATUS_FAILED = "failed"

# 演示文稿后缀：不进入 parse-docs（与 pptx-craft 素材分支一致）
PRESENTATION_EXTENSIONS = frozenset({".ppt", ".pptx", ".pot", ".potx"})

_STYLE_CONSTRAINTS_PROMPT_PREFIX = (
    "用户显式版式要求（必须满足，优先级高于风格文件/模板默认值，"
    "冲突处理见 designer.md §用户显式要求优先）："
)

# Phase1 新增字段默认值（Wave0/1）；下游未消费时也保持键存在，避免 KeyError。
_PHASE1_DEFAULTS: dict[str, Any] = {
    "style_constraints": "",
    "user_dimensions": [],
    "user_structure": "",
    "notes_requirements": "",
    "notes_request_verbatim": [],
    "file_size_constraint": "",
    "doc_summary": "",
    "doc_summary_path": "",
    "doc_manifest_path": "",
    "material_map_path": "",
    "source_assets_dir": "",
    "vision_verified": False,
    "images_extracted": False,
    "parse_degraded": False,
    "page_count_user_specified": False,
    "content_branch": "",
    "thinking_strategy": "accelerated",  # P6/P8 DisableThinkingMixin 节点强制 thinking=off
    "presentation_paths": [],
}


def _strip_line_numbers(text: str) -> str:
    return _CAT_N_PREFIX_RE.sub("", text)


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
    def strip_page_gen_excluded_text(cls, text: str, inputs: dict[str, Any]) -> str:
        """从页面/大纲用 user 原文中逐字剥离备注触发片段与文件体积要求。

        契约：notes.md 调用方隔离 + SKILL.md §约束 11。fail-open：片段为空、
        等于全文、或剥离后剩余不足原文一半时原样返回。
        """
        source = text if isinstance(text, str) else ""
        if not source:
            return source

        fragments: list[str] = []
        verbatim = inputs.get("notes_request_verbatim") if isinstance(inputs, dict) else None
        if isinstance(verbatim, list):
            for item in verbatim:
                if isinstance(item, str) and item.strip():
                    fragments.append(item)
        elif isinstance(verbatim, str) and verbatim.strip():
            fragments.append(verbatim)
        if isinstance(inputs, dict):
            raw_size = inputs.get("file_size_constraint")
            if isinstance(raw_size, str) and raw_size.strip():
                fragments.append(raw_size)

        if not fragments:
            return source

        result = source
        for frag in fragments:
            if not frag or not frag.strip():
                logger.warning("[PptCommon] strip skip: empty fragment")
                continue
            if frag == source:
                logger.warning("[PptCommon] strip abort: fragment equals full text")
                return source
            if frag in result:
                result = result.replace(frag, "")

        src_len = len(source.strip())
        out_len = len(result.strip())
        if src_len > 0 and out_len < src_len * 0.5:
            logger.warning(
                "[PptCommon] strip abort: remaining %.0f%% < 50%%",
                (out_len / src_len) * 100 if src_len else 0,
            )
            return source
        return result

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

    @classmethod
    def parse_tool_file_content(cls, result: Any) -> str:
        """从 read_file / write_file 工具返回值中提取文本内容，并去掉 cat -n 行号前缀。"""
        if result is None:
            return ""
        # 增加兜底防止异常值(str(result)) 被当作文件内容返回。      
        if hasattr(result, "success") and result.success is False:
            return ""
        if isinstance(result, str):
            text = result.strip()
            if text.startswith("success=False") or text.startswith("success= False"):
                return ""
            return _strip_line_numbers(text)
        if isinstance(result, dict):
            content = result.get("content", "")
            if isinstance(content, str):
                return _strip_line_numbers(content.strip())
            return _strip_line_numbers(str(content or "").strip())
        if hasattr(result, "data"):
            data = result.data
            if isinstance(data, dict):
                content = data.get("content", "")
                if isinstance(content, str):
                    return _strip_line_numbers(content.strip())
                return _strip_line_numbers(str(content or "").strip())
            if isinstance(data, str):
                return _strip_line_numbers(data.strip())
        return _strip_line_numbers(str(result).strip())

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
        if not node.has_tool("read_file"):
            if required:
                raise error_type(f"read_file 工具不可用，无法读取 {label}")
            return ""
        try:
            result = await node.call_tool("read_file", file_path=str(path))
        except Exception as exc:
            if isinstance(exc, AbortError):
                raise
            if required:
                raise error_type(f"读取 {label} 失败: {path}: {exc}") from exc
            return ""
        text = cls.parse_tool_file_content(result)
        if not text:
            if required:
                raise error_type(f"{label} 为空或不存在: {path}")
            return ""
        if max_chars is not None and len(text) > max_chars:
            return text[:max_chars] + "\n\n...(内容已截断)"
        return text

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
            await node.call_tool(
                "write_file",
                file_path=str(path),
                content=normalized,
            )
        except Exception as exc:
            if isinstance(exc, AbortError):
                raise
            raise error_type(f"写入 {label} 失败: {path}: {exc}") from exc
        return path

    @classmethod
    def page_path_lock(cls, path: str):
        """Per-path async lock context manager for page HTML IO."""
        return page_path_lock_cm(path)

    @classmethod
    async def safe_overwrite_file(
        cls,
        node: Any,
        path: str,
        content: str,
        *,
        already_locked: bool = False,
        log_prefix: str = "[ppt]",
    ) -> bool:
        return await safe_overwrite_file_impl(
            node,
            path,
            content,
            already_locked=already_locked,
            log_prefix=log_prefix,
        )

    @classmethod
    def ensure_phase1_defaults(cls, inputs: dict[str, Any]) -> dict[str, Any]:
        """为 Phase1 新增字段补默认值；已有非空值不覆盖。"""
        for key, default in _PHASE1_DEFAULTS.items():
            if key not in inputs:
                inputs[key] = list(default) if isinstance(default, list) else default
            elif key in ("user_dimensions", "presentation_paths") and not isinstance(
                inputs.get(key), list
            ):
                inputs[key] = []
        return inputs

    @classmethod
    def is_presentation_path(cls, path: str | Path) -> bool:
        return Path(str(path)).suffix.casefold() in PRESENTATION_EXTENSIONS

    @classmethod
    def resolve_content_branch(cls, inputs: dict[str, Any]) -> str:
        """按新 skill §2.1：有内容文档/散图/数据文件 → material，否则 research。

        Wave1 仅写入字段；真正分支逻辑由 Wave2 消费。
        """
        doc_paths = inputs.get("doc_paths") or []
        image_paths = inputs.get("image_paths") or []
        has_docs = bool(inputs.get("has_documents")) or (
            isinstance(doc_paths, list) and len(doc_paths) > 0
        )
        has_images = isinstance(image_paths, list) and len(image_paths) > 0
        if has_docs or has_images:
            return CONTENT_BRANCH_MATERIAL
        return CONTENT_BRANCH_RESEARCH

    @classmethod
    def apply_content_branch(cls, inputs: dict[str, Any]) -> str:
        branch = cls.resolve_content_branch(inputs)
        inputs["content_branch"] = branch
        return branch

    @classmethod
    def ensure_content_branch(cls, inputs: dict[str, Any]) -> str:
        """返回已有 content_branch；缺失或非法时重新计算并写入。"""
        branch = str(inputs.get("content_branch") or "").strip()
        if branch in (CONTENT_BRANCH_MATERIAL, CONTENT_BRANCH_RESEARCH):
            return branch
        return cls.apply_content_branch(inputs)

    @staticmethod
    def min_citations_for_depth(research_depth: str) -> int:
        """新 skill：L1=2 / L2=3 / L3=5。"""
        mapping = {"L1": 2, "L2": 3, "L3": 5}
        return mapping.get(str(research_depth or "").strip().upper(), 3)

    @classmethod
    def is_expand_page_mode(cls, inputs: dict[str, Any]) -> bool:
        """dims/structure 非空且用户未指定页数 → 扩展模式（✅ ≥ page_count）。"""
        if inputs.get("page_count_user_specified"):
            return False
        dims = inputs.get("user_dimensions") or []
        has_dims = isinstance(dims, list) and any(
            isinstance(d, str) and d.strip() for d in dims
        )
        structure = str(inputs.get("user_structure") or "").strip()
        return has_dims or bool(structure)

    @classmethod
    async def load_source_material(
        cls,
        node: Any,
        inputs: dict[str, Any],
        *,
        max_chars: int = 4000,
        error_type: type[Exception] = RuntimeError,
    ) -> str:
        """优先 doc_summary（inline → path），再退 doc_raw；写入 inputs['source_material']。"""
        inline = str(inputs.get("doc_summary") or "").strip()
        if inline:
            text = inline[:max_chars]
            inputs["source_material"] = text
            return text

        summary_path = inputs.get("doc_summary_path")
        if summary_path:
            text = await cls.read_file(
                node,
                summary_path,
                max_chars=max_chars,
                error_type=error_type,
            )
            if text.strip():
                inputs["source_material"] = text
                return text

        text = await cls.read_file(
            node,
            inputs.get("doc_raw_path"),
            max_chars=max_chars,
            error_type=error_type,
        )
        inputs["source_material"] = text
        return text

    @staticmethod
    def format_style_constraints_prompt_line(constraints: Any) -> str:
        """非空时返回 build-standard 规定的逐字约束行；空则 \"\"（禁止写「无」）。"""
        text = str(constraints or "").strip()
        if not text:
            return ""
        return f"{_STYLE_CONSTRAINTS_PROMPT_PREFIX}{text}"

    @staticmethod
    def normalize_delivery_status(
        *,
        pptx_ok: bool,
        send_file_status: str,
    ) -> str:
        """归一 delivery_status → sent | unavailable | failed。"""
        status = str(send_file_status or "").strip()
        if not pptx_ok or status == "failed":
            return DELIVERY_STATUS_FAILED
        if status == "sent":
            return DELIVERY_STATUS_SENT
        if status in ("skipped", "unavailable"):
            return DELIVERY_STATUS_UNAVAILABLE
        return DELIVERY_STATUS_FAILED

    @classmethod
    async def load_designer_bundle(
        cls,
        node: Any,
        pptx_root: str,
        *,
        error_type: type[Exception] = RuntimeError,
    ) -> dict[str, str]:
        """读取拆分后的 designer 主文件 + 附录（appendix/charts/images）。"""
        root = str(pptx_root or "").strip()
        empty = {
            "designer_md_text": "",
            "designer_appendix_text": "",
            "designer_charts_text": "",
            "designer_images_text": "",
        }
        if not root:
            return empty

        base = Path(root) / "references"
        main = await cls.read_file(
            node, base / "designer.md", label="designer.md", error_type=error_type,
        )
        appendix = await cls.read_file(
            node,
            base / "designer" / "appendix.md",
            label="designer/appendix.md",
            error_type=error_type,
        )
        charts = await cls.read_file(
            node,
            base / "designer" / "charts.md",
            label="designer/charts.md",
            error_type=error_type,
        )
        images = await cls.read_file(
            node,
            base / "designer" / "images.md",
            label="designer/images.md",
            error_type=error_type,
        )
        return {
            "designer_md_text": main,
            "designer_appendix_text": appendix,
            "designer_charts_text": charts,
            "designer_images_text": images,
        }

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

    @staticmethod
    def default_mid_structural_pages(page_count: int) -> int:
        """outline-planner：用户要中间结构页但未给数量时的默认页数。

        ``page_count <= 5`` → 1；否则 ``ceil(page_count / 4)``。
        """
        if not isinstance(page_count, int) or page_count <= 0:
            return 1
        if page_count <= 5:
            return 1
        return -(-page_count // 4)

    @staticmethod
    def resolve_mid_structural_page_count(
        page_count: Any = None,
        structural_page_request: Any = "none",
        structural_page_count: Any = None,
    ) -> int:
        """解析中间结构页数量（不含 cover/ending），对齐 outline-planner。"""
        spr = str(structural_page_request or "none").strip().lower()
        if spr in ("", "none"):
            return 0
        if isinstance(structural_page_count, int) and structural_page_count > 0:
            return int(structural_page_count)
        if spr == "agenda":
            return 1
        try:
            pc = int(page_count) if page_count is not None else 0
        except (TypeError, ValueError):
            pc = 0
        return PptCommon.default_mid_structural_pages(pc)

    @staticmethod
    def content_pages_from_total_pages(
        total_pages: int,
        *,
        structural_page_request: Any = "none",
        structural_page_count: Any = None,
        exclude_cover_ending: bool = False,
    ) -> int:
        """§1.2B：用户总页数 N → 内容页 page_count（代码求解，不经 LLM 试算）。

        满足 ``N = page_count + cover_ending + mid_structural``。
        """
        if not isinstance(total_pages, int) or total_pages <= 0:
            return 1
        cover_ending = 0 if exclude_cover_ending else 2
        spr = str(structural_page_request or "none").strip().lower()
        if spr in ("", "none"):
            return max(total_pages - cover_ending, 1)
        if isinstance(structural_page_count, int) and structural_page_count > 0:
            return max(total_pages - cover_ending - int(structural_page_count), 1)
        for content in range(max(total_pages - cover_ending, 1), 0, -1):
            mid = PptCommon.resolve_mid_structural_page_count(
                content, spr, None,
            )
            if content + cover_ending + mid == total_pages:
                return content
        return max(total_pages - cover_ending - 1, 1)


# 与 pptx-craft CLI resolveDensity / EVIDENCE_LIMITED_ANNOTATION 对齐
_EVIDENCE_LIMITED_TERMS = (
    "数据有限",
    "数据不足",
    "资料有限",
    "资料不足",
    "来源有限",
    "来源不足",
    "证据不足",
)
_EVIDENCE_LIMITED_ANNOTATION_RE = re.compile(
    rf"(?:^|[\s>*\-–—。；;：:（(【\[])(?:{'|'.join(_EVIDENCE_LIMITED_TERMS)})[，,]\s*基于",
    re.MULTILINE,
)
_EVIDENCE_LIMITED_MENTION_RE = re.compile("|".join(_EVIDENCE_LIMITED_TERMS))


def research_evidence_limited_mentioned(text: str) -> bool:
    """Match validate-research EVIDENCE_LIMITED_MENTION (quota downgrade)."""
    return bool(_EVIDENCE_LIMITED_MENTION_RE.search(text or ""))


def pipeline_role_boundary(stage: str) -> str:
    """Shared cross-stage role boundary (pptx-craft SKILL.md)."""
    stage_label = stage.strip() or "PPT"
    lines = [
        f"### PPT 流水线角色边界（{stage_label}）",
        f"- 当前阶段 {stage_label}：只完成本阶段交付物，不代做下游工作",
        "- 输入优先级：user_dimensions > user_structure > topic > 自动推断",
        "- 证据驱动：无来源不写数值结论；缺口留给下游 research/designer",
        "- 结构标签（主轴/关键数据/上屏要点/案例/来源留痕）是文件 schema，不是幻灯片可见文案",
    ]
    if stage_label.upper().startswith("P8"):
        lines.append("- 几何以 check-layout CLI 退出码为最终权威")
    if stage_label.upper().startswith(("P4", "P6")):
        lines.append("- 信息不足不阻塞：描述性成稿，缺口写入研究查询/数据需求或标注数据有限")
    return "\n".join(lines) + "\n\n"


def resolve_layout_density(research_text: str | None) -> str:
    """Resolve check-layout --density tier from per-page research text."""
    if research_text is None:
        return "lean"
    text = research_text.strip()
    if not text:
        return "standard"
    if _EVIDENCE_LIMITED_ANNOTATION_RE.search(text):
        return "lean"
    return "standard"
