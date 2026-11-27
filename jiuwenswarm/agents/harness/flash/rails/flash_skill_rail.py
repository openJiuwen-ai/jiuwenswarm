# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashSkillRail —— 7xx 技能域常驻重组（点 5 技能检索翻牌）。

把 flash 原生 ``SkillUseRail`` 钉死 ALL 模式产出的全量 ``skills`` 段（约 10,772 字符）
换成五段常驻规则 + 精简清单：

- 700 技能总则（存在方式 / 发现路径 / 触发语义）
- 710 技能选择与互斥（互斥组 / 不误替换条款）
- 720 技能加载规则（入口统一 skill_tool + 强约束前置 + 经验查询/加载正文块）
- 730 技能执行规范（执行纪律 9 条；取自 skill_protocol 段抽取，⚠️ 并入第 9 条）
- 740 技能清单（manifest.yaml 启用集：名称 + 一句话描述，~5k）

740 数据源 = manifest.yaml 的 ``default_enabled: true`` 条目（部署侧 relay 技能仓，
只读、不改 relay）。manifest 缺失/解析失败时回退到实际已加载且允许执行的技能
（``fallback_skills_provider``），并把 manifest 未覆盖的本地技能合并进清单，避免私有
技能随接管消失；两者皆空时才提示走 ``search_skill`` 检索。

750 加速通道（skill_turbo_guide）由 FlashTurboPromptRail 注入、FlashSectionOrderRail
收口到 750；shared ``skill_protocol`` 段在 flash 技能域接管成功后被收口器剥除
（710 已并入其加载块、730 已承载其执行规范）。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

import yaml

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.prompts.sections import SectionName
from openjiuwen.harness.rails import SkillUseRail
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.rails.skill_protocol_prompt_rail import (
    _build_skill_protocol_section_text,
)

logger = logging.getLogger(__name__)

# 段号即 priority（sections.py 7xx 带）。
_SECTION_OVERVIEW = "skill_overview"
_SECTION_LOADING = "skill_loading"
_SECTION_CHOICE = "skill_selection_policy"
_SECTION_EXECUTION = "skill_execution_rules"
_SECTION_CATALOG = "skill_catalog"

_PRIORITY_OVERVIEW = 700
_PRIORITY_CHOICE = 710   # 技能选择（先选）
_PRIORITY_LOADING = 720  # 技能加载（后加载）
_PRIORITY_EXECUTION = 730
_PRIORITY_CATALOG = 740

_MANIFEST_FILENAME = "manifest.yaml"

_DEFAULT_DESCRIPTION_MAX_LENGTH = 800
_DEFAULT_MAX_ENTRIES = 50


def _load_enabled_skills(manifest_path: Path) -> list[tuple[str, str]] | None:
    """读取 manifest.yaml，返回 ``default_enabled: true`` 的 ``(name, description)`` 列表。

    只读、不抛。返回 ``None`` 表示**解析失败/结构异常**（上层据此回退到原生
    skills 段）；返回 ``[]`` 表示**解析成功但没有启用技能**（上层照常接管，
    注入空清单）。二者必须区分，不能像旧版那样都用空列表表示失败——否则上层
    无法判断该回退还是接管。
    """
    try:
        text = manifest_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None

    if not isinstance(data, dict):
        return None
    skills = data.get("skills")
    if not isinstance(skills, dict):
        return None

    enabled: list[tuple[str, str]] = []
    for name, meta in skills.items():
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(meta, dict) or not meta.get("default_enabled"):
            continue
        description = meta.get("description")
        if not isinstance(description, str):
            description = ""
        enabled.append((name.strip(), _one_line(description)))
    return enabled


def _one_line(text: str) -> str:
    """折叠换行/缩进为单行空格（YAML 折叠/块标量都压成一行描述）。"""
    return " ".join(text.split())


def _truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return f"{text[:cap]}…"


_EN_SPLIT_RE = re.compile(r"\s+EN\s*:\s*")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])\s*")
# 流程句：属于 SKILL.md 执行细节，不应占用清单里的「能力 + 触发词 + 限制」预算。
_PROCEDURE_RE = re.compile(
    r"加载方式|加载后|环境预检|source\s+manifest|scenario_router|builder|校验管线|"
    r"交付执行|route\s*→|→\s*manifest|输出模板|安装依赖|逐字照抄|"
    r"office_claw_load_skill|relative_file_path",
    re.IGNORECASE,
)
# 保留标记：限制/触发词/场景/首句能力块——即使句内提到流程词（如「禁止绕过」块里的
# builder/校验管线）也必须保留，否则会把「反绕过」护栏误删。
_KEEP_MARKER_RE = re.compile(r"【(?:强制·第一步|禁止|触发词|固定)")


