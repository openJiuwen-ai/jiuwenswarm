from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from jiuwenswarm.server.runtime.skill_turbo.plan_node import PlanNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_common import PptCommon

_collect_user_text = PptCommon.collect_user_text

_DOC_EXTENSIONS = (
    ".docx",
    ".doc",
    ".pdf",
    ".md",
    ".txt",
)

_IMAGE_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
)

_LLM_PATH_ONLY_SYSTEM_PROMPT = """你是文件路径提取助手。从用户消息中识别所有与 PPT 制作相关的本地文件路径或 @文件引用。

规则：
1. 只提取用户明确给出或可解析为具体路径的文件（含 @文件名、绝对路径、相对路径）。
2. 忽略纯自然语言指代且无法对应到具体路径的描述（如「那份周报」且无路径）。
3. 只返回文档/图片类文件（常见扩展名：docx、doc、pdf、md、txt、png、jpg、jpeg、gif、webp）。
4. 无文件时返回空数组。

必须只输出 JSON，格式：
{"doc_paths": ["路径1", "路径2"]}"""

_LLM_PATH_AND_SLOTS_SYSTEM_PROMPT = """你是 PPT 任务分析助手。从用户消息中按优先级完成以下任务：

第一步：文件路径提取（优先级最高）
- 只提取用户明确给出的本地文件路径或 @文件引用
- 忽略纯自然语言指代且无法对应到具体路径的描述（如「那份周报」且无路径）
- 只返回文档/图片类文件（docx、doc、pdf、md、txt、png、jpg、jpeg、gif、webp）
- 无文件时返回空数组

第二步：PPT 需求信息提取（仅当没有找到任何文件路径时执行）
- 只提取用户**明确提到**的信息，不要推断或补充
- 未提及的字段留空字符串或 null
- page_count 必须是正整数（内容页数，不含封面/结束页，也不含目录页/章节页等中间结构页；总页数 = page_count + 2 + 中间结构页数）。
  判断规则：①用户说"生成N页PPT"/"做N页汇报"/"PPT共N页"/"总页数N页"/"总共N页"/"一共N页"/"N页"/"做N页PPT"/"N页以内"/"不超过N页"/"最多N页"等未特指内容页的表达 -> N 表示总页数 -> page_count = max(N - 2 - 结构页扣减, 1)；结构页扣减 = 用户明确要求的中间结构页数量（取本请求提取的 structural_page_request / structural_page_count）：structural_page_request != "none" 且用户指定数量时按 structural_page_count 扣减；未指定数量时按 1 页扣减（如目录页）；structural_page_request == "none" 时扣减 0；
  ②用户明确说"N个内容页"/"N页正文"，或正在回答"需要多少页内容页"时 → page_count = N（中间结构页另行添加，不占此配额）。
  示例："10页以内"→8, "总页数8页"→6, "8页"→6, "做8页PPT"→6, "共7页"+要求目录页→4, "8页PPT"+3个章节页→3
- style_id 可选值：business-classic / tech-minimal / elegant-narrative / industrial-tech / custom / 其他风格名
  用户要求“自由发挥”时填写 custom
  “华为风格/华为/华为红/华为风/华为商务”统一填写 business-classic，不得填 custom
- audience 可选值：公司高管 / 技术团队 / 投资人 / 普通受众 / 其他
- presentation_purpose 可选值：工作汇报 / 产品展示 / 教学分享 / auto / 其他
- pack_dir: 用户提供的模板包目录绝对路径（字符串；未知则 ""）。
  当用户在消息中提到"用 XX 模板""用模板包""template pack"等，且给出了目录路径时提取该路径。
  路径可能是 Windows 格式（如 D:\\path\\to\\pack）或 Unix 格式（/path/to/pack）。
  仅提取用户明确给出的路径，不要编造。
- structural_page_request: 用户是否要求中间结构页（目录页/章节页/分隔页）。字符串，取值：
  "none"（默认，未要求任何中间结构页）；
  "agenda"（用户要求目录页/议程页）；
  "section"（用户要求章节页/章节分隔页/分节页）；
  "chapter"（用户要求 PART 页/章首页/Chapter）；
  "auto"（用户要求章节页但未指定类型，由大纲规划阶段自动选择 section 或 chapter）。
  提取规则：仅当用户明确表达时才提取，例如"加章节页""每章一个章节页""需要目录页""加 PART 页""加章首页"。
  普通章节结构、素材中的标题层级、模型自己觉得需要分节，都不构成触发条件 -> "none"。
  用户指定数量时（如"加 2 页章节页"），数量信息保留在 structural_page_count 中。
- structural_page_count: 用户指定的中间结构页数量（整数；未指定或"每章一个"等需自动计算时为 null）。
- requested_total_pages: 用户原文明确要求的总页数；未提及则为 null。保留原值，不换算成内容页数。
- required_sections: 仅当用户用“包含/包括/含有/涵盖”等表达明确列出 PPT 页面或章节清单时填写。
  数组元素格式为 {"title":"用户原文标题","page_type":"cover|agenda|content|ending"}。
  标题页/封面归 cover，目录/议程归 agenda，可承担最终总结的展望/结论/致谢归 ending，其余明确业务章节归 content。
  “包含销量数据、柱状图、案例”等页内素材或表现形式不属于页面清单，必须返回空数组。
  当 required_sections 非空时，必须完整保留用户列出的所有页面/章节，不因 requested_total_pages 较小而删减或合并。

重要：如果找到了文件路径，slots 各字段留空，不需要提取需求信息；
      如果没有找到任何文件路径，则必须提取 slots 信息。

必须只输出 JSON，格式：
{"doc_paths": ["路径1"], "slots": {"topic": "", "page_count": null, "audience": "",
"presentation_purpose": "", "style_id": "", "pack_dir": "",
"structural_page_request": "none", "structural_page_count": null,
"requested_total_pages": null, "required_sections": []}}
（page_count 为正整数或 null，禁止字符串）"""


