from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError, PlanNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_common import PptCommon

logger = logging.getLogger(__name__)

_TEXT_SOURCE_KEYS = PptCommon.TEXT_SOURCE_KEYS
_collect_user_text = PptCommon.collect_user_text
_parse_json_payload = PptCommon.parse_json_payload
_DOC_EXCERPT_MAX_CHARS = 4000
_DEFAULT_AUDIENCE = "通用商务/知识分享"
_DEFAULT_PRESENTATION_PURPOSE = "auto"
_DEFAULT_PAGE_COUNT = 6
_MAX_PAGE_COUNT = 30

_VALID_STYLE_IDS = frozenset(
    {"business-classic", "tech-minimal", "elegant-narrative", "industrial-tech", "custom"}
)
_VALID_SEARCH_MODES = frozenset({"auto", "no_search", "force_search"})
_VALID_SOURCE_TYPES = frozenset({"topic", "outline", "description"})
_VALID_RESEARCH_DEPTHS = frozenset({"L1", "L2", "L3"})
_VALID_STRUCTURAL_REQUESTS = frozenset(
    {"none", "agenda", "section", "chapter", "auto"}
)

_STYLE_LABEL_TO_ID: dict[str, str] = {
    "商务经典": "business-classic",
    "华为": "business-classic",
    "华为风格": "business-classic",
    "科技极简": "tech-minimal",
    "典雅叙事": "elegant-narrative",
    "工业科技": "industrial-tech",
    "自由发挥": "custom",
}

_PAGE_LABEL_TO_COUNT: dict[str, int] = {
    "3-6 页（推荐）": 6,
    "8-12 页": 10,
    "15-20 页": 18,
}

_PURPOSE_LABEL_TO_VALUE: dict[str, str] = {
    "工作汇报": "工作汇报",
    "产品/方案展示": "产品展示",
    "教学/分享": "教学分享",
    "AI 自动判断": "auto",
}

_SLOT_FIELDS = ("topic", "page_count", "audience", "presentation_purpose", "style_id")
_ASK_BATCH_FIELDS = ("page_count", "audience", "presentation_purpose")
_P21_GAP_FIELDS = _ASK_BATCH_FIELDS

_P21_SLOT_SYSTEM_PROMPT = ("""你是 PPT 需求槽位分析助手。从用户消息与文档摘要中提取已知信息，并判断仍缺失的字段。

提取字段：
- topic: 演示主题（字符串；未知则 ""）
- page_count: 内容页数（整数；不含封面/结束页；总页数 = page_count + 2；未知则 null）。
  判断规则：①用户说"生成N页PPT"/"做N页汇报"/"PPT共N页"/"总页数N页"/"总共N页"/"一共N页"/"N页"/"做N页PPT"/"N页以内"/"不超过N页"/"最多N页"/"不大于N页"等未特指内容页的表达 → N 表示总页数 → page_count = max(N - 2, 1)；
  ②用户明确说"N个内容页"/"N页正文"，或正在回答"需要多少页内容页"时 → page_count = N。
  示例："10页以内"→8, "总页数严格为8页"→6, "8页"→6, "做8页PPT"→6
- audience: 目标受众（字符串；未知则 ""）
- presentation_purpose: 汇报目的，如「工作汇报」「产品展示」「教学分享」「auto」；未知则 ""
- style_id: 用户明确提及风格时填写：business-classic / tech-minimal / elegant-narrative / industrial-tech / custom；“自由发挥”统一填写 custom；未知则 ""
  “华为风格/华为/华为红/华为风/华为商务”统一填写 business-classic，不得填 custom
- style_description: style_id 为 custom 时的描述；否则 ""
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
  提取规则：仅当用户明确表达时才提取，例如"加章节页""每章一个章节页""加 3 页章节分隔""需要目录页""保留我大纲里的章节页""加 PART 页""加章首页"。
  普通章节结构、素材中的标题层级、模型自己觉得需要分节，都不构成触发条件 -> "none"。
  用户指定数量时（如"加 2 页章节页"），数量信息保留在 structural_page_count 中。
- structural_page_count: 用户指定的中间结构页数量（整数；未指定或"每章一个"等需自动计算时为 null）。
- missing_fields: 仍缺失且需用户补充的字段名数组，取值限于 topic / page_count / audience / presentation_purpose / style_id
- need_ask_style: 用户未明确风格时为 true，否则 false

规则：
1. 不要编造用户未提及的信息。
2. 页数最多 30；范围取合理中位值。
3. 已知主题（来自上游 P3）时不要修改 topic，且 missing_fields 不得包含 topic。
4. 不要输出 search_mode / source_type。
5. topic 缺失时由下游 LLM 生成 4 个主题候选并 ask 用户选择，不要生成询问文案。
6. pack_dir 存在时 style_id 填 "custom"（模板包优先于预设风格），need_ask_style 设 false。
7. page_count 为内容页数（不含封面/结束页），系统会在此基础上自动加 2 页（封面+结束页）。
   用户说"生成N页PPT"/"做N页汇报"/"PPT共N页"/"总页数N页"/"总共N页"/"一共N页"/"N页"/"做N页PPT"/"N页以内"/"不超过N页"/"最多N页"等未特指内容页的表达 → N 表示总页数，page_count = max(N - 2, 1)；
   用户明确说"N个内容页"/"N页正文"或正在回答"需要多少页内容页"时，page_count = N。

必须只输出 JSON："""
    + '{"topic":"","page_count":null,"audience":"","presentation_purpose":"",'
    + '"style_id":"","style_description":"","pack_dir":"",'
    + '"structural_page_request":"none","structural_page_count":null,'
    + '"missing_fields":[],"need_ask_style":true}')

_TOPIC_SUGGEST_COUNT = 4

_P21_TOPIC_SUGGEST_SYSTEM_PROMPT = f"""你是 PPT 主题策划助手。根据用户消息与文档摘要，生成恰好 {_TOPIC_SUGGEST_COUNT} 个可直接作为演示文稿主题的候选。

要求：
1. 每个主题必须足够具体、完整，单独一条即可支撑一次 PPT 制作（含明确对象、范围或角度），通常 12~40 字。
2. 四个主题应互不重复，覆盖不同切入点（如受众、角度、范围、侧重点）。
3. 不要输出笼统词如「工作总结」「产品介绍」，要落到可执行的演示命题。
4. 不要编造与用户上下文无关的主题。

必须只输出 JSON：
{{"topics":["主题1","主题2","主题3","主题4"]}}"""

_P24_SYSTEM_PROMPT = """你是 PPT 流水线派生参数分析助手。根据已收集的需求与文档情况，推断 search_mode、source_type、research_depth。

search_mode 规则（互斥，按优先级取第一个匹配）：
1. 用户明确要求不搜索、仅按给定材料、局部改稿或样式微调 → no_search
2. 用户要求最新数据、趋势、市场分析、竞品对比等 → force_search
3. 其余情况（含宽泛主题、有/无文档、用户提供大纲等）→ auto

source_type 规则（核心判据：用户对每页内容的指导深度）：
- 用户提供了结构化大纲文本（章节/页面结构），各条目以标题或简短主题为主，未对单页的内容细节（如数据维度、图表选型、视觉规范、解读逻辑等）做明确指导 → outline
- 用户提供了完整的内容描述，不仅给出了页面结构，还对各页的内容细节有明确指导（如指定了图表类型、数据维度、视觉设计要求、解读规则、颜色规范等），系统可直接据此生成页面内容 → description
- 用户给出宽泛主题、简短描述，或主要依赖上传文档 → topic
注意：不要仅凭"是否有编号列表"来判断 outline。关键看每个列表项的内容深度——只给出页面主题/标题的为 outline，已包含具体内容指导的为 description。

research_depth 规则（与 search_mode、page_count 联动；L1/L2/L3 含义见下游 research-writer）：
- search_mode 为 no_search → L1
- search_mode 为 force_search，或 page_count > 15 → L3
- page_count 在 8~15 → L2
- 其余（含 auto 且页数 ≤7）→ L1

need_imagegen 规则（本地图片之外是否需要 AI 生成配图）：
- 用户明确要求 AI 生图/生成配图/插图/插画 → true
- 用户描述了希望出现的具体画面/场景/元素（如"画面包含X、Y、Z""X组合画面""要配风电场景图"），或对画面内容有具体约束（如"不出现人像""背景必须是森林"）→ true
- 仅泛泛的主题词或风格词（如"科技风""简约大气"），未描述任何具体画面 → false

必须只输出 JSON，四个字段均必填且取值必须在枚举内：
{"search_mode":"auto","source_type":"topic","research_depth":"L2","need_imagegen":false}"""


