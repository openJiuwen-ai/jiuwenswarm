# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import logging
import re
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.flash.sections import FLASH_SECTION_BANDS

logger = logging.getLogger(__name__)

# 迁号表：现段名 → 目标段号（均在 sections.FLASH_SECTION_REGISTRY 登记）。
# - 静态收口：可用工具（tools 段）→310、call_goal→320、工具导航→350、
#   上下文压缩/召回（offload + compression_recall）→450、技能加速→750、
#   记忆巨石→600（内容回退 deep 原样、仅迁号）。
# - 动态 1000 带：env→1000（并集主体）、trusted/tui（路径事实）→1010、time（时间事实）→1020；
#   runtime_environment 由 FlashPersonaRail 一次性裁剪为独立实体「Python 库与权限」@1001，
#   本 rail 不再迁号/合并（两实体各自我生命周期）。
_REHOME: dict[str, int] = {
    "tools": 310,                       # 可用工具目录（SDK ContextAssembleRail，原 30 撞号）
    "tool_call_goal": 320,              # call_goal 约定
    "skill_turbo_guide": 750,           # 技能加速通道
    "tool_navigation": 350,             # 渐进式工具导航（静态驻留工具域）
    "offload": 450,                     # 上下文压缩（OFFLOAD，SDK 原 90）
    "compression_recall": 450,          # 压缩上下文召回（RECALL，SDK 原 90）
    "memory": 600,                      # 记忆（deep 原样巨石段，仅迁号）
    "env": 1000,                        # 环境与 Shell（并集主体；runtime_environment 尾巴并入后删除）
    "trusted_dirs_policy": 1010,        # 路径事实（工作目录策略）
    "tui_current_project_policy": 1010, # 路径事实（运行时目录上下文）
    "time": 1020,                       # 时间事实
}

# 剥除表：被新段（200 目录模型 / 210 路径与权限 / 620 存储层级）完全取代的冗余段。
_REMOVE: frozenset[str] = frozenset({
    "workspace",  # 旧 工作空间@70（工作目录路径值由 1010 trusted/tui 承载，文件表由 200/620 覆盖）
})

# 段顶标题层级重整：每段标题重锚到 H2，段前 prepend 域 H1。
# 999 relay 产品包豁免降级（其内容自带 ## 结构、由 relay 维护），但补一个域 H1 总帽，
# 避免它视觉上挂到上一个域（技能）下面。
_EXEMPT: frozenset[str] = frozenset({
    "request_system_prompt",
})
_EXEMPT_HEADERS: dict[str, dict[str, str]] = {
    "request_system_prompt": {"cn": "会话策略包", "en": "Session Policy Pack"},
}

# 图片理解引导并入工具执行路由：MultimodalImageRail 的非原生模型策略段
# （multimodal_image_policy，纯文本无标题）并入 340 tool_result，作为「图片理解路由」子块。
_IMAGE_POLICY_SECTION = "multimodal_image_policy"
_IMAGE_ROUTER_SECTION = "tool_result"
_IMAGE_ROUTER_HEADINGS: dict[str, str] = {
    "cn": "## 图片理解路由",
    "en": "## Image Understanding Routing",
}
_IMAGE_ROUTER_MARKERS: dict[str, str] = {
    "cn": "图片理解路由",
    "en": "Image Understanding Routing",
}

# 域 → 域头标题（双语）。键与 sections.FLASH_SECTION_BANDS 一致。
_DOMAIN_HEADERS: dict[str, dict[str, str]] = {
    "identity":    {"cn": "身份与总则", "en": "Identity & General Rules"},
    "security":    {"cn": "安全与边界", "en": "Safety & Boundaries"},
    "workspace":   {"cn": "工作空间与文件", "en": "Workspace & Files"},
    "tools":       {"cn": "工具", "en": "Tools"},
    "execution":   {"cn": "任务执行", "en": "Task Execution"},
    "interaction": {"cn": "交互与输出", "en": "Interaction & Output"},
    "memory":      {"cn": "记忆", "en": "Memory"},
    "skills":      {"cn": "技能", "en": "Skills"},
    "facts":       {"cn": "运行事实", "en": "Runtime Facts"},
}