def _catalog_excerpt(description: str, max_chars: int) -> str:
    """把 manifest 描述压成「能力 + 触发词 + 限制」的清单摘录。

    与 ``_truncate`` 的差异：不按字符硬切。先剥掉末尾 ``EN`` 双语重复，再丢弃
    「加载方式 / 执行管线」等流程句（它们属于 SKILL.md，模型加载后自会看到），
    只保留能力句、触发词与限制；仍超预算时才退回 ``_truncate`` 兜底。这样像
    xlsx-craft 这类「触发词落在描述尾部」的技能，触发信号不会被截掉。
    """
    if not description:
        return ""
    text = _EN_SPLIT_RE.split(description, maxsplit=1)[0].strip()
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    kept = [
        s for s in sentences
        if _KEEP_MARKER_RE.search(s) or not _PROCEDURE_RE.search(s)
    ]
    result = " ".join(kept).strip()
    if not result:
        return _truncate(text, max_chars)
    return result if len(result) <= max_chars else _truncate(result, max_chars)


def _promote_h3_to_h2(text: str) -> str:
    """把行首恰好 ``### ``（H3）的子标题提升为 ``## ``（H2），其余不动。

    用于把协议抽取块并入加载段（段顶 H1）时消除「H1→H3」跳级；``#### `` 等更高级不受影响。
    """
    return re.sub(r"(?m)^### ", "## ", text)


# ─────────────────────────────────────────────────────────────
# 从共享 ``skill_protocol`` 正文抽取（710 加载块 / 730 执行规范）
#
# deep 正文 ``_CN_PROTOCOL`` / ``_EN_PROTOCOL`` 一字不动，本文件只在运行时按锚点
# 切出 flash 需要的两段，避免双份转抄；锚点缺失 / 乱序返回 ``None``，调用方降级。
# ─────────────────────────────────────────────────────────────

_EXECUTION_ANCHOR_CN = "随后按 SKILL 工作流执行"
_EXECUTION_ANCHOR_EN = "Then execute the workflow"


def _extract_loading_blocks(content: str, language: str = "cn") -> str | None:
    """从协议正文抽取「加载前置」两块（①经验查询 + ②加载 SKILL.md），供 flash 710 并入。

    返回从「### 经验查询…」起到执行锚点前的连续正文（两块恒相邻）；任一块标题或
    锚点缺失 / 乱序返回 ``None``（guard，不盲切）。
    """
    headings = (
        ["### 经验查询与技能流程的顺序", "### 加载 SKILL.md 正文（禁止用 bash 执行工具名）"]
        if language == "cn"
        else [
            "### Experience lookup before the skill workflow",
            "### Load SKILL.md body (never run tool names as shell/bash commands)",
        ]
    )
    anchor = _EXECUTION_ANCHOR_CN if language == "cn" else _EXECUTION_ANCHOR_EN
    if not content:
        return None
    i1 = content.find(headings[0])
    i2 = content.find(headings[1])
    end = content.find(anchor)
    if i1 < 0 or i2 < 0 or end < 0:
        return None
    if not i1 < i2 < end:
        return None
    block = content[i1:end].rstrip()
    return block or None


def _extract_execution_rules(content: str, language: str = "cn") -> str | None:
    """从协议正文抽取「技能执行规范」9 条，flash 730 段正文。

    取「随后按 SKILL 工作流执行 / Then execute the workflow」起、到正文末尾的规则区；
    末尾 ``⚠️ 消息语义`` 单独段并入第 9 条（作其末条 bullet）。守卫：锚点缺失返回 None。
    """
    anchor = _EXECUTION_ANCHOR_CN if language == "cn" else _EXECUTION_ANCHOR_EN
    if not content:
        return None
    idx = content.find(anchor)
    if idx < 0:
        return None
    rules = content[idx:].strip()
    widx = rules.rfind("⚠️")
    if widx > 0:
        main = rules[:widx].rstrip()
        tail = rules[widx:].strip()
        rules = main + "\n   - " + tail
    return rules


# ─────────────────────────────────────────────────────────────
# 700 / 710 / 720 常驻规则正文（双语）
# ─────────────────────────────────────────────────────────────

_OVERVIEW_CN = """# 技能总则
技能是针对特定领域任务的可复用流程包（SKILL.md + 资源）。本系统通过技能目录发现并加载技能。
- 存在方式：下方「技能清单」列出当前已启用的技能；清单未列出的技能不常驻，需要时用 search_skill 检索。
- 发现路径：任务匹配清单中某个技能的描述时加载它；清单外但有明确技能意图时，先用 search_skill 查。
- 触发语义：仅在任务确实匹配技能描述时加载；不相关的任务不加载技能，直接用基础工具完成。"""