class RequirementCollectError(RuntimeError):
    """P2 需求收集失败。"""


def _normalize_page_count(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        count = value
    elif isinstance(value, float):
        count = int(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        range_match = re.search(r"(\d+)\s*[-~到]\s*(\d+)", stripped)
        if range_match:
            low, high = int(range_match.group(1)), int(range_match.group(2))
            count = (low + high) // 2
        else:
            digits = re.search(r"\d+", stripped)
            if not digits:
                return None
            count = int(digits.group(0))
    else:
        return None

    if count < 1:
        return None
    return min(count, _MAX_PAGE_COUNT)


def _normalize_style_id(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    raw = value.strip()
    if not raw:
        return ""
    lowered = raw.casefold()
    alias_map = {
        "business-classic": "business-classic",
        "huawei": "business-classic",
        "华为": "business-classic",
        "华为风格": "business-classic",
        "商务经典": "business-classic",
        "tech-minimal": "tech-minimal",
        "tech minimal": "tech-minimal",
        "科技极简": "tech-minimal",
        "elegant-narrative": "elegant-narrative",
        "典雅叙事": "elegant-narrative",
        "industrial-tech": "industrial-tech",
        "工业科技": "industrial-tech",
        # 兼容历史输入，但下游只保留新版 canonical custom 状态。
        "free": "custom",
        "自由发挥": "custom",
        "custom": "custom",
    }
    if lowered in alias_map:
        return alias_map[lowered]
    if raw in _VALID_STYLE_IDS:
        return raw
    return ""


def _resolve_style_id(style_id: Any, style_description: Any = None) -> str:
    """归一化 style_id，支持从 style_description 回退。

    当 LLM 输出 style_id="custom" + style_description="华为风格" 时，
    先尝试对 style_description 做归一化，匹配别名映射（如"华为风格"→"business-classic"）。
    """
    normalized = _normalize_style_id(style_id)
    if normalized and normalized != "custom":
        return normalized
    if normalized == "custom":
        desc = _normalize_style_id(style_description)
        if desc and desc != "custom":
            return desc
    return normalized


def _style_id_from_label(label: str) -> tuple[str, str]:
    text = label.strip()
    if not text or text == "其他":
        return "", ""
    if text in _STYLE_LABEL_TO_ID:
        return _STYLE_LABEL_TO_ID[text], ""
    normalized = _normalize_style_id(text)
    if normalized:
        return normalized, ""
    return "custom", text


def _page_count_from_label(label: str, other_text: str = "") -> int | None:
    source = other_text.strip() or label.strip()
    if label.strip() in _PAGE_LABEL_TO_COUNT:
        return _PAGE_LABEL_TO_COUNT[label.strip()]
    return _normalize_page_count(source)


def _purpose_from_label(label: str, other_text: str = "") -> str:
    source = label.strip()
    if source in _PURPOSE_LABEL_TO_VALUE:
        return _PURPOSE_LABEL_TO_VALUE[source]
    custom = other_text.strip() or source
    return custom if custom and custom != "其他" else _DEFAULT_PRESENTATION_PURPOSE


def _audience_from_label(label: str, other_text: str = "") -> str:
    custom = other_text.strip()
    if custom and label.strip() in ("其他", _DEFAULT_AUDIENCE):
        return custom
    source = label.strip()
    if not source or source == "其他":
        return _DEFAULT_AUDIENCE
    return source


def _has_nonempty_topic(inputs: dict[str, Any]) -> bool:
    topic = inputs.get("topic")
    return isinstance(topic, str) and bool(topic.strip())


def _set_requirement_artifact(ctx: dict[str, Any]) -> None:
    """把 P2 需求收集的关键槽位写入 __artifact__，供跨请求复用。"""
    ctx["__artifact__"] = {
        "info": {
            "topic": ctx.get("topic", ""),
            "page_count": ctx.get("page_count"),
            "style_id": ctx.get("style_id", ""),
            "audience": ctx.get("audience", ""),
            "presentation_purpose": ctx.get("presentation_purpose", ""),
        },
    }


def _apply_slot_defaults(inputs: dict[str, Any]) -> None:
    if not inputs.get("audience"):
        inputs["audience"] = _DEFAULT_AUDIENCE
    if not inputs.get("presentation_purpose"):
        inputs["presentation_purpose"] = _DEFAULT_PRESENTATION_PURPOSE
    if inputs.get("page_count") is None:
        inputs["page_count"] = _DEFAULT_PAGE_COUNT


def _batch_field_is_satisfied(inputs: dict[str, Any], field: str) -> bool:
    if field == "page_count":
        return inputs.get("page_count") is not None
    if field == "audience":
        audience = inputs.get("audience")
        return isinstance(audience, str) and bool(audience.strip())
    if field == "presentation_purpose":
        purpose = inputs.get("presentation_purpose")
        return isinstance(purpose, str) and bool(purpose.strip())
    return False


def _unsatisfied_batch_fields(inputs: dict[str, Any]) -> list[str]:
    return [
        field
        for field in _ASK_BATCH_FIELDS
        if not _batch_field_is_satisfied(inputs, field)
    ]


def _require_batch_fields_collected(inputs: dict[str, Any]) -> None:
    missing = _unsatisfied_batch_fields(inputs)
    if not missing:
        return
    labels = {
        "page_count": "页数",
        "audience": "受众",
        "presentation_purpose": "汇报目的",
    }
    names = "、".join(labels.get(field, field) for field in missing)
    raise RequirementCollectError(f"需求收集未完成：缺少 {names}")


def _prune_satisfied_batch_missing_fields(inputs: dict[str, Any]) -> None:
    inputs["missing_fields"] = [
        field
        for field in (inputs.get("missing_fields") or [])
        if field not in _ASK_BATCH_FIELDS or not _batch_field_is_satisfied(inputs, field)
    ]


def _merge_slot_payload(
    inputs: dict[str, Any],
    payload: dict[str, Any],
    *,
    preserve_topic: bool = False,
) -> None:
    if not preserve_topic:
        topic = payload.get("topic")
        if isinstance(topic, str) and topic.strip():
            inputs["topic"] = topic.strip()

    page_count = _normalize_page_count(payload.get("page_count"))
    if page_count is not None:
        inputs["page_count"] = page_count

    audience = payload.get("audience")
    if isinstance(audience, str) and audience.strip():
        inputs["audience"] = audience.strip()

    purpose = payload.get("presentation_purpose")
    if isinstance(purpose, str) and purpose.strip():
        inputs["presentation_purpose"] = purpose.strip()

    style_id = _resolve_style_id(payload.get("style_id"), payload.get("style_description"))
    if style_id:
        inputs["style_id"] = style_id

    style_description = payload.get("style_description")
    if isinstance(style_description, str) and style_description.strip():
        inputs["style_description"] = style_description.strip()

    pack_dir = payload.get("pack_dir")
    if isinstance(pack_dir, str) and pack_dir.strip():
        inputs["pack_dir"] = pack_dir.strip()
        # pack_dir 存在时强制 style_id=custom，跳过风格询问
        if not inputs.get("style_id"):
            inputs["style_id"] = "custom"
        inputs["need_ask_style"] = False

    missing = payload.get("missing_fields")
    if isinstance(missing, list):
        allowed = (
            tuple(field for field in _SLOT_FIELDS if field != "topic")
            if preserve_topic
            else _SLOT_FIELDS
        )
        inputs["missing_fields"] = [
            str(item).strip()
            for item in missing
            if isinstance(item, str) and str(item).strip() in allowed
        ]
    elif not preserve_topic:
        inputs.setdefault("missing_fields", [])

    # 结构页需求提取
    spr = payload.get("structural_page_request")
    if isinstance(spr, str) and spr.strip().lower() in _VALID_STRUCTURAL_REQUESTS:
        inputs["structural_page_request"] = spr.strip().lower()
    else:
        inputs.setdefault("structural_page_request", "none")

    spc = payload.get("structural_page_count")
    if isinstance(spc, int) and spc > 0:
        inputs["structural_page_count"] = spc
    else:
        inputs.setdefault("structural_page_count", None)

    need_ask_style = payload.get("need_ask_style")
    if isinstance(need_ask_style, bool) and not inputs.get("pack_dir"):
        inputs["need_ask_style"] = need_ask_style
    elif "need_ask_style" not in inputs:
        inputs["need_ask_style"] = not bool(inputs.get("style_id"))


def _build_p21_slot_prompt(
    user_text: str,
    doc_excerpt: str,
    inputs: dict[str, Any],
    *,
    preserve_topic: bool,
) -> str:
    parts = ["请分析 PPT 需求槽位与缺失项。\n"]
    if preserve_topic:
        parts.append(
            f"已知主题（来自上游，勿修改）：{inputs.get('topic', '').strip()}\n"
            "missing_fields 不得包含 topic。\n"
        )
    if user_text:
        parts.append(f"用户消息：\n{user_text}\n")
    if doc_excerpt:
        parts.append(f"文档摘要（doc_raw）：\n{doc_excerpt}\n")
    if inputs.get("has_documents"):
        parts.append(f"has_documents: {bool(inputs.get('has_documents'))}\n")
    parts.append("按 JSON 返回全部槽位、missing_fields、need_ask_style。")
    return "\n".join(parts)


def _parse_slot_analysis_response(raw: str, *, preserve_topic: bool) -> dict[str, Any]:
    payload = _parse_json_payload(raw)
    if not isinstance(payload, dict):
        default_missing = list(_P21_GAP_FIELDS) if preserve_topic else list(_SLOT_FIELDS)
        return {
            "topic": "",
            "page_count": None,
            "audience": "",
            "presentation_purpose": "",
            "style_id": "",
            "style_description": "",
            "pack_dir": "",
            "structural_page_request": "none",
            "structural_page_count": None,
            "missing_fields": default_missing,
            "need_ask_style": True,
        }
    return payload


def _build_p24_prompt(inputs: dict[str, Any], user_text: str, doc_excerpt: str) -> str:
    parts = ["请根据以下已收集需求推断派生参数。\n"]
    parts.append(
        "已收集：\n"
        f"- topic: {inputs.get('topic', '')}\n"
        f"- page_count: {inputs.get('page_count')}\n"
        f"- audience: {inputs.get('audience', '')}\n"
        f"- presentation_purpose: {inputs.get('presentation_purpose', '')}\n"
        f"- style_id: {inputs.get('style_id', '')}\n"
        f"- has_documents: {bool(inputs.get('has_documents'))}\n"
        f"- doc_parse_ok: {bool(inputs.get('doc_parse_ok'))}\n"
        f"- image_paths: {bool(inputs.get('image_paths'))}\n"
    )
    if user_text:
        parts.append(f"用户原文：\n{user_text}\n")
    if doc_excerpt:
        parts.append(f"文档摘要：\n{doc_excerpt}\n")
    parts.append("按 JSON 返回 search_mode、source_type、research_depth、need_imagegen。")
    return "\n".join(parts)


async def _ask_missing_batch_fields(node: PlanNode, inputs: dict[str, Any]) -> None:
    missing_fields = _unsatisfied_batch_fields(inputs)
    if not missing_fields:
        return

    questions = _build_batch_questions(missing_fields)
    if not questions:
        raise RequirementCollectError("无法组装页数/受众/目的的询问题目")

    if not node.has_tool("ask_user"):
        raise RequirementCollectError("缺少 ask_user 工具，无法收集页数/受众/目的")

    result = await node.call_tool("ask_user", questions=questions)
    status, answers = _normalize_ask_result(result)

    if _is_auto_skip(status, answers):
        logger.info(
            "[P2.2] ask_user 自动应答（用户超时），LLM 兜底补默认值: %s",
            missing_fields,
        )
        await _llm_default_batch_fields(node, inputs, missing_fields)
        _prune_satisfied_batch_missing_fields(inputs)
        return

    if status != "answered" or not answers:
        detail = ""
        if isinstance(result, dict):
            detail = str(result.get("message") or "").strip()
        raise RequirementCollectError(
            "批量需求收集未完成（页数/受众/目的）"
            + (f": {detail}" if detail else f"（status={status}）")
        )

    _apply_ask_answers(inputs, answers, sent_questions=questions)
    _prune_satisfied_batch_missing_fields(inputs)

    # 部分字段在回填中仍空（如用户选了"其他"但未填文本）——继续 LLM 兜底
    still_missing = _unsatisfied_batch_fields(inputs)
    if still_missing:
        logger.info(
            "[P2.2] 用户作答后仍存在缺失字段，LLM 兜底补默认值: %s", still_missing,
        )
        await _llm_default_batch_fields(node, inputs, still_missing)
        _prune_satisfied_batch_missing_fields(inputs)


def _parse_derive_params_response(raw: str) -> dict[str, str]:
    payload = _parse_json_payload(raw)
    if not isinstance(payload, dict):
        raise RequirementCollectError("派生参数解析失败：LLM 未返回有效 JSON")

    search_mode = str(payload.get("search_mode") or "").strip()
    source_type = str(payload.get("source_type") or "").strip()
    research_depth = str(payload.get("research_depth") or "").strip().upper()

    if not search_mode:
        raise RequirementCollectError("派生参数不完整：缺少 search_mode")
    if search_mode not in _VALID_SEARCH_MODES:
        raise RequirementCollectError(f"派生参数无效：search_mode={search_mode!r}")

    if not source_type:
        raise RequirementCollectError("派生参数不完整：缺少 source_type")
    if source_type not in _VALID_SOURCE_TYPES:
        raise RequirementCollectError(f"派生参数无效：source_type={source_type!r}")

    if not research_depth:
        raise RequirementCollectError("派生参数不完整：缺少 research_depth")
    if research_depth not in _VALID_RESEARCH_DEPTHS:
        raise RequirementCollectError(f"派生参数无效：research_depth={research_depth!r}")

    need_imagegen = bool(payload.get("need_imagegen", False))

    return {
        "search_mode": search_mode,
        "source_type": source_type,
        "research_depth": research_depth,
        "need_imagegen": need_imagegen,
    }


async def _derive_params_via_llm(node: PlanNode, inputs: dict[str, Any]) -> dict[str, str]:
    user_text = _collect_user_text(inputs)
    doc_excerpt = await PptCommon.read_file(
        node,
        inputs.get("doc_raw_path"),
        max_chars=_DOC_EXCERPT_MAX_CHARS,
        error_type=RequirementCollectError,
    )

    response = await node.stream_llm_collect(
        _build_p24_prompt(inputs, user_text, doc_excerpt),
        system_prompt=_P24_SYSTEM_PROMPT,
    )
    if not isinstance(response, str) or not response.strip():
        raise RequirementCollectError("派生参数推断失败：LLM 返回为空")

    return _parse_derive_params_response(response)


def _field_from_header(header: str) -> str | None:
    mapping = {
        "页数": "page_count",
        "受众": "audience",
        "目的": "presentation_purpose",
        "主题": "topic",
        "风格": "style_id",
    }
    return mapping.get(header.strip())


def _field_for_answer_item(
    item: dict[str, Any],
    sent_questions: list[dict[str, Any]] | None,
) -> str | None:
    """按答案中的 question 文本与发出题目精确匹配，映射到槽位字段。"""
    answer_q = str(item.get("question") or "").strip()
    if answer_q and sent_questions:
        for sent in sent_questions:
            sent_q = str(sent.get("question") or "").strip()
            if sent_q and sent_q == answer_q:
                return _field_from_header(str(sent.get("header") or ""))
    return _field_from_header(str(item.get("header") or ""))


def _apply_answer_item(
    inputs: dict[str, Any],
    item: dict[str, Any],
    *,
    sent_questions: list[dict[str, Any]] | None = None,
) -> None:
    field = _field_for_answer_item(item, sent_questions)

    selected = item.get("selected_options")
    label = ""
    if isinstance(selected, list) and selected:
        label = str(selected[0]).strip()
    other_text = str(
        item.get("other_text")
        or item.get("custom_text")
        or item.get("custom_input")
        or ""
    ).strip()

    if field == "page_count":
        count = _page_count_from_label(label, other_text)
        if count is not None:
            inputs["page_count"] = count
    elif field == "audience":
        inputs["audience"] = _audience_from_label(label, other_text)
    elif field == "presentation_purpose":
        inputs["presentation_purpose"] = _purpose_from_label(label, other_text)
    elif field == "topic":
        if label.startswith("确认："):
            inputs["topic"] = label.removeprefix("确认：").strip()
        else:
            topic = other_text or label
            if topic and topic != "其他":
                inputs["topic"] = topic
    elif field == "style_id":
        style_id, description = _style_id_from_label(label if label != "其他" else other_text)
        if not style_id and other_text:
            style_id, description = _style_id_from_label(other_text)
        if style_id:
            inputs["style_id"] = style_id
        if description:
            inputs["style_description"] = description
            if style_id == "custom":
                inputs["additional_notes"] = description


def _apply_ask_answers(
    inputs: dict[str, Any],
    answers: list[Any],
    *,
    sent_questions: list[dict[str, Any]] | None = None,
) -> None:
    for item in answers:
        if isinstance(item, dict):
            _apply_answer_item(inputs, item, sent_questions=sent_questions)


def _build_batch_questions(missing_fields: list[str]) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []

    if "page_count" in missing_fields:
        questions.append(
            {
                "header": "页数",
                "question": "需要多少页内容页？（不含封面、结束页）",
                "multi_select": False,
                "options": [
                    {"label": "3-6 页（推荐）", "description": "适合简短汇报、产品介绍"},
                    {"label": "8-12 页", "description": "适合详细分析、项目方案"},
                    {"label": "15-20 页", "description": "适合深度报告、培训材料"},
                ],
            }
        )

    if "audience" in missing_fields:
        questions.append(
            {
                "header": "受众",
                "question": "目标受众是谁？",
                "multi_select": False,
                "options": [
                    {"label": "企业高管", "description": "强调结论先行、数据驱动"},
                    {"label": "技术团队", "description": "可包含技术细节与架构"},
                    {"label": "投资人/客户", "description": "强调商业价值与 ROI"},
                    {"label": "普通大众", "description": "简洁易懂、避免术语"},
                ],
            }
        )

    if "presentation_purpose" in missing_fields:
        questions.append(
            {
                "header": "目的",
                "question": "这次演示的主要目的是？",
                "multi_select": False,
                "options": [
                    {"label": "工作汇报", "description": "汇报进展、成果、总结"},
                    {"label": "产品/方案展示", "description": "产品发布、方案推介"},
                    {"label": "教学/分享", "description": "培训教程、知识分享"},
                    {"label": "AI 自动判断", "description": "根据主题自动选择目的"},
                ],
            }
        )

    return questions[:4]


def _build_style_question() -> dict[str, Any]:
    return {
        "header": "风格",
        "question": "请选择演示文稿的视觉风格",
        "multi_select": False,
        "options": [
            {"label": "商务经典", "description": "企业汇报、红色主题、严谨专业"},
            {"label": "科技极简", "description": "产品发布、黑白调性、极简设计"},
            {"label": "典雅叙事", "description": "文化主题、温暖质感、有机插图"},
            {"label": "工业科技", "description": "硬核场景、高对比度、工业科技感"},
            {"label": "自由发挥", "description": "由 AI 根据主题自动设计"},
        ],
    }


def _style_id_resolved(inputs: dict[str, Any]) -> str:
    return _resolve_style_id(inputs.get("style_id"), inputs.get("style_description"))


def _style_needs_user_ask(inputs: dict[str, Any]) -> bool:
    """style_id 仍缺失时，判断是否需 ask（调用方应已确认 style 未 resolved）。"""
    if bool(inputs.get("need_ask_style")):
        return True
    return "style_id" in (inputs.get("missing_fields") or [])


def _finalize_style_slot(inputs: dict[str, Any], *, fallback: str | None = None) -> None:
    style_id = _style_id_resolved(inputs) or (fallback or "")
    if not style_id:
        raise RequirementCollectError("风格收集未完成：缺少 style_id")
    inputs["style_id"] = style_id
    inputs["need_ask_style"] = False
    inputs["missing_fields"] = [
        field for field in (inputs.get("missing_fields") or [])
        if field != "style_id"
    ]


async def _ask_missing_style(node: PlanNode, inputs: dict[str, Any]) -> None:
    if not node.has_tool("ask_user"):
        raise RequirementCollectError("缺少 ask_user 工具，无法收集风格")

    style_question = _build_style_question()
    result = await node.call_tool(
        "ask_user",
        questions=[style_question],
    )
    status, answers = _normalize_ask_result(result)

    if _is_auto_skip(status, answers):
        fallback_style = await _llm_default_style(node, inputs)
        logger.info("[P2.3] ask_user 自动应答（用户超时），style_id 兜底为 %s", fallback_style)
        inputs["style_id"] = fallback_style
        inputs["need_ask_style"] = False
        return

    if status != "answered" or not answers:
        detail = ""
        if isinstance(result, dict):
            detail = str(result.get("message") or "").strip()
        raise RequirementCollectError(
            "风格收集未完成"
            + (f": {detail}" if detail else f"（status={status}）")
        )

    _apply_ask_answers(inputs, answers, sent_questions=[style_question])

    # 用户作答后仍未拿到有效 style_id（例如选了"其他"未填文本）——LLM 兜底
    if not _style_id_resolved(inputs):
        fallback_style = await _llm_default_style(node, inputs)
        logger.info("[P2.3] 用户作答后仍缺 style_id，LLM 兜底为 %s", fallback_style)
        inputs["style_id"] = fallback_style
        inputs["need_ask_style"] = False


def _normalize_ask_result(result: Any) -> tuple[str, list[Any]]:
    if not isinstance(result, dict):
        return "error", []
    status = str(result.get("status") or "error")
    answers = result.get("answers")
    if not isinstance(answers, list):
        answers = []
    return status, answers


def _answer_item_is_empty(item: Any) -> bool:
    """An answer item is 'empty' when both selected_options and free text are blank.

    Relay-Claw 在前端卡片倒计时（120s）到期时会回填空 selected_options，
    用此函数把这种"自动应答"识别为需要走兜底逻辑。"""
    if not isinstance(item, dict):
        return True
    selected = item.get("selected_options")
    has_selected = isinstance(selected, list) and any(
        isinstance(s, str) and s.strip() for s in selected
    )
    if has_selected:
        return False
    for key in ("other_text", "custom_text", "custom_input", "edited_text", "text", "free_text"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return False
    return True


def _is_auto_skip(status: str, answers: list[Any]) -> bool:
    """Detect when the user did not provide a usable answer.

    Two shapes both count as "auto-skip" -> LLM fallback fills defaults:
    1. Relay-Claw 120s timeout auto-answer: ``status=="answered"`` with a
       non-empty answers list whose items are all empty (blank
       selected_options + no free text).
    2. Pure skip / empty answers: ``status=="answered"`` with an empty (or
       None-coerced) answers list. The previous ``not answers`` short-circuit
       wrongly classified this as a RequirementCollectError, which triggered
       fallback subagent re-asks of ask_user in a loop (the LLM saw an empty
       answers envelope and decided to ask again). Treating empty answers as
       auto-skip lets the existing ``_llm_default_*`` fallback infer the field
       from context, breaking the re-ask loop.
    """
    if status != "answered":
        return False
    if not answers:
        return True
    return all(_answer_item_is_empty(item) for item in answers)


_BATCH_FALLBACK_SYSTEM_PROMPT = """你是 PPT 需求兜底分析助手。当用户未在限时内作答时，请基于用户原始消息、文档摘要与候选选项，为每个缺失字段挑选最合理的默认值。

字段取值范围：
- page_count: 整数（内容页数，不含封面/结束页；候选 6 / 10 / 18 中三选一；若用户上下文暗示更具体，可输出 1~30 的整数）
- audience: 字符串（优先从候选标签中选；无明显倾向时填 "通用商务/知识分享"）
- presentation_purpose: 字符串（候选「工作汇报」「产品展示」「教学分享」「auto」中四选一）

规则：
1. 仅为 missing 列表中出现的字段输出值；其他字段省略。
2. 仅基于已有信息推断，不要编造与上下文无关的内容。
3. 必须只输出 JSON，且字段名严格匹配 missing 列表。

示例：
{"page_count": 10, "audience": "技术团队", "presentation_purpose": "工作汇报"}"""


_TOPIC_FALLBACK_SYSTEM_PROMPT = """你是 PPT 主题兜底选择助手。用户未在限时内从候选主题中作答，请基于用户消息与文档摘要，从给定候选中挑选最契合的一项。

规则：
1. 必须从候选列表中选择一项原文，不要改写或新建。
2. 必须只输出 JSON：{"topic": "候选原文"}。"""


_STYLE_FALLBACK_SYSTEM_PROMPT = """你是 PPT 风格兜底选择助手。用户未在限时内作答风格选择，请基于用户消息与主题，从给定 style_id 中挑选最合适的一项。

style_id 候选：business-classic / tech-minimal / elegant-narrative / industrial-tech / custom
含义：
- business-classic: 企业汇报、红色主题、严谨专业
- tech-minimal: 产品发布、黑白调性、极简设计
- elegant-narrative: 文化主题、温暖质感
- industrial-tech: 硬核科技、高对比度
- custom: 由 AI 根据主题自动设计

规则：
1. style_id 必须取上述五者之一。
2. 必须只输出 JSON：{"style_id": "<id>"}。"""


def _build_batch_fallback_prompt(
    missing_fields: list[str],
    user_text: str,
    doc_excerpt: str,
) -> str:
    parts = ["请为下列缺失字段挑选最合理的默认值（用户已超时未作答）。\n"]
    parts.append(f"missing: {json.dumps(missing_fields, ensure_ascii=False)}\n")
    if user_text:
        parts.append(f"用户消息：\n{user_text}\n")
    if doc_excerpt:
        parts.append(f"文档摘要：\n{doc_excerpt}\n")
    parts.append("仅输出 JSON。")
    return "\n".join(parts)


def _build_topic_fallback_prompt(
    topic_options: list[str],
    user_text: str,
    doc_excerpt: str,
) -> str:
    parts = ["请从下列候选主题中选出与用户上下文最契合的一项（用户已超时未作答）。\n"]
    parts.append(f"候选：{json.dumps(topic_options, ensure_ascii=False)}\n")
    if user_text:
        parts.append(f"用户消息：\n{user_text}\n")
    if doc_excerpt:
        parts.append(f"文档摘要：\n{doc_excerpt}\n")
    parts.append('仅输出 JSON：{"topic":"候选原文"}。')
    return "\n".join(parts)


def _build_style_fallback_prompt(inputs: dict[str, Any], user_text: str) -> str:
    parts = ["请为本次 PPT 选择最合适的 style_id（用户已超时未作答）。\n"]
    topic = str(inputs.get("topic") or "").strip()
    if topic:
        parts.append(f"主题：{topic}\n")
    audience = str(inputs.get("audience") or "").strip()
    if audience:
        parts.append(f"受众：{audience}\n")
    purpose = str(inputs.get("presentation_purpose") or "").strip()
    if purpose:
        parts.append(f"目的：{purpose}\n")
    if user_text:
        parts.append(f"用户消息：\n{user_text}\n")
    parts.append('仅输出 JSON：{"style_id":"<id>"}。')
    return "\n".join(parts)


async def _llm_default_batch_fields(
    node: PlanNode,
    inputs: dict[str, Any],
    missing_fields: list[str],
) -> None:
    """超时兜底：LLM 推断缺失 batch 字段；最终仍为空时落到模块级 default。"""
    user_text = _collect_user_text(inputs)
    doc_excerpt = await PptCommon.read_file(
        node,
        inputs.get("doc_raw_path"),
        max_chars=_DOC_EXCERPT_MAX_CHARS,
        error_type=RequirementCollectError,
    )
    payload: dict[str, Any] = {}
    try:
        response = await node.stream_llm_collect(
            _build_batch_fallback_prompt(missing_fields, user_text, doc_excerpt),
            system_prompt=_BATCH_FALLBACK_SYSTEM_PROMPT,
        )
        parsed = _parse_json_payload(response)
        if isinstance(parsed, dict):
            payload = parsed
    except Exception as exc:
        if isinstance(exc, AbortError):
            raise
        logger.warning("[P2.2] LLM 兜底解析失败，将使用静态默认值: %s", exc)

    if "page_count" in missing_fields:
        count = _normalize_page_count(payload.get("page_count"))
        inputs["page_count"] = count if count is not None else _DEFAULT_PAGE_COUNT
    if "audience" in missing_fields:
        audience = payload.get("audience")
        inputs["audience"] = (
            audience.strip() if isinstance(audience, str) and audience.strip()
            else _DEFAULT_AUDIENCE
        )
    if "presentation_purpose" in missing_fields:
        purpose_raw = payload.get("presentation_purpose")
        purpose = purpose_raw.strip() if isinstance(purpose_raw, str) else ""
        if purpose in _PURPOSE_LABEL_TO_VALUE:
            purpose = _PURPOSE_LABEL_TO_VALUE[purpose]
        inputs["presentation_purpose"] = purpose or _DEFAULT_PRESENTATION_PURPOSE


async def _llm_default_topic(
    node: PlanNode,
    inputs: dict[str, Any],
    topic_options: list[str],
) -> str:
    """超时兜底：LLM 从候选中挑选最契合的主题；失败时取第一项。"""
    user_text = _collect_user_text(inputs)
    doc_excerpt = await PptCommon.read_file(
        node,
        inputs.get("doc_raw_path"),
        max_chars=_DOC_EXCERPT_MAX_CHARS,
        error_type=RequirementCollectError,
    )
    try:
        response = await node.stream_llm_collect(
            _build_topic_fallback_prompt(topic_options, user_text, doc_excerpt),
            system_prompt=_TOPIC_FALLBACK_SYSTEM_PROMPT,
        )
        payload = _parse_json_payload(response)
        if isinstance(payload, dict):
            chosen = payload.get("topic")
            if isinstance(chosen, str) and chosen.strip():
                chosen_str = chosen.strip()
                for option in topic_options:
                    if option.strip() == chosen_str:
                        return option
                # LLM 改写过则退回到与候选近似的项
                for option in topic_options:
                    if chosen_str in option or option in chosen_str:
                        return option
    except Exception as exc:
        if isinstance(exc, AbortError):
            raise
        logger.warning("[P2.1] LLM 主题兜底解析失败，将取首个候选: %s", exc)
    return topic_options[0]


async def _llm_default_style(node: PlanNode, inputs: dict[str, Any]) -> str:
    """超时兜底：LLM 从五个有效 style_id 中挑选；失败时返回 'business-classic'。"""
    user_text = _collect_user_text(inputs)
    try:
        response = await node.stream_llm_collect(
            _build_style_fallback_prompt(inputs, user_text),
            system_prompt=_STYLE_FALLBACK_SYSTEM_PROMPT,
        )
        payload = _parse_json_payload(response)
        if isinstance(payload, dict):
            normalized = _resolve_style_id(payload.get("style_id"), payload.get("style_description"))
            if normalized:
                return normalized
    except Exception as exc:
        if isinstance(exc, AbortError):
            raise
        logger.warning("[P2.3] LLM 风格兜底解析失败，将使用 'business-classic': %s", exc)
    return "business-classic"


def _build_topic_suggest_prompt(inputs: dict[str, Any], doc_excerpt: str) -> str:
    parts = [f"请生成 {_TOPIC_SUGGEST_COUNT} 个可独立制作 PPT 的演示主题候选。\n"]
    user_text = _collect_user_text(inputs)
    if user_text:
        parts.append(f"用户消息：\n{user_text}\n")
    if doc_excerpt:
        parts.append(f"文档摘要：\n{doc_excerpt}\n")
    parts.append(
        f'按 JSON 返回 {{"topics":["..."]}}，topics 数组长度必须为 {_TOPIC_SUGGEST_COUNT}。'
    )
    return "\n".join(parts)


def _parse_topic_suggestions(raw: str) -> list[str]:
    payload = _parse_json_payload(raw)
    if not isinstance(payload, dict):
        return []
    topics_raw = payload.get("topics")
    if not isinstance(topics_raw, list):
        return []

    seen: set[str] = set()
    topics: list[str] = []
    for item in topics_raw:
        text = str(item).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        topics.append(text)
    return topics


def _build_topic_ask_question(topics: list[str]) -> dict[str, Any]:
    if len(topics) < 2:
        raise RequirementCollectError("主题候选不足，无法发起用户选择")
    option_topics = topics[:_TOPIC_SUGGEST_COUNT]
    return {
        "header": "主题",
        "question": "请选择本次演示的主题方向（每个选项均可直接作为完整 PPT 主题）：",
        "multi_select": False,
        "options": [{"label": topic} for topic in option_topics],
    }


def _topic_text_from_ask_answers(answers: list[Any]) -> str:
    for item in answers:
        if not isinstance(item, dict):
            continue
        other_text = str(
            item.get("other_text")
            or item.get("custom_text")
            or item.get("custom_input")
            or ""
        ).strip()
        if other_text:
            return other_text
        selected = item.get("selected_options")
        if not isinstance(selected, list) or not selected:
            continue
        label = str(selected[0]).strip()
        if label and label != "其他":
            return label
    return ""


def _append_topic_supplement(inputs: dict[str, Any], reply_text: str) -> None:
    text = reply_text.strip()
    if not text:
        return
    inputs["topic_user_reply"] = text
    supplement = f"[用户补充主题]: {text}"
    for key in _TEXT_SOURCE_KEYS:
        existing = inputs.get(key)
        if isinstance(existing, str) and existing.strip():
            inputs[key] = f"{existing.strip()}\n{supplement}"
            return
    inputs["user_message"] = supplement


async def _generate_topic_suggestions(
    node: PlanNode,
    inputs: dict[str, Any],
    doc_excerpt: str,
) -> list[str]:
    response = await node.stream_llm_collect(
        _build_topic_suggest_prompt(inputs, doc_excerpt),
        system_prompt=_P21_TOPIC_SUGGEST_SYSTEM_PROMPT,
    )
    topics = _parse_topic_suggestions(response)
    if len(topics) < _TOPIC_SUGGEST_COUNT:
        raise RequirementCollectError(
            f"未能生成 {_TOPIC_SUGGEST_COUNT} 个有效主题候选（实际 {len(topics)} 个）"
        )
    return topics[:_TOPIC_SUGGEST_COUNT]


async def _resolve_topic_via_ask(node: PlanNode, inputs: dict[str, Any]) -> None:
    if not node.has_tool("ask_user"):
        raise RequirementCollectError("缺少 ask_user 工具，无法收集演示主题")

    doc_excerpt = await PptCommon.read_file(
        node,
        inputs.get("doc_raw_path"),
        max_chars=_DOC_EXCERPT_MAX_CHARS,
        error_type=RequirementCollectError,
    )
    topic_options = await _generate_topic_suggestions(node, inputs, doc_excerpt)
    topic_question = _build_topic_ask_question(topic_options)

    ask_result = await node.call_tool(
        "ask_user",
        questions=[topic_question],
    )
    status, answers = _normalize_ask_result(ask_result)

    if _is_auto_skip(status, answers):
        selected_topic = await _llm_default_topic(node, inputs, topic_options)
        logger.info("[P2.1] ask_user 自动应答（用户超时），topic 兜底为 %r", selected_topic)
    else:
        if status != "answered":
            detail = ""
            if isinstance(ask_result, dict):
                detail = str(ask_result.get("message") or "").strip()
            raise RequirementCollectError(
                f"未能获取用户主题选择（status={status}）" + (f": {detail}" if detail else "")
            )

        selected_topic = _topic_text_from_ask_answers(answers)
        if not selected_topic:
            # 用户在 120s 内点击但未选择有效项 → LLM 兜底从候选中挑选
            selected_topic = await _llm_default_topic(node, inputs, topic_options)
            logger.info("[P2.1] 用户作答未给出有效主题，LLM 兜底为 %r", selected_topic)

    inputs["topic"] = selected_topic
    inputs["topic_user_reply"] = selected_topic
    inputs["missing_fields"] = [
        field for field in (inputs.get("missing_fields") or []) if field != "topic"
    ]
    _append_topic_supplement(inputs, selected_topic)


class P21SlotExtractNode(PlanNode):
    """P2.1 — LLM 槽位分析；topic 缺失时 LLM 生成 4 个主题候选并 ask 用户选择。"""

    def __init__(self) -> None:
        super().__init__(
            plan_name="p2_1_slot_extract",
            instruction=(
                "## P2.1 槽位识别与主题确认\n"
                "\n"
                "### 节点职责\n"
                "LLM 提取槽位与 missing_fields；topic 缺失时生成候选并 ask 用户选择。\n"
                "\n"
                "### 前置条件\n"
                "- `stream_llm` / `ask_user` 工具可用\n"
                "- `doc_raw_path`（可选）: 有文档时读取摘要辅助 LLM\n"
                "\n"
                "### 输入\n"
                "- `task` | `user_request` | `user_message` | `query`（可选）: 用户原文\n"
                "- `topic`（可选）: 上游已填主题（非空时保留，不覆盖）\n"
                "- `doc_raw_path`（可选）: 文档摘要路径\n"
                "\n"
                "### 输出\n"
                "- 各槽位字段: `topic`, `page_count`, `audience`, `presentation_purpose`, `style_id` 等\n"
                "- `missing_fields`: list[str] — 仍缺失的槽位名称\n"
                "- `topic`: str — 若初始缺失则通过 ask 用户选择获得（非空，否则 raise）\n"
                "- `need_ask_style`: bool — 是否需要 P2.3 询问 style\n"
                "- `requirement_collect_status`: str = 'slots_analyzed'\n"
                "\n"
                "### 执行流程\n"
                "1. 构造 LLM prompt（含用户文本 + 文档摘要），提取槽位与 missing_fields\n"
                "2. topic 仍缺失时：call_llm 生成 4 个主题候选 → ask_user 供用户选择\n"
                "3. 用户所选 label 直接写入 topic，不再二次提炼\n"
                "\n"
                "### 失败兜底\n"
                "- LLM 无有效输出: raise RequirementCollectError\n"
                "- 用户未选择主题: raise RequirementCollectError\n"
            ),
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        user_text = _collect_user_text(inputs)
        doc_excerpt = await PptCommon.read_file(
            self,
            inputs.get("doc_raw_path"),
            max_chars=_DOC_EXCERPT_MAX_CHARS,
            error_type=RequirementCollectError,
        )
        preserve_topic = _has_nonempty_topic(inputs)

        response = await self.stream_llm_collect(
            _build_p21_slot_prompt(user_text, doc_excerpt, inputs, preserve_topic=preserve_topic),
            system_prompt=_P21_SLOT_SYSTEM_PROMPT,
        )
        payload = _parse_slot_analysis_response(response, preserve_topic=preserve_topic)
        _merge_slot_payload(inputs, payload, preserve_topic=preserve_topic)

        if not _has_nonempty_topic(inputs):
            await _resolve_topic_via_ask(self, inputs)

        inputs["requirement_collect_status"] = "slots_analyzed"
        return inputs


class P22AskBatchNode(PlanNode):
    """P2.2 — 收集 page_count / audience / presentation_purpose，缺一不可。"""

    def __init__(self) -> None:
        super().__init__(
            plan_name="p2_2_ask_batch",
            instruction=(
                "## P2.2 批量字段收集\n"
                "\n"
                "### 节点职责\n"
                "确保 page_count / audience / presentation_purpose 三项均已收集，缺一不可。\n"
                "\n"
                "### 前置条件\n"
                "- P2.1 已完成，topic 已填\n"
                "- `ask_user` 工具可用\n"
                "\n"
                "### 输入\n"
                "- `page_count`（可选）: 已有页数\n"
                "- `audience`（可选）: 已有受众\n"
                "- `presentation_purpose`（可选）: 已有演示目的\n"
                "\n"
                "### 输出\n"
                "- `page_count`: int/str — 页数（非空，缺失时 raise）\n"
                "- `audience`: str — 受众（非空，缺失时 raise）\n"
                "- `presentation_purpose`: str — 演示目的（非空，缺失时 raise）\n"
                "\n"
                "### 执行流程\n"
                "1. 检查三项是否齐备\n"
                "2. 缺失字段合并为 ask_user 一次性询问\n"
                "3. 再次校验，任一仍缺失则 raise\n"
                "\n"
                "### 失败兜底\n"
                "- 用户未提供有效回复: raise RequirementCollectError\n"
                "- 三项缺一不可，不填默认值\n"
            ),
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        await _ask_missing_batch_fields(self, inputs)
        _require_batch_fields_collected(inputs)
        return inputs


class P23AskStyleNode(PlanNode):
    """P2.3 — 收集 style_id。

    进入本节点时，topic 及 page_count / audience / presentation_purpose
    应已由 P2.1、P2.2 填齐；本节点只负责 style_id（custom 时含 style_description）。
    """

    def __init__(self) -> None:
        super().__init__(
            plan_name="p2_3_ask_style",
            instruction=(
                "## P2.3 风格收集\n"
                "\n"
                "### 节点职责\n"
                "确保 style_id 已收集；P2.1 标记 need_ask_style 或 style_id 缺失时 ask 用户，否则隐式 custom。\n"
                "\n"
                "### 前置条件\n"
                "- P2.1 / P2.2 已完成，topic / batch 字段已齐备\n"
                "- `ask_user` 工具可用\n"
                "\n"
                "### 输入\n"
                "- `style_id`（可选）: 已有风格标识\n"
                "- `need_ask_style`（可选）: P2.1 标记是否需要 ask\n"
                "\n"
                "### 输出\n"
                "- `style_id`:str - 风格标识（business-classic/tech-minimal/elegant-narrative/industrial-tech/custom）\n"
                "- `style_description`: str — custom 模式下用户自描述（其他模式为空）\n"
                "- `additional_notes`: str — 补充说明（通常为空，custom 时可能有值）\n"
                "\n"
                "### 执行流程\n"
                "1. 检查 need_ask_style 标记与 style_id 是否缺失\n"
                "2. 需要询问时: ask_user 提供风格选项\n"
                "3. 不需要询问时: 隐式设 style_id='custom'\n"
                "4. finalize style_slot（补 style_description / additional_notes）\n"
                "\n"
                "### 失败兜底\n"
                "- 用户未回复风格选择: 隐式 custom\n"
            ),
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        use_implicit_custom = False
        if not _style_id_resolved(inputs):
            if _style_needs_user_ask(inputs):
                await _ask_missing_style(self, inputs)
            else:
                use_implicit_custom = True

        _finalize_style_slot(inputs, fallback="custom" if use_implicit_custom else None)
        return inputs


class P24DeriveParamsNode(PlanNode):
    """P2.4 — LLM 推断 search_mode、source_type、research_depth。

    进入本节点时，P2.1–P2.3 应已填齐 topic / 批量槽位 / style_id；
    本节点只负责三项派生参数，解析或校验失败即报错，不使用默认值。
    """

    def __init__(self) -> None:
        super().__init__(
            plan_name="p2_4_derive_params",
            instruction=(
                "## P2.4 派生参数推断\n"
                "\n"
                "### 节点职责\n"
                "LLM 推断 search_mode / source_type / research_depth 三项派生参数。\n"
                "\n"
                "### 前置条件\n"
                "- P2.1–P2.3 已完成，topic / batch / style_id 已齐备\n"
                "- `stream_llm` 工具可用\n"
                "\n"
                "### 输入\n"
                "- `topic`（必填）: 演示主题\n"
                "- `page_count`, `audience`, `presentation_purpose`（可选）: 辅助推断\n"
                "- `has_documents`, `doc_parse_ok`（可选）: 影响 source_type 判断\n"
                "\n"
                "### 输出\n"
                "- `search_mode`: str — 搜索策略，取值范围 {no_search, auto, force_search}\n"
                "- `source_type`: str — 素材来源，取值范围 {topic, outline, description}\n"
                "- `research_depth`: str — 研究深度，取值范围 {L1, L2, L3}\n"
                "\n"
                "### 执行流程\n"
                "1. 构造 LLM prompt（含全部已知槽位与文档信息）\n"
                "2. call_llm 推断三项派生参数\n"
                "3. 校验三项均在枚举值域内\n"
                "\n"
                "### 失败兜底\n"
                "- LLM 无有效输出: raise RequirementCollectError\n"
                "- 任一字段不在枚举内: raise RequirementCollectError\n"
                "- 不使用默认值\n"
            ),
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        derived = await _derive_params_via_llm(self, inputs)
        inputs.update(derived)

        # 写 imagegen_status.json（供 P6.5 读取）
        need_imagegen = derived.get("need_imagegen", False)
        output_dir = str(inputs.get("output_dir", "")).strip()
        if need_imagegen and output_dir:
            try:
                content = json.dumps(
                    {"supported": True},
                    ensure_ascii=False,
                )
                await PptCommon.write_file(
                    self, f"{output_dir}/imagegen_status.json",
                    content, label="imagegen_status",
                    error_type=RequirementCollectError,
                )
                logger.info("[P2.4] imagegen_status.json 已写入 (supported=true)")
            except Exception as e:
                if isinstance(e, AbortError):
                    raise
                logger.warning("[P2.4] 写 imagegen_status.json 失败: %s", e)
        elif not need_imagegen and output_dir:
            try:
                content = json.dumps(
                    {"supported": False},
                    ensure_ascii=False,
                )
                await PptCommon.write_file(
                    self, f"{output_dir}/imagegen_status.json",
                    content, label="imagegen_status",
                    error_type=RequirementCollectError,
                )
                logger.info("[P2.4] imagegen_status.json 已写入 (supported=false)")
            except Exception as e:
                if isinstance(e, AbortError):
                    raise
                logger.warning("[P2.4] 写 imagegen_status.json 失败: %s", e)

        return inputs


class RequirementCollectNode(PlanNode):
    """P2 — 需求收集（P2.1 → P2.2 → P2.3 → P2.4）。

    预期输入（ctx / inputs）:
        可选: task | user_request | user_message | query — 用户原文（含 officeclaw JSON 包装）
        可选: topic — 上游 P3 推断或用户已给主题
        可选: topic_inferred, doc_raw_path, has_documents, doc_parse_ok
        可选: slots_from_query — P1 在无附件且无路径时预提取的槽位信息
        可选: slots_from_query_complete — P1 标记预提取槽位是否全部非空

    预期输出（成功时写入同一 ctx，下列字段必须齐备）:
        topic, page_count, audience, presentation_purpose, style_id
        style_description, additional_notes（style_id=custom 时）
        search_mode, source_type, research_depth
        requirement_collect_status（P2.1 写入，通常为 slots_analyzed）

    过程字段（成功收尾后通常已清理）:
        missing_fields, need_ask_style

    子步骤保证:
        P2.1 — 槽位识别；topic 缺失时 ask + 二次 LLM 提炼
        P2.2 — page_count / audience / presentation_purpose 缺一不可
        P2.3 — 仅收集 style_id（P2.1 判定无需 ask 时可隐式 custom）
        P2.4 — LLM 推断三项派生参数，解析/校验失败即报错

    快捷路径:
        无附件且 P1 已预提取全部槽位（slots_from_query_complete=True）时，
        直接填入预提取值，跳过 P2.1–P2.3，仅执行 P2.4。

    失败时 raise RequirementCollectError，不静默填 batch 槽位或派生参数默认值。
    """

    def __init__(self) -> None:
        super().__init__(
            plan_name="p2_requirement_collect",
            instruction=(
                "## P2 需求收集\n"
                "\n"
                "### 节点职责\n"
                "1. 槽位识别与缺失字段补全（topic / page_count / audience / presentation_purpose / style_id）\n"
                "2. 派生参数推断（search_mode / source_type / research_depth）\n"
                "3. 快捷路径：无附件且 P1 已预提取全部槽位时可跳过 P2.1–P2.3，仅执行 P2.4\n"
                "\n"
                "### 前置条件\n"
                "- P0 已完成，P1 已产出 `has_documents` 与 `doc_paths`\n"
                "- `stream_llm` / `ask_user` 工具可用\n"
                "\n"
                "### 输入\n"
                "- `task` | `user_request` | `user_message` | `query`（可选）: 用户原文\n"
                "- `topic`（可选）: 上游 P3 推断或用户已给主题\n"
                "- `topic_inferred`, `doc_raw_path`, `has_documents`, `doc_parse_ok`（可选）\n"
                "- `slots_from_query` / `slots_from_query_complete`（可选）: P1 预提取槽位\n"
                "\n"
                "### 输出\n"
                "- `topic`: str — 演示主题（非空，缺失时 raise RequirementCollectError）\n"
                "- `page_count`: int/str — 页数\n"
                "- `audience`: str — 受众\n"
                "- `presentation_purpose`: str — 演示目的\n"
                "- `style_id`:str — 风格标识（business-classic/tech-minimal/elegant-narrative/industrial-tech/custom）\n"
                "- `style_description`: str — custom 模式下用户自描述；其他模式通常为空\n"
                "- `additional_notes`: str — 补充说明（style_id=custom 时可能有值）\n"
                "- `search_mode`: str — 搜索策略（no_search / auto / force_search）\n"
                "- `source_type`: str — 素材来源（topic / outline / description）\n"
                "- `research_depth`: str — 研究深度（L1 / L2 / L3）\n"
                "- `requirement_collect_status`: str — 通常为 'slots_analyzed'\n"
                "\n"
                "### 执行流程\n"
                "1. 快捷路径判定：slots_from_query_complete=True → 直接填入预提取值，跳过 P2.1–P2.3\n"
                "2. P2.1: LLM 槽位识别 + missing_fields 标记；topic 缺失时 ask 用户选择\n"
                "3. P2.2: 确保 page_count / audience / presentation_purpose 三项齐备\n"
                "4. P2.3: 确保 style_id 已收集（缺省隐式 custom）\n"
                "5. P2.4: LLM 推断 search_mode / source_type / research_depth\n"
                "\n"
                "### 失败兜底\n"
                "- topic 缺失且用户未选择: raise RequirementCollectError\n"
                "- batch 字段缺一不可: 任一仍缺失则 raise RequirementCollectError\n"
                "- 派生参数不在枚举内: raise RequirementCollectError\n"
                "- 不静默填默认值，缺失即报错\n"
            ),
            sub_plans=[
                P21SlotExtractNode(),
                P22AskBatchNode(),
                P23AskStyleNode(),
                P24DeriveParamsNode(),
            ],
        )

    def _ensure_image_vars(self, ctx: dict[str, Any]) -> None:
        """图片变量兜底：image_paths 空数组 + image_sources 默认 local。

        ai 源由 P6.5 读取 imagegen_status.json 动态启用，不在此处判断。
        """
        ctx.setdefault("image_paths", [])
        ctx.setdefault("image_sources", ["local"])

    def _set_style_mode(self, ctx: dict[str, Any]) -> None:
        """根据 style_id / pack_dir 设置 style_mode（供下游 P3.5/P7/P8/P9 分支判断）。"""
        existing_mode = str(ctx.get("style_mode") or "").strip()
        if existing_mode:
            if existing_mode == "free":
                ctx["style_id"] = "custom"
                ctx["style_mode"] = "custom"
            return  # 已显式设置，不覆盖；仅归一化历史 free 状态
        pack_dir = str(ctx.get("pack_dir") or "").strip()
        if pack_dir:
            # prod 版模板包用 template-spec.json，不再依赖 template-manifest.json
            spec_path = Path(pack_dir) / "template-spec.json"
            if not spec_path.is_file():
                logger.warning(
                    "[P2] 模板包不完整（缺少 template-spec.json），降级为 custom 模式: %s",
                    pack_dir,
                )
                ctx["style_mode"] = "custom"
                ctx["style_id"] = "custom"
                ctx["template_pack_degraded"] = True
                return
            ctx["style_mode"] = "template_canvas"
            return
        style_id = str(ctx.get("style_id") or "").strip()
        if style_id in _VALID_STYLE_IDS - {"custom"}:
            ctx["style_mode"] = "preset"
        else:
            if style_id and style_id not in {"custom", "free"} and not ctx.get("style_description"):
                ctx["style_description"] = style_id
            ctx["style_id"] = "custom"
            ctx["style_mode"] = "custom"

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        ctx = inputs

        # 快捷路径：无附件且 P1 已从 query 预提取全部槽位
        pre_slots = ctx.get("slots_from_query", {})
        all_filled = ctx.get("slots_from_query_complete", False)
        if not ctx.get("has_documents") and all_filled and pre_slots:
            for slot in ("topic", "page_count", "audience", "presentation_purpose", "style_id", "pack_dir"):
                v = pre_slots.get(slot)
                if slot == "page_count" and v is not None:
                    ctx[slot] = v
                elif slot == "style_id" and isinstance(v, str) and v.strip():
                    # 归一化 style_id（如"华为风格"->"business-classic"），支持 style_description 回退
                    normalized = _resolve_style_id(v, pre_slots.get("style_description"))
                    ctx[slot] = normalized if normalized else v.strip()
                elif isinstance(v, str) and v.strip():
                    ctx[slot] = v
            # 结构页需求透传
            _spr = pre_slots.get("structural_page_request")
            if (
                isinstance(_spr, str)
                and _spr.strip().lower() in _VALID_STRUCTURAL_REQUESTS
            ):
                ctx["structural_page_request"] = _spr.strip().lower()
            else:
                ctx.setdefault("structural_page_request", "none")
            _spc = pre_slots.get("structural_page_count")
            if isinstance(_spc, int) and _spc > 0:
                ctx["structural_page_count"] = _spc
            else:
                ctx.setdefault("structural_page_count", None)
            await self.skip_subplan(self.sub_plans[0], ctx, message="slots pre-filled from query")
            await self.skip_subplan(self.sub_plans[1], ctx, message="slots pre-filled from query")
            await self.skip_subplan(self.sub_plans[2], ctx, message="slots pre-filled from query")
            await self.execute_subplan(self.sub_plans[3], ctx)  # P2.4 必跑

            if not _has_nonempty_topic(ctx):
                raise RequirementCollectError("缺少演示主题 topic，无法继续 PPT 流水线")
            self._set_style_mode(ctx)
            # 图片变量兜底（供 P6.5 Diana 消费）
            self._ensure_image_vars(ctx)
            # 写入 __artifact__，供跨请求续跑复用需求上下文
            _set_requirement_artifact(ctx)
            return ctx

        # 部分预填：把 P1 提取的已知槽位填入 ctx，让 P2.1 减少工作量
        if pre_slots and not ctx.get("has_documents"):
            for slot, value in pre_slots.items():
                if slot == "page_count" and value is not None and ctx.get("page_count") is None:
                    ctx[slot] = value
                elif isinstance(value, str) and value.strip() and not ctx.get(slot):
                    ctx[slot] = value
            # 结构页需求透传
            if not ctx.get("structural_page_request"):
                _spr = pre_slots.get("structural_page_request")
                if (
                    isinstance(_spr, str)
                    and _spr.strip().lower() in _VALID_STRUCTURAL_REQUESTS
                ):
                    ctx["structural_page_request"] = _spr.strip().lower()
                else:
                    ctx.setdefault("structural_page_request", "none")
            if not ctx.get("structural_page_count"):
                _spc = pre_slots.get("structural_page_count")
                if isinstance(_spc, int) and _spc > 0:
                    ctx["structural_page_count"] = _spc
                else:
                    ctx.setdefault("structural_page_count", None)

        await self.execute_subplan(self.sub_plans[0], ctx)

        for subplan in self.sub_plans[1:]:
            await self.execute_subplan(subplan, ctx)

        if not _has_nonempty_topic(ctx):
            raise RequirementCollectError("缺少演示主题 topic，无法继续 PPT 流水线")

        self._set_style_mode(ctx)
        # 图片变量兜底（供 P6.5 Diana 消费）
        self._ensure_image_vars(ctx)
        # 写入 __artifact__，供跨请求续跑复用需求上下文
        _set_requirement_artifact(ctx)
        return ctx