# 严格 markdown 标题：行首 1–6 个 # + 空白（# 后必须有空格，避免误伤 #注释/正文）。
_HEADING_RE = re.compile(r"^(#{1,6})([ \t])")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")

# 目标顶层标题级别（段内所有标题按此相对平移）。
_TOP_HEADING_LEVEL = 2


def _rebind_headings_to_h2(text: str) -> str | None:
    """把段内标题整体重锚：**首个标题**固定为 H2，其后标题相对平移但**不低于 H3**。

    - 仅处理严格标题行（行首 1–6 个 `#` + 空格），fence（``` / ~~~）内跳过；
    - 无标题 → 返回 None（调用方保留原样）；
    - 处理「顶层 H2 + 段内又出现 H1」这类畸形（如 relay 注入的 SOUL.md/AGENT.md：
      ``## SOUL.md`` 下又有 ``# 灵魂``）——内部标题一律夹到 ≥ H3；
    - 守卫：**非标题内容逐字节不变**（只允许改标题前缀），否则返回 None。
    """
    lines = text.split("\n")
    top_level = _first_heading_level(lines)
    if top_level is None:
        return None
    shift = _TOP_HEADING_LEVEL - top_level

    out: list[str] = []
    in_fence = False
    first_done = False
    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence:
            m = _HEADING_RE.match(line)
            if m:
                level = len(m.group(1))
                if not first_done:
                    new_level = _TOP_HEADING_LEVEL
                    first_done = True
                else:
                    new_level = max(_TOP_HEADING_LEVEL + 1, min(6, level + shift))
                line = "#" * new_level + line[level:]
        out.append(line)
    new_text = "\n".join(out)

    # 守卫：剥离每行标题前缀后，全文必须逐字节一致（只允许标题前缀变化）。
    if _strip_heading_prefix(new_text) != _strip_heading_prefix(text):
        return None
    return new_text


def _strip_heading_prefix(text: str) -> str:
    """去掉每行开头的 markdown 标题前缀（`#`+空格），用于「非标题内容不变」校验。"""
    return "\n".join(_HEADING_RE.sub("", line) for line in text.split("\n"))


def _first_heading_level(lines: list[str]) -> int | None:
    in_fence = False
    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if m:
            return len(m.group(1))
    return None