_OVERVIEW_EN = (
    "# Skill Overview\n"
    "A skill is a reusable procedure package (SKILL.md + resources) for a "
    "specific domain task.\n"
    "- Existence: the catalog below lists currently enabled skills; skills not "
    "listed are not resident — use search_skill when you need one.\n"
    "- Discovery: load a skill when the task matches its description; for clear "
    "skill intent not in the catalog, search with search_skill first.\n"
    "- Triggering: load a skill only when the task genuinely matches its "
    "description; otherwise complete the task with base tools."
)

_LOADING_CN = """# 技能加载
- 加载入口统一为 skill_tool（skill_name + SKILL.md 路径）。禁止用 read_file 直接读 SKILL.md 来加载技能——那会绕过授权、凭证注入与防过期治理链。
- 部分技能有强约束前置条件（例如 xlsx-craft 要求任何 Excel 任务第一步必须先加载）。命中这类技能时，严格按 SKILL.md 里的前置条件执行，不得跳过。
- 加载成功后按 SKILL.md 流程执行；加载失败或返回「不支持」时，改用基础工具或其它技能，不要原样重试。"""

_LOADING_EN = (
    "# Skill Loading\n"
    "- Load through skill_tool (skill_name + SKILL.md path). Do not load a skill "
    "by reading SKILL.md directly with read_file — that bypasses authorization, "
    "credential injection and expiry governance.\n"
    "- Some skills carry mandatory preconditions (e.g. xlsx-craft requires loading "
    "first for any Excel task). Follow the SKILL.md preconditions exactly.\n"
    "- After a successful load, follow the SKILL.md workflow; on failure or "
    "\"unsupported\", switch to base tools or another skill — do not retry "
    "unchanged."
)

_CHOICE_CN = """# 技能选择
- 一个任务只选一个最匹配的技能；存在互斥技能组（多个技能都声称处理同一文件类型）时，只加载其中一个。
- 不要仅因两个技能处理同一文件类型就互相替换；用户明确指定的技能优先，且指定技能必须与任务范围匹配。
- 选择依据 = 用户原始请求 + 历史上下文；不要擅自把「编辑」改写成「新建」或把「新建」改写成「编辑」。"""

_CHOICE_EN = (
    "# Skill Selection\n"
    "- Choose exactly one best-matching skill per task; when a mutually exclusive "
    "group exists (multiple skills claiming the same file type), load only one.\n"
    "- Do not substitute one skill for another merely because they handle the same "
    "file type; the user's explicit choice takes priority and must match the task "
    "scope.\n"
    "- Decide from the user's original request plus history; never rewrite an "
    "\"edit\" as a \"new\" task or vice versa."
)