def _normalize_doc_path(raw: str) -> str | None:
    value = (raw or "").strip().strip("\"'")
    if not value:
        return None
    try:
        path = Path(value).expanduser()
        return str(path.resolve()) if path.exists() else str(path)
    except (OSError, ValueError):
        return value


def _dedupe_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in paths:
        normalized = _normalize_doc_path(item)
        if not normalized:
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(normalized)
    return ordered


def _looks_like_document_path(path: str) -> bool:
    suffix = Path(path).suffix.casefold()
    return suffix in _DOC_EXTENSIONS or suffix in _IMAGE_EXTENSIONS


def _is_image_path(path: str) -> bool:
    return Path(path).suffix.casefold() in _IMAGE_EXTENSIONS


def _split_image_paths(paths: list[str]) -> tuple[list[str], list[str]]:
    """将路径列表分流为 (doc_paths, image_paths)。图片不进 doc_paths，避免触发 P3 Eve 解析。"""
    docs: list[str] = []
    images: list[str] = []
    for p in paths:
        if _is_image_path(p):
            images.append(p)
        else:
            docs.append(p)
    return docs, images


_FILE_PATH_KEYS = ("path", "file_path", "filepath", "local_path", "uri")


def _flatten_file_entries(raw: Any) -> list[Any]:
    """将 attachments / files 各类容器展平为路径字符串或文件对象列表。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, dict):
        entries: list[Any] = []
        for value in raw.values():
            if isinstance(value, list):
                entries.extend(value)
            elif value is not None:
                entries.append(value)
        return entries
    if isinstance(raw, list):
        flat: list[Any] = []
        for item in raw:
            if isinstance(item, list):
                flat.extend(item)
            else:
                flat.append(item)
        return flat
    return []


def _collect_paths_from_file_entries(raw: Any) -> list[str]:
    paths: list[str] = []
    for item in _flatten_file_entries(raw):
        if isinstance(item, str):
            paths.append(item)
        elif isinstance(item, dict):
            for key in _FILE_PATH_KEYS:
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    paths.append(value)
                    break
    return _dedupe_paths(paths)


def _collect_attachment_paths(inputs: dict[str, Any]) -> list[str]:
    return _collect_paths_from_file_entries(inputs.get("attachments"))


def _collect_files_paths(inputs: dict[str, Any]) -> list[str]:
    """读取 interface 传入的 files（含 OfficeClaw files.uploaded 格式）。"""
    return _collect_paths_from_file_entries(inputs.get("files"))


_SLOT_NAMES = ("topic", "page_count", "audience", "presentation_purpose", "style_id", "pack_dir")


def _build_llm_path_prompt(text: str) -> str:
    return (
        "请从以下用户消息中提取所有文档/图片文件路径，按 JSON 格式返回。\n\n"
        f"用户消息：\n{text.strip()}"
    )


def _build_llm_path_and_slots_prompt(text: str) -> str:
    return (
        "请从以下用户消息中提取文件路径和 PPT 需求信息，按 JSON 格式返回。\n"
        "如果有文件路径，重点提取路径，slots 各字段留空；\n"
        "如果没有文件路径，则提取 PPT 需求信息填入 slots。\n\n"
        f"用户消息：\n{text.strip()}"
    )


class IntentClassifyError(RuntimeError):
    """P1 意图识别失败。"""


# 演讲备注触发词（prod Stage 8 契约）
_SPEAKER_NOTES_KEYWORDS = (
    "演讲备注", "演讲者备注", "讲稿", "speaker notes", "演讲稿",
    "口播稿", "旁白", "备注稿", "演讲要点",
)

# 编辑已有 PPT 触发词
_EDIT_EXISTING_KEYWORDS = (
    "修改这个ppt", "编辑这个ppt", "改这个ppt", "调整这个ppt",
    "修改这份ppt", "编辑这份ppt", "改这份ppt", "调整这份ppt",
    "modify this ppt", "edit this ppt",
)


def _detect_speaker_notes_request(text: str) -> bool:
    """检测用户是否要求生成演讲备注。"""
    if not text:
        return False
    lower = text.lower()
    return any(kw in lower for kw in _SPEAKER_NOTES_KEYWORDS)


def _detect_edit_existing_request(text: str, doc_paths: list[str]) -> bool:
    """检测用户是否要编辑已有 PPT（而非从零生成）。"""
    if not text:
        return False
    lower = text.lower()
    if any(kw in lower for kw in _EDIT_EXISTING_KEYWORDS):
        return True
    # 用户上传了 .pptx 文件且明确要"修改/编辑"
    for p in doc_paths:
        if p.lower().endswith(".pptx"):
            if any(kw in lower for kw in ("修改", "编辑", "改", "调整", "modify", "edit")):
                return True
    return False


def _parse_slots_from_llm_response(raw: str) -> dict[str, Any]:
    """从 LLM 响应中提取 slots 字段，返回 {slot_name: value_or_empty}。"""
    if not raw or not raw.strip():
        return {}
    text = raw.strip()
    fence_match = PptCommon.JSON_FENCE_PATTERN.search(text)
    if fence_match:
        text = fence_match.group(1).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        object_match = re.search(r"\{[\s\S]*\}", text)
        if not object_match:
            return {}
        try:
            payload = json.loads(object_match.group(0))
        except json.JSONDecodeError:
            return {}
    if not isinstance(payload, dict):
        return {}
    slots_raw = payload.get("slots")
    if not isinstance(slots_raw, dict):
        return {}
    result: dict[str, Any] = {}
    for name in _SLOT_NAMES:
        value = slots_raw.get(name)
        if value is None and name == "page_count":
            result[name] = None
        elif name == "page_count" and isinstance(value, str) and value.strip():
            raise IntentClassifyError(
                f"page_count 必须是正整数或 null，LLM 返回了字符串: {value!r}"
            )
        elif isinstance(value, str) and value.strip():
            result[name] = value.strip()
        elif isinstance(value, (int, float)) and name == "page_count":
            result[name] = value
        else:
            result[name] = "" if name != "page_count" else None
    requested_total = slots_raw.get("requested_total_pages")
    if isinstance(requested_total, (int, float)) and requested_total > 0:
        result["requested_total_pages"] = int(requested_total)
    else:
        result["requested_total_pages"] = None
    result["required_sections"] = PptCommon.normalize_required_sections(
        slots_raw.get("required_sections")
    )
    return result


def _parse_paths_from_llm_response(raw: str) -> list[str]:
    if not raw or not raw.strip():
        return []

    text = raw.strip()
    fence_match = PptCommon.JSON_FENCE_PATTERN.search(text)
    if fence_match:
        text = fence_match.group(1).strip()

    payload: Any
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        array_match = re.search(r"\[[\s\S]*\]", text)
        object_match = re.search(r"\{[\s\S]*\}", text)
        candidate = array_match.group(0) if array_match else (
            object_match.group(0) if object_match else ""
        )
        if not candidate:
            return []
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            return []

    if isinstance(payload, dict):
        paths = payload.get("doc_paths") or payload.get("paths") or payload.get("files")
    elif isinstance(payload, list):
        paths = payload
    else:
        return []

    if not isinstance(paths, list):
        return []

    filtered = [
        str(item).strip()
        for item in paths
        if isinstance(item, str) and str(item).strip() and _looks_like_document_path(str(item))
    ]
    return _dedupe_paths(filtered)


class IntentClassifyNode(PlanNode):
    """P1 — workflow 内文档门控（结构化附件 + LLM 文本解析 + 无附件时槽位预提取）。

    说明：PPT 任务是否进入本 workflow 由 SkillTurbo / Planner 在入口判定；
    本节点负责标记是否存在待解析文档，供根节点决定先 P3 还是直接 P2。
    当无附件且 query 中无文件路径时，从同一 LLM 调用中提取槽位信息供 P2 快捷使用。

    预期输入（ctx / inputs）:
        可选: attachments — 附件绝对路径列表，或含 path/file_path/uri 字段的对象列表
        可选: files — OfficeClaw 等通道的结构化上传文件（如 files.uploaded[].path）
        可选: task | user_request | user_message | query — 用户文本

    预期输出（写入同一 ctx）:
        has_documents: bool — 是否进入 P3 文档解析
        doc_paths: list[str] — 待解析文件路径列表（无文档时为 []）
        slots_from_query: dict — 仅在无附件且无路径时非空，从 query 预提取的槽位信息
        slots_from_query_complete: bool — 仅在无附件且无路径时写入，预提取槽位是否全部非空
        topic_from_query / page_count_from_query / ... — 各槽位的预填值
    """

    def __init__(self) -> None:
        super().__init__(
            plan_name="p1_intent_classify",
            instruction=(
                "## P1 文档检测与意图分类\n"
                "\n"
                "### 节点职责\n"
                "1. 检测用户是否提供了待解析文档（attachments / files / query 中的路径）\n"
                "2. 决定是否进入 P3 文档解析（`has_documents` 门控根节点 P3 跳过/执行）\n"
                "3. 无附件且无路径时，LLM 从 query 预提取槽位信息供 P2 快捷路径使用\n"
                "\n"
                "### 前置条件\n"
                "- P0 已完成，`output_dir` 已就绪\n"
                "- `stream_llm` / `ask_user` 工具可用\n"
                "\n"
                "### 输入\n"
                "- `attachments`（可选）: 附件绝对路径列表，或含 path/file_path/uri 字段的对象列表\n"
                "- `files`（可选）: OfficeClaw 等通道的结构化上传文件\n"
                "- `task` | `user_request` | `user_message` | `query`（可选）: 用户文本\n"
                "\n"
                "### 输出\n"
                "- `has_documents`: bool — 是否进入 P3 文档解析（决定根节点 P3 跳过/执行）\n"
                "- `doc_paths`: list[str] — 待解析文件绝对路径列表（无文档时为 []）\n"
                "\n"
                "无附件且无路径时额外产出（供 P2 快捷路径使用）：\n"
                "- `slots_from_query`: dict — 从 query 预提取的槽位信息\n"
                "- `slots_from_query_complete`: bool — 预提取槽位是否全部非空\n"
                "- `topic_from_query` / `page_count_from_query` 等 — 各槽位预填值\n"
                "\n"
                "### 执行流程\n"
                "1. 优先读取 `attachments` + `files`，提取文件路径\n"
                "2. 有附件时：LLM 仅从 query 提取额外文件路径 → 合并为 `doc_paths`，`has_documents=True`\n"
                "3. 无附件时：LLM 从 query 同时提取路径与槽位信息\n"
                "   - 有路径 → `has_documents=True`, `doc_paths` 非空\n"
                "   - 无路径 → `has_documents=False`, `doc_paths=[]`, 写入 `slots_from_query` 供 P2 快捷\n"
                "\n"
                "### 失败兜底\n"
                "- attachments/files 解析异常: has_documents=False, doc_paths=[]\n"
                "- LLM 调用失败: has_documents=False, doc_paths=[], slots_from_query 为空\n"
            ),
        )

    async def _extract_paths_only_with_llm(self, text: str) -> list[str]:
        """场景 A：有附件，LLM 仅从 query 提取额外文件路径。"""
        if not text or not text.strip():
            return []
        response = await self.stream_llm_collect(
            _build_llm_path_prompt(text),
            system_prompt=_LLM_PATH_ONLY_SYSTEM_PROMPT,
        )
        return _parse_paths_from_llm_response(response)

    async def _extract_paths_and_slots_with_llm(
        self, text: str,
    ) -> tuple[list[str], dict[str, Any]]:
        """场景 B/C：无附件，LLM 提取路径 +（没路径时）提取槽位。"""
        if not text or not text.strip():
            return [], {}
        response = await self.stream_llm_collect(
            _build_llm_path_and_slots_prompt(text),
            system_prompt=_LLM_PATH_AND_SLOTS_SYSTEM_PROMPT,
        )
        doc_paths = _parse_paths_from_llm_response(response)
        slots = _parse_slots_from_llm_response(response)
        if doc_paths:
            # 场景 B：query 中有路径 → 用路径，不用 slots
            return doc_paths, {}
        # 场景 C：无路径 → 用 slots 预填 P2
        return [], slots

    async def _collect_doc_paths(
        self, inputs: dict[str, Any],
    ) -> tuple[list[str], dict[str, Any]]:
        """返回 (doc_paths, slots)。slots 仅在无附件且无路径时非空。"""

        # 1. 收集结构化附件 + files
        from_attachments = _collect_attachment_paths(inputs)
        from_files = _collect_files_paths(inputs)
        structured_paths = _dedupe_paths(from_attachments + from_files)
        has_structured = bool(structured_paths)

        user_text = _collect_user_text(inputs)
        if not user_text:
            return structured_paths, {}

        if has_structured:
            # 场景 A：有附件，LLM 仅提取 query 中额外的文件路径
            llm_paths = await self._extract_paths_only_with_llm(user_text)
            merged = _dedupe_paths(structured_paths + llm_paths)
            return merged, {}  # slots 为空，信息留给 P2 从附件内容提取

        # 场景 B/C：无附件，一次调用同时判断路径 + 提取信息
        doc_paths, slots = await self._extract_paths_and_slots_with_llm(user_text)
        if doc_paths:
            # 场景 B：query 中有路径 → 用路径，不用 slots
            return doc_paths, {}
        # 场景 C：无附件也无路径 → 用 slots 预填 P2
        return [], slots

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        doc_paths, slots = await self._collect_doc_paths(inputs)
        # 图片路径分流：图片不进 doc_paths（不触发 P3 Eve），单独存 image_paths 供 Diana
        doc_paths, image_paths = _split_image_paths(doc_paths)
        inputs["doc_paths"] = doc_paths
        inputs["image_paths"] = image_paths
        inputs["has_documents"] = bool(doc_paths)

        # 演讲备注触发词检测（prod Stage 8 契约）
        user_text = PptCommon.collect_user_text(inputs)
        inputs["need_speaker_notes"] = _detect_speaker_notes_request(user_text)

        # 编辑已有 PPT 路由检测（prod 路由表：编辑已有页面入口）
        inputs["edit_existing_ppt"] = _detect_edit_existing_request(user_text, doc_paths)

        # 仅在场景 C（无附件、无路径、slots 非空）时写入预填信息
        if not inputs["has_documents"] and slots:
            all_filled = (
                bool(slots.get("topic", ""))
                and slots.get("page_count") is not None
                and bool(slots.get("audience", ""))
                and bool(slots.get("presentation_purpose", ""))
                and bool(slots.get("style_id", ""))
            )
            inputs["slots_from_query"] = slots
            inputs["slots_from_query_complete"] = all_filled
            for k, v in slots.items():
                inputs[f"{k}_from_query"] = v

        return inputs

    async def _execute_stream(self, inputs: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        yield {
            "node": self.plan_name,
            "status": "progress",
            "message": "正在识别 PPT 任务中的附件与需求信息...",
        }
        result = await self._execute(inputs)
        has_docs = result.get("has_documents")
        slots_complete = result.get("slots_from_query_complete")
        if has_docs:
            msg = f"发现 {len(result['doc_paths'])} 个待解析文件"
        elif slots_complete:
            msg = "未发现附件，已从需求描述中提取完整信息"
        elif result.get("slots_from_query"):
            msg = "未发现附件，已提取部分需求信息，后续将补充确认"
        else:
            msg = "未发现附件，将收集详细需求"
        yield {
            **result,
            "node": self.plan_name,
            "status": "ok",
            "message": msg,
        }
