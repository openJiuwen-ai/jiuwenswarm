# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurboPlanner -- 任务匹配 skill 并返回 plan_code。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jiuwenswarm.server.runtime.skill_turbo.json_utils import extract_llm_json

if TYPE_CHECKING:
    from jiuwenswarm.server.runtime.skill_turbo.environment import SkillTurboEnvironment, Skill

logger = logging.getLogger(__name__)

_SKILL_CODES_PACKAGE = "jiuwenswarm.server.runtime.skill_turbo.skill_codes"
_MIN_ROUTE_CONFIDENCE = 0.6
_ROUTE_MAX_ATTEMPTS = 2


class PlanGenerationError(Exception):
    """规划生成失败。"""


class SkillTurboPlanner:
    """根据任务匹配 skill，返回对应的预规划 plan_code。"""

    def __init__(self, environment: SkillTurboEnvironment):
        self._env = environment

    async def plan(self, task: str, context: dict[str, Any] | None = None) -> str | None:
        """
        根据任务匹配 skill，返回 plan_code。
        无匹配或 skill 无预规划代码时返回 None，由 SkillTurbo 降级处理。
        """
        task = (task or "").strip()
        if not task:
            logger.info("[SkillTurboPlanner] empty task, skip planning")
            return None

        logger.info(
            "[SkillTurboPlanner] planning task=%s context_keys=%s skill_names=%s",
            task,
            list((context or {}).keys()),
            list(self._env.skills.keys()),
        )

        skill = await self.match_skill(task, context)
        if skill is None:
            logger.info("[SkillTurboPlanner] no skill routed")
            return None

        return self.build_plan_code(skill)

    async def match_skill(
        self,
        task: str,
        context: dict[str, Any] | None = None,
    ) -> Skill | None:
        """调用 LLM 将任务路由到已注册 skill。失败或无合适 skill 时返回 None。"""
        return await self._match_skill(task, context)

    def build_plan_code(self, skill: Skill | str) -> str | None:
        """根据 skill 对象或 skill 名查找入口并组装 plan_code。

        Args:
            skill: Skill 对象或 skill 名称字符串
        """
        if isinstance(skill, str):
            skill_obj = self._env.skills.get(skill)
            if skill_obj is None:
                logger.warning("[SkillTurboPlanner] unknown skill name: %s", skill)
                return None
        else:
            skill_obj = skill
        return self._build_skill_plan_code(skill_obj)

    async def _match_skill(
        self,
        task: str,
        context: dict[str, Any] | None = None,
    ) -> Skill | None:
        """调用 LLM 将任务路由到已注册 skill。失败或无合适 skill 时返回 None。"""
        registered_skills = list(self._env.skills.values())
        if not registered_skills:
            logger.info("[SkillTurboPlanner] no registered skills")
            return None

        client = getattr(self._env, "model_client", None)
        if client is None:
            logger.warning("[SkillTurboPlanner] model_client is not configured")
            return None

        skills_payload = [
            {
                "name": getattr(skill, "name", ""),
                "description": getattr(skill, "description", ""),
                "match_keywords": getattr(skill, "match_keywords", []),
            }
            for skill in registered_skills
        ]
        logger.info("[SkillTurboPlanner] route skills detail: %s", skills_payload)

        messages = self._build_route_messages(task, context, skills_payload)
        # 路由输出偶发不可解析（推理模型空 content / 非 JSON 文本）属瞬态失败：
        # 重试一次再降级，避免单次采样异常直接放弃整条加速通道。
        # 注意：confidence 不足 / skill_name=null 是模型正常判定，不在此重试范围内。
        route: dict[str, Any] | None = None
        for attempt in range(1, _ROUTE_MAX_ATTEMPTS + 1):
            collected: list[str] = []
            try:
                # 使用流式调用：部分模型服务对非流式调用存在路由注册限制，
                # 可能触发 429 Route missed，流式调用可规避
                async for chunk in client.stream(messages):
                    content = getattr(chunk, "content", None)
                    if content:
                        collected.append(content)
                raw_content = "".join(collected)
                route = extract_llm_json(raw_content, expected_type=dict)
                break
            except Exception as e:
                # 记录原始返回前缀，便于区分空 content / 非 JSON 文本两类失败
                logger.warning(
                    "[SkillTurboPlanner] LLM route failed attempt=%d/%d error=%s: %s raw_content=%r",
                    attempt,
                    _ROUTE_MAX_ATTEMPTS,
                    type(e).__name__,
                    e,
                    "".join(collected)[:300],
                )
        if route is None:
            return None

        skill_name = route.get("skill_name")
        confidence = self._parse_confidence(route.get("confidence"))
        logger.info(
            "[SkillTurboPlanner] LLM route result skill=%s confidence=%s",
            skill_name,
            confidence,
        )

        if not skill_name:
            return None
        if confidence < _MIN_ROUTE_CONFIDENCE:
            logger.info(
                "[SkillTurboPlanner] route confidence too low skill=%s confidence=%s",
                skill_name,
                confidence,
            )
            return None

        skill = self._env.skills.get(str(skill_name))
        if skill is None:
            logger.warning("[SkillTurboPlanner] routed unknown skill: %s", skill_name)
            return None
        return skill

    @staticmethod
    def _build_route_messages(
        task: str,
        context: dict[str, Any] | None,
        skills_payload: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        system_prompt = """
你是 SkillTurboPlanner 的 skill 路由器。
核心任务：结合用户任务与对话上下文，在全部已注册 skills 里选出适配度最高的单一 skill。

## 强制约束
1. skill_name 只能填写已注册 skills 中的名称；无任何适配技能时固定为 null。
2. confidence 取值范围 0.0 ~ 1.0，数值越高代表匹配程度越高；无匹配时固定填 0.0。
3. 禁止输出执行代码、plan_code、额外解释文字、Markdown 格式，仅返回纯净 JSON。
4. reason 使用简短中文，一句话说明选中 / 未匹配的核心依据。

## PPT pipeline 准入规则（满足任意一条，PPT skill 参与候选）
1. 新建空白 PPT 演示文稿
2. 依据给定主题、素材生成整套演示文稿
3. 上传 docx、pdf、md、txt 等文档，要求将文档内容转为 PPT
4. 调整、重构 PPT 大纲、页面结构、文案逻辑用于生成演示文稿
5. 以已有 PPT 文件内容为参考素材，重新生成全新版本 PPT

## PPT pipeline 排除规则（满足任意一条，PPT skill 不参与候选）
1. 寒暄闲聊、普通问答、纯事实查询、单纯概念讲解
2. 仅对文本做总结提炼，未提出生成 PPT 的需求
3. 撰写普通文章、报告、邮件、脚本等文本，无演示文稿产出要求
4. 仅生成图片、设计图，未提及 PPT / 演示文稿
5. 上传.ppt/.pptx 文件，要求直接编辑、修改原有 PPT 文件（参考旧内容重做新版不受本条限制）
6. 当涉及定时任务、计划任务、调度、提醒、指定某个时间 / 周期再执行等调度类诉求，
   即便文案中出现"PPT / 演示文稿"等字样，也不视为当前要立即生成 PPT，
   应判定为非 PPT pipeline（skill_name 固定为 null）
7. context 中存在 "__skill_turbo_prior_artifacts__" 字段时，需对比该字段记录的已完成产物信息
   与当前任务意图：
   - 若当前任务是基于已有产物的迭代、调整、修复、续作等**增量操作**（如"改成15页"、
     "换风格"、"继续上次"），PPT skill **不参与候选**（由上层流程复用已有产物）；
   - 若当前任务与已有产物的**任意关键参数不同**（包括但不限于主题 topic、
     页数 page_count、风格 style_id、受众 audience、演示目的 presentation_purpose），
     属于从零开始的全新任务，PPT skill **正常参与候选**，不受此条限制。

## 输出固定 JSON 结构
{
"skill_name": "最优技能名称 /null",
"confidence": 0.0~1.0,
"reason": "简短中文判定理由"
}
""".strip()
        user_prompt = json.dumps(
            {
                "registered_skills": skills_payload,
                "task": task,
                "context": context or {},
            },
            ensure_ascii=False,
            default=str,
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    @staticmethod
    def _parse_confidence(value: Any) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, confidence))

    def _build_skill_plan_code(self, skill: Skill) -> str | None:
        """
        查找 skill_code 入口并组装 executor 可执行的 plan_code。

        入口约定：
        1. {skill_codes_dir}/{skill.name}/{skill.name}_gen_root.py
        2. {skill_codes_dir}/{skill.name}/*_gen_root.py
        3. {skill_codes_dir}/{skill.name}/{skill.name}_root.py
        4. {skill_codes_dir}/{skill.name}/*_root.py
        5. skill.plan_code 显式配置兜底
        """
        root_file = self._find_skill_root_file(skill.name)
        if root_file is not None:
            module = self._to_skill_root_module(skill.name, root_file)
            plan_code = f"from {module} import root"
            logger.info(
                "[SkillTurboPlanner] use discovered skill root skill=%s file=%s module=%s",
                skill.name,
                root_file,
                module,
            )
            return plan_code

        if skill.plan_code:
            logger.info("[SkillTurboPlanner] use configured plan_code skill=%s", skill.name)
            return skill.plan_code

        return None

    def _find_skill_root_file(self, skill_name: str) -> Path | None:
        skill_dir = self._skill_codes_dir() / skill_name
        if not skill_dir.is_dir():
            return None

        candidates = [
            skill_dir / f"{skill_name}_gen_root.py",
            skill_dir / f"{skill_name}_root.py",
            *sorted(skill_dir.glob("*_gen_root.py")),
            *sorted(skill_dir.glob("*_root.py")),
            skill_dir / "plan_code.py",
        ]

        seen: set[Path] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate.is_file():
                return candidate
        return None

    def _skill_codes_dir(self) -> Path:
        configured = (
            getattr(self._env, "skill_codes_dir", "")
            or getattr(self._env, "_skill_codes_dir", "")
        )
        if configured:
            return Path(str(configured)).expanduser().resolve()
        return Path(__file__).resolve().parent / "skill_codes"

    def _to_skill_root_module(self, skill_name: str, root_file: Path) -> str:
        package = (
            getattr(self._env, "skill_code_import_package", "")
            or _SKILL_CODES_PACKAGE
        )
        return f"{package}.{skill_name}.{root_file.stem}"