class FlashSkillRail(DeepAgentRail):
    """把原生全量 skills 段翻牌为 700/710/720/730(执行规范)/740(清单)。"""

    # 必须在 SkillUseRail 之后执行：SkillUseRail 每次模型调用重建原生 skills 段，
    # 本 rail 要在其后把它撤下并换成 700–730，否则模型同时看到两份技能提示。
    priority = SkillUseRail.priority - 1
    inherit_to_subagents = False

    def __init__(
        self,
        *,
        manifest_path: Optional[str | Path] = None,
        skills_dirs: Optional[list[str]] = None,
        catalog_description_max_length: int = _DEFAULT_DESCRIPTION_MAX_LENGTH,
        catalog_max_entries: int = _DEFAULT_MAX_ENTRIES,
        fallback_skills_provider: Any = None,
        config_provider: Any = None,
    ) -> None:
        super().__init__()
        self._manifest_path = Path(manifest_path) if manifest_path else None
        self._skills_dirs: list[Path] = [Path(p) for p in (skills_dirs or [])]
        if catalog_description_max_length < 1:
            raise ValueError("skill-catalog: catalog_description_max_length 必须 >= 1")
        if catalog_max_entries < 1:
            raise ValueError("skill-catalog: catalog_max_entries 必须 >= 1")
        self._description_max_length = catalog_description_max_length
        self._max_entries = catalog_max_entries
        self._fallback_skills_provider = fallback_skills_provider
        self._config_provider = config_provider

        self._system_prompt_builder: Any = None
        # 缓存清单路径与解析结果；manifest 属于部署配置，会话内不变。解析结果按
        # (mtime_ns, size) 缓存，避免每次 before_model_call 同步重读/重解析大文件。
        self._resolved_manifest: Optional[Path] = None
        self._manifest_missing: bool = False
        self._enabled_cache: Optional[tuple[tuple[int, int], list[tuple[str, str]]]] = None
        self._warned_extract_miss = False

    def _enabled_by_config(self) -> bool:
        """每轮现读 flash.skill_catalog.enabled（热重载同步）。provider 缺省时默认启用。"""
        provider = self._config_provider
        if provider is None:
            return True
        try:
            config = provider()
        except Exception:
            return True
        return bool(config.get("enabled", True)) if isinstance(config, dict) else True

    def init(self, agent: Any) -> None:
        self._system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent: Any) -> None:
        self._system_prompt_builder = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        builder = getattr(ctx.agent, "system_prompt_builder", None) or self._system_prompt_builder
        if builder is None:
            return
        self._system_prompt_builder = builder

        if not self._enabled_by_config():
            # 禁用：撤下本 rail 注入的 700–740 五段、不再删原生 skills 段。
            for name in (
                _SECTION_OVERVIEW,
                _SECTION_LOADING,
                _SECTION_CHOICE,
                _SECTION_EXECUTION,
                _SECTION_CATALOG,
            ):
                builder.remove_section(name)
            return

        language = self._resolve_language()
        manifest = self._resolve_manifest()
        manifest_entries: list[tuple[str, str]] | None = None
        if manifest is not None:
            manifest_entries = self._load_enabled_skills_cached(manifest)
            if manifest_entries is None:
                logger.warning(
                    "FlashSkillRail: manifest 解析失败（%s），回退本地已加载技能生成清单",
                    manifest,
                )

        # 评审 #3：manifest 缺失/解析失败时用实际已加载技能兜底；manifest 存在但
        # 未覆盖的本地技能合并进清单，避免私有技能随接管消失。
        fallback = self._fallback_skills()
        if manifest_entries is None:
            enabled = fallback if fallback else None
        else:
            names = {n for n, _ in manifest_entries}
            extra = [(n, d) for n, d in fallback if n not in names]
            enabled = list(manifest_entries) + extra

        # 撤下原生全量 skills 段（SkillUseRail 本轮已重建）。原生段在无技能命中时
        # 提示「用 read_file 读 SKILL.md」，与 skill_protocol「禁止 read_file、只走
        # skill_tool」直接冲突——故无论 manifest 是否可用都接管：700/710/720 规则段
        # 与 manifest 无关、恒注入，只有 730 清单依赖 manifest（缺失时注入空清单提示）。
        builder.remove_section(SectionName.SKILLS)

        # 720 技能加载：常驻加载规则 + 「经验查询前置/加载 SKILL.md 正文」块（取自
        # shared skill_protocol 抽取，单一事实源；抽取守卫命中则只保留常驻规则）。
        # 抽取块在协议源里是 H3（段内子标题），并入本段（段顶 H1）前提升一级为 H2，
        # 避免「H1 段顶 → H3 子块」跳级；收口器统一重锚 H2 后即为「## 加载 → ### 子块」。
        loading_base = _LOADING_CN if language == "cn" else _LOADING_EN
        loading_extra = _extract_loading_blocks(
            _build_skill_protocol_section_text(language), language,
        )
        if loading_extra is not None:
            loading_extra = _promote_h3_to_h2(loading_extra)

        # 730 技能执行规范：9 条执行纪律（⚠️ 已并入第 9 条）。
        execution_rules = _extract_execution_rules(
            _build_skill_protocol_section_text(language), language,
        )

        if (loading_extra is None or execution_rules is None) and not self._warned_extract_miss:
            self._warned_extract_miss = True
            logger.warning(
                "[FlashSkillRail] skill_protocol 抽取未命中（加载块=%s，执行规范=%s）："
                "上游锚点/标题可能已变——720 加载块降级、730 执行规范缺失，"
                "请同步 _extract_loading_blocks / _extract_execution_rules 的锚点",
                loading_extra is not None,
                execution_rules is not None,
            )

        builder.add_section(PromptSection(
            name=_SECTION_OVERVIEW,
            content={language: _OVERVIEW_CN if language == "cn" else _OVERVIEW_EN},
            priority=_PRIORITY_OVERVIEW,
        ))
        builder.add_section(PromptSection(
            name=_SECTION_LOADING,
            content={
                language: (loading_base + "\n\n" + loading_extra)
                if loading_extra is not None else loading_base,
            },
            priority=_PRIORITY_LOADING,
        ))
        builder.add_section(PromptSection(
            name=_SECTION_CHOICE,
            content={language: _CHOICE_CN if language == "cn" else _CHOICE_EN},
            priority=_PRIORITY_CHOICE,
        ))
        if execution_rules is not None:
            execution_header = (
                "# 技能执行规范（强制）\n\n"
                if language == "cn"
                else "# Skill Execution Protocol (Mandatory)\n\n"
            )
            builder.add_section(PromptSection(
                name=_SECTION_EXECUTION,
                content={language: execution_header + execution_rules},
                priority=_PRIORITY_EXECUTION,
            ))
        builder.add_section(PromptSection(
            name=_SECTION_CATALOG,
            content={language: self._render_catalog(enabled, language)},
            priority=_PRIORITY_CATALOG,
        ))

    def _resolve_manifest(self) -> Optional[Path]:
        if self._resolved_manifest is not None:
            return self._resolved_manifest
        if self._manifest_missing:
            return None

        candidates: list[Path] = []
        if self._manifest_path is not None:
            candidates.append(self._manifest_path)
        for skills_dir in self._skills_dirs:
            candidates.append(skills_dir / _MANIFEST_FILENAME)

        for candidate in candidates:
            if candidate.is_file():
                self._resolved_manifest = candidate
                return candidate

        self._manifest_missing = True
        return None

    def _load_enabled_skills_cached(self, manifest: Path) -> list[tuple[str, str]] | None:
        """按 (mtime, size) 缓存 ``_load_enabled_skills`` 的解析结果。

        解析失败（None）不缓存，下一轮重试；成功结果缓存到文件变化（部署侧更新
        manifest）为止，避免真实 manifest（16 万字符级）每轮同步重读重解析。
        """
        try:
            stat = manifest.stat()
            key = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            # stat 失败交给底层 read_text 处理（同样返回 None）。
            return _load_enabled_skills(manifest)

        cached = self._enabled_cache
        if cached is not None and cached[0] == key:
            return cached[1]

        result = _load_enabled_skills(manifest)
        if result is not None:
            self._enabled_cache = (key, result)
        return result

    def _resolve_language(self) -> str:
        lang = getattr(self._system_prompt_builder, "language", None) or "cn"
        return "cn" if lang in ("cn", "zh") else "en"

    def _fallback_skills(self) -> list[tuple[str, str]]:
        """从 fallback provider 取实际已加载技能 (name, description)（评审 #3）。"""
        provider = self._fallback_skills_provider
        if provider is None:
            return []
        try:
            result = provider()
        except Exception as exc:
            logger.warning("[FlashSkillRail] fallback skills provider failed: %s", exc)
            return []
        if not result:
            return []
        return [(str(n), str(d or "")) for n, d in result]

    def _render_catalog(self, enabled: list[tuple[str, str]] | None, language: str) -> str:
        header = (
            "# 技能清单\n\n已启用的技能（名称 + 一句话说明）。需要用某个技能时，"
            "用 skill_tool 加载其 SKILL.md。\n"
            if language == "cn"
            else "# Skill Catalog\n\nEnabled skills (name + one-line description). "
            "Load a skill's SKILL.md through skill_tool when you need it.\n"
        )
        if enabled is None:
            note = (
                "当前技能清单不可用（manifest.yaml 缺失或解析失败）。"
                "需要用技能时，先用 search_skill 检索，再经 skill_tool 加载。"
                if language == "cn"
                else "The skill catalog is unavailable (manifest.yaml missing or unparsable). "
                "To use a skill, search with search_skill first, then load it via skill_tool."
            )
            return header + note
        if not enabled:
            note = (
                "当前没有默认启用的技能（manifest.yaml 中 default_enabled 均为关闭）。"
                "需要用技能时，先用 search_skill 检索，再经 skill_tool 加载。"
                if language == "cn"
                else "No skills are enabled by default (all default_enabled entries are off). "
                "To use a skill, search with search_skill first, then load it via skill_tool."
            )
            return header + note
        shown = enabled[: self._max_entries]
        hidden = len(enabled) - len(shown)
        lines = [header]
        for name, description in shown:
            desc = _catalog_excerpt(description, self._description_max_length) if description else ""
            lines.append(f"- {name}: {desc}" if desc else f"- {name}")
        if hidden > 0:
            tail = (
                f"\n其余 {hidden} 个技能未列出，需要时用 search_skill 检索。"
                if language == "cn"
                else f"\nThe other {hidden} skills are not listed; use search_skill to find them."
            )
            lines.append(tail)
        return "\n".join(lines)


__all__ = [
    "FlashSkillRail",
    "_load_enabled_skills",
    "_one_line",
    "_truncate",
]