class FlashSectionOrderRail(DeepAgentRail):
    """段序收口器：把仍占旧号的段迁到目标段号，剥除被替代的冗余段。"""

    # 需在全部段注入之后执行（ContextAssembleRail=85、stream=60 级、RuntimePromptRail=5、
    # 域 rail=4、relay_merge/tool_guide=3、pressure=3 之后）、audit(0) 与
    # DisabledToolsRail(1) 之前；取 2。
    priority = 2
    inherit_to_subagents = False

    def __init__(self) -> None:
        super().__init__()
        self._system_prompt_builder: Any = None
        # 层级重整幂等：段名 → 已处理段对象的 id。对象未变（跨轮驻留）跳过；变（重注入）重做。
        self._transformed: dict[str, int] = {}

    def init(self, agent: Any) -> None:
        self._system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent: Any) -> None:
        self._system_prompt_builder = None
        self._transformed = {}

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        builder = (
            getattr(ctx.agent, "system_prompt_builder", None)
            or self._system_prompt_builder
        )
        if builder is None:
            return
        self._system_prompt_builder = builder
        self.apply(builder)

    def apply(self, builder: Any) -> None:
        """Apply the order-correction transform without owning Rail callback registration."""
        if builder is None:
            return
        self._rehome(builder)
        self._strip(builder)
        self._merge_image_router(builder)
        self._normalize_hierarchy(builder)

    def _rehome(self, builder: Any) -> None:
        """把仍占旧号的段 remove + 重加到目标段号；段不存在或已在目标号则跳过。"""
        for name, priority in _REHOME.items():
            section = self._get_section(builder, name)
            if section is None:
                continue
            if int(getattr(section, "priority", 0) or 0) == priority:
                continue
            content = getattr(section, "content", None)
            if isinstance(content, dict):
                new_content = {lang: str(v) for lang, v in content.items() if v}
            else:
                new_content = {self._resolve_language(builder): str(content or "")}
            if not new_content:
                continue
            builder.remove_section(name)
            builder.add_section(PromptSection(
                name=name,
                content=new_content,
                priority=priority,
            ))
            logger.debug("[FlashSectionOrderRail] rehome %s -> %s", name, priority)

    def _strip(self, builder: Any) -> None:
        """按名剥除被新段取代的冗余段（存在才剥）。"""
        for name in _REMOVE:
            if self._get_section(builder, name) is None:
                continue
            builder.remove_section(name)
            logger.debug("[FlashSectionOrderRail] remove redundant %s", name)
        # shared skill_protocol 段：flash 技能域接管成功（含 730 执行规范）后其内容已
        # 分散到 710（加载块）/ 730（执行规范），剥除旧段；回退到原生技能域时保留作兜底。
        if (
            self._get_section(builder, "skill_execution_rules") is not None
            and self._get_section(builder, "skill_protocol") is not None
        ):
            builder.remove_section("skill_protocol")
            logger.debug("[FlashSectionOrderRail] remove redundant skill_protocol")

    # ── 图片理解路由并入（工具执行指导）──────────────────────────────
    def _merge_image_router(self, builder: Any) -> None:
        """把 ``multimodal_image_policy`` 段原样并入 340 ``tool_result`` 的「图片理解路由」子块。

        - 文案原样搬运（不改旧行为）；用 ``##`` 标题，经重锚后变 ``### 图片理解路由``；
        - 守卫：``tool_result`` 缺失 → 保留游离段（不丢内容）；
        - 幂等：目标已含「图片理解路由」标记则不重复追加（tool_result 跨轮驻留时）。
        """
        image_section = self._get_section(builder, _IMAGE_POLICY_SECTION)
        if image_section is None:
            return
        target = self._get_section(builder, _IMAGE_ROUTER_SECTION)
        if target is None:
            return  # 兜底：目标段缺失，保留游离段
        image_content = getattr(image_section, "content", None)
        target_content = getattr(target, "content", None)
        if not isinstance(image_content, dict) or not isinstance(target_content, dict):
            return

        merged: dict[str, str] = dict(target_content)
        changed = False
        covered = 0
        for lang, text in image_content.items():
            base = merged.get(lang)
            if not isinstance(text, str) or not text.strip() or not isinstance(base, str):
                continue  # 目标无该语言（非渲染语言）→ 忽略
            covered += 1
            is_cn = lang in ("cn", "zh")
            marker = _IMAGE_ROUTER_MARKERS["cn" if is_cn else "en"]
            if marker in base:  # 已并入（幂等），无需重复追加
                continue
            heading = _IMAGE_ROUTER_HEADINGS["cn" if is_cn else "en"]
            merged[lang] = base.rstrip() + f"\n\n{heading}\n\n{text.strip()}"
            changed = True
        if covered == 0:
            return  # 目标无任何对应语言，保守保留游离段（不丢内容）
        # 目标已覆盖图片段的渲染语言 → 删除游离段（无论本轮是否新追加）。
        builder.remove_section(_IMAGE_POLICY_SECTION)
        if changed:
            builder.add_section(PromptSection(
                name=target.name,
                content=merged,
                priority=target.priority,
            ))
        logger.debug("[FlashSectionOrderRail] merge %s -> %s", _IMAGE_POLICY_SECTION, _IMAGE_ROUTER_SECTION)

    # ── 标题层级重整 ──────────────────────────────────────────────
    def _normalize_hierarchy(self, builder: Any) -> None:
        """每段标题重锚 H2 + 域首段 prepend 域 H1（幂等，带强守卫）。

        - ``_EXEMPT`` 内段（999 relay）跳过，原样保留；
        - 段对象 id 未变（跨轮驻留）跳过；对象变化（每轮重注入）才重做；
        - 标题重锚失败（守卫命中）→ 该段原样保留，不盲改。
        """
        sections = self._all_sections(builder)
        band_first = self._band_first_names(sections)
        for name, section in sections.items():
            if self._transformed.get(name) == id(section):
                continue
            content = getattr(section, "content", None)
            if not isinstance(content, dict):
                continue
            rewritten: dict[str, str] = {}
            changed = False
            for lang, text in content.items():
                if not isinstance(text, str):
                    rewritten[lang] = text
                    continue
                if name in _EXEMPT:
                    # 豁免段：不降级（保留 relay 自带层级），仅 prepend 域 H1 总帽。
                    header = self._exempt_header(name, lang)
                    rebased = (header + "\n\n" + text) if (header and not text.startswith(header)) else text
                else:
                    rebased = _rebind_headings_to_h2(text)
                    if rebased is None:
                        rebased = text  # 守卫命中：保留原样
                    if name in band_first:
                        header = self._domain_header(band_first[name], lang)
                        if header:
                            rebased = header + "\n\n" + rebased
                rewritten[lang] = rebased
                changed = changed or (rebased != text)
            if not changed:
                self._transformed[name] = id(section)
                continue
            builder.add_section(PromptSection(
                name=section.name,
                content=rewritten,
                priority=section.priority,
            ))
            new_section = builder.get_section(section.name)
            self._transformed[name] = id(new_section) if new_section is not None else id(section)
            logger.debug("[FlashSectionOrderRail] normalize heading hierarchy: %s", name)

    def _band_first_names(self, sections: dict[str, Any]) -> dict[str, str]:
        """每个域（band）里优先级最小的段名 → 域键。用于 prepend 域 H1。"""
        result: dict[str, str] = {}
        # 先按 band 收集 (priority, name)
        by_band: dict[str, list[tuple[int, str]]] = {}
        for name, section in sections.items():
            if name in _EXEMPT:
                continue
            priority = int(getattr(section, "priority", 0) or 0)
            band = self._band_of(priority)
            if band is None:
                continue
            by_band.setdefault(band, []).append((priority, name))
        for band, entries in by_band.items():
            entries.sort()
            result[entries[0][1]] = band
        return result

    @staticmethod
    def _band_of(priority: int) -> str | None:
        for band, rng in FLASH_SECTION_BANDS.items():
            if priority in rng:
                return band
        return None

    @staticmethod
    def _domain_header(band: str, lang: str) -> str:
        titles = _DOMAIN_HEADERS.get(band)
        if not titles:
            return ""
        title = titles.get("cn" if lang in ("cn", "zh") else "en") or titles.get("cn", "")
        return f"# {title}" if title else ""

    @staticmethod
    def _exempt_header(name: str, lang: str) -> str:
        titles = _EXEMPT_HEADERS.get(name)
        if not titles:
            return ""
        title = titles.get("cn" if lang in ("cn", "zh") else "en") or titles.get("cn", "")
        return f"# {title}" if title else ""

    @staticmethod
    def _all_sections(builder: Any) -> dict[str, Any]:
        get_all = getattr(builder, "get_all_sections", None)
        if callable(get_all):
            try:
                result = get_all()
                if isinstance(result, dict):
                    return dict(result)
            except Exception:
                logger.debug("[FlashSectionOrderRail] get_all_sections() failed", exc_info=True)
        sections = getattr(builder, "_sections", None)
        if isinstance(sections, dict):
            return dict(sections)
        return {}

    @staticmethod
    def _get_section(builder: Any, name: str) -> Any | None:
        get_section = getattr(builder, "get_section", None)
        if not callable(get_section):
            return None
        try:
            return get_section(name)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _resolve_language(builder: Any) -> str:
        language = getattr(builder, "language", None) or "cn"
        return "cn" if language in ("cn", "zh") else "en"


__all__ = [
    "_DOMAIN_HEADERS",
    "_EXEMPT",
    "_REHOME",
    "_REMOVE",
    "FlashSectionOrderRail",
    "_rebind_headings_to_h2",
]
