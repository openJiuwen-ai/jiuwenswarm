# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""DESC_OPTIMIZE 阶段处理器.

核心流程（对齐官方 Description Optimization，但用我们自己的模型 API 实现）：

1. Agent 生成 ~20 个 trigger eval queries（should_trigger / should_not_trigger）
2. Train/test split (60% / 40%)
3. 迭代优化循环（最多 max_iterations 轮）：
   a. 对每个 query，调用模型判断当前 description 是否会触发
   b. 统计 pass rate
   c. 基于失败案例，调用模型生成改进的 description
   d. 如果 train 全部通过则提前退出
4. 选 test score 最高的 description（防过拟合）
5. 将 best_description 写回 SKILL.md frontmatter
6. 下一阶段为 PACKAGE（在打包前完成描述优化）

官方实现用 `claude -p` CLI subprocess 做触发测试和描述改进。
我们的实现通过 ctx.create_stage_agent 直接调用模型 API，不依赖 CLI。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from jiuwenclaw.agentserver.skilldev.context import SkillDevContext
from jiuwenclaw.agentserver.skilldev.schema import (
    DescOptimizeIteration,
    SkillDevEventType,
    SkillDevStage,
    TriggerEvalQuery,
)
from jiuwenclaw.agentserver.skilldev.utils.skill_md_validation import (
    DESCRIPTION_MAX_WEIGHTED,
    truncate_description_to_weighted_limit,
)
from jiuwenclaw.agentserver.skilldev.stages.base import StageHandler, StageResult
from jiuwenclaw.agentserver.skilldev.stages.validate_stage import (
    parse_skill_frontmatter,
)

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 3
HOLDOUT_RATIO = 0.4
_EVAL_CONCURRENCY = 6

# ---------------------------------------------------------------------------
# Prompts（内化自官方 improve_description.py 的 prompt 结构）
# ---------------------------------------------------------------------------

TRIGGER_QUERY_GEN_PROMPT = """\
你是一个 Skill 触发优化专家。根据以下 Skill 的名称和描述，生成 10 个测试查询。

Skill 名称: {skill_name}
当前 Description: {description}

## 要求

### should_trigger=true 的查询（约 5 个）
- 用户确实需要这个 Skill 时会说的话
- 不同表达风格（正式/随意/简短/详细）
- 有些不直接提及 Skill 名称但确实需要其功能
- 包含具体细节（文件路径、个人背景、数据名称等）

### should_trigger=false 的查询（约 5 个）
- 关键词相近但实际不需要这个 Skill 的 **近似场景**
- 相邻领域、歧义措辞、看似相关但应由其他工具处理
- 不要用明显无关的查询（"写斐波那契函数"对 PDF 技能来说太容易区分了）

输出 JSON 数组：
[{{"query": "具体的用户查询", "should_trigger": true}}, ...]
"""

IMPROVE_DESC_PROMPT = """\
你正在优化一个名为 "{skill_name}" 的 Skill 的 description 字段。
description 出现在模型的 available_skills 列表中，模型仅凭 description 决定是否使用该 Skill。

当前 description：
"{current_description}"

当前得分：{scores_summary}

{failure_details}

{history_section}

## 要求

根据失败案例，写一个更好的 description：
- 从失败中 **泛化**，不要过拟合到具体查询
- 用祈使句（"Use when..." 而非 "This skill does..."）
- 聚焦用户意图而非实现细节
- 让触发场景具体且可区分
- 严格不超过加权长度 {max_len}（中文计2、英文计1；纯中文≤512，纯英文≤1024），且 token ≤300（中文 0.6 / 英文 0.3）

请在 <new_description> 标签中只输出新的 description 文本：
<new_description>新描述内容</new_description>
"""

TRIGGER_EVAL_PROMPT = """\
你是一个 Skill 触发判定器。请根据用户查询与 skill description，判断该 skill 是否应该被触发。

Skill description:
"{description}"

用户查询:
"{query}"

输出要求：
- 仅输出一个 JSON 对象
- 不要 Markdown，不要解释文本
- JSON 结构必须是：
{{
  "triggered": true/false,
  "reason": "一句话原因，简短即可"
}}
"""


@dataclass
class _OptimizationLoopInput:
    """描述优化循环的输入参数封装."""

    skill_name: str
    skill_body: str
    current_desc: str
    train_set: list[TriggerEvalQuery]
    test_set: list[TriggerEvalQuery]


@dataclass
class _ImproveDescriptionInput:
    """描述改进步骤的输入参数封装."""

    skill_name: str
    skill_body: str
    current_desc: str
    train_results: list[dict]
    history: list[DescOptimizeIteration]


class DescOptimizeStageHandler(StageHandler):
    """DESC_OPTIMIZE 阶段：优化 SKILL.md 的 description 以提高触发准确率."""

    async def execute(self, ctx: SkillDevContext) -> StageResult:
        logger.info("[session=%s] [DescOptimizeStage] 开始进行描述优化阶段", ctx.state.task_id)

        skill_dir = ctx.workspace / "skill"
        skill_md = skill_dir / "SKILL.md"

        if not skill_md.exists():
            await ctx.emit(
                SkillDevEventType.PROGRESS, {"message": "未找到 SKILL.md，跳过描述优化"}
            )
            return StageResult(next_stage=SkillDevStage.PACKAGE)

        skill_name, current_desc, body = parse_skill_frontmatter(skill_md)

        # Step 1: 生成触发测试查询
        await ctx.emit(
            SkillDevEventType.PROGRESS, {"message": "正在生成触发测试查询集..."}
        )
        logger.info("[session=%s] [DescOptimizeStage] 生成触发测试查询集", ctx.state.task_id)

        queries = await self._generate_trigger_queries(ctx, skill_name, current_desc)

        # Step 2: Train/test split
        train_set, test_set = self._split_eval_set(queries, HOLDOUT_RATIO)

        await ctx.emit(
            SkillDevEventType.PROGRESS,
            {
                "message": f"开始描述优化循环（train={len(train_set)}, test={len(test_set)}）",
            },
        )
        logger.info("[session=%s] [DescOptimizeStage] 开始描述优化循环", ctx.state.task_id)
        # Step 3: 优化循环
        loop_input = _OptimizationLoopInput(
            skill_name=skill_name,
            skill_body=body,
            current_desc=current_desc,
            train_set=train_set,
            test_set=test_set,
        )
        best_desc, history = await self._optimization_loop(ctx, loop_input)

        # Step 4: 写回 SKILL.md
        if best_desc and best_desc != current_desc:
            self._apply_description(skill_md, current_desc, best_desc)

        # Step 5: 结果
        best_iter = (
            max(history, key=lambda h: h.test_passed or 0)
            if test_set and history
            else (max(history, key=lambda h: h.train_passed) if history else None)
        )
        result = {
            "original_description": current_desc,
            "best_description": best_desc,
            "best_score": f"{best_iter.test_passed}/{best_iter.test_total}"
            if best_iter and best_iter.test_passed is not None
            else (
                f"{best_iter.train_passed}/{best_iter.train_total}"
                if best_iter
                else "N/A"
            ),
            "iterations_run": len(history),
            "history": [h.to_dict() for h in history],
        }
        ctx.state.desc_optimize_result = result

        await ctx.emit(SkillDevEventType.DESC_OPT_READY, result)
        return StageResult(next_stage=SkillDevStage.PACKAGE)

    # ------------------------------------------------------------------
    # 生成触发测试查询
    # ------------------------------------------------------------------

    async def _generate_trigger_queries(
        self,
        ctx: SkillDevContext,
        skill_name: str,
        description: str,
    ) -> list[TriggerEvalQuery]:
        """调用 Agent 生成 ~20 个触发测试查询."""
        prompt = TRIGGER_QUERY_GEN_PROMPT.format(
            skill_name=skill_name,
            description=description,
        )
        agent = ctx.create_stage_agent(
            stage_name="desc_optimize_query_gen",
            system_prompt=(
                "你是严谨的 JSON 生成器。只输出符合要求的 JSON，"
                "不要输出任何额外文本。"
            ),
            tools=["file_read"],
            whitelist_key="desc_optimize",
            max_iterations=20,
        )
        output = await ctx.run_stage_agent_streaming(
            agent,
            stage_name="desc_optimize",
            query=prompt,
        )
        parsed = self._parse_json_candidate(output)
        queries = self._normalize_trigger_queries(parsed, skill_name)
        if len(queries) < 4:
            logger.warning(
                "[session=%s] [DescOptimizeStage] generated query set too small, fallback to defaults. size=%d",
                ctx.state.task_id,
                len(queries),
            )
            queries = self._default_trigger_queries(skill_name)
        ctx.release_agent_tools(agent)
        return queries[:10] if len(queries) > 10 else queries

    # ------------------------------------------------------------------
    # Train/test split（内化自官方 run_loop.py 的 split_eval_set）
    # ------------------------------------------------------------------

    @staticmethod
    def _split_eval_set(
        queries: list[TriggerEvalQuery],
        holdout: float,
        seed: int = 42,
    ) -> tuple[list[TriggerEvalQuery], list[TriggerEvalQuery]]:
        """按 should_trigger 分层切分 train/test."""
        rng = random.Random(seed)

        trigger = [q for q in queries if q.should_trigger]
        no_trigger = [q for q in queries if not q.should_trigger]
        rng.shuffle(trigger)
        rng.shuffle(no_trigger)

        n_t = max(1, int(len(trigger) * holdout))
        n_nt = max(1, int(len(no_trigger) * holdout))

        test = trigger[:n_t] + no_trigger[:n_nt]
        train = trigger[n_t:] + no_trigger[n_nt:]
        return train, test

    # ------------------------------------------------------------------
    # 优化循环（内化自官方 run_loop.py 的核心逻辑）
    # ------------------------------------------------------------------

    async def _optimization_loop(
        self,
        ctx: SkillDevContext,
        loop_input: _OptimizationLoopInput,
    ) -> tuple[str, list[DescOptimizeIteration]]:
        """运行 eval → improve 循环，返回 (best_description, history)."""
        skill_name = loop_input.skill_name
        skill_body = loop_input.skill_body
        current_desc = loop_input.current_desc
        train_set = loop_input.train_set
        test_set = loop_input.test_set
        history: list[DescOptimizeIteration] = []

        for i in range(1, MAX_ITERATIONS + 1):
            await ctx.emit(
                SkillDevEventType.PROGRESS,
                {
                    "message": f"描述优化第 {i}/{MAX_ITERATIONS} 轮...",
                },
            )
            logger.info("[session=%s] [DescOptimizeStage] 第 %d 轮描述优化", ctx.state.task_id, i)

            # 评估 train + test
            train_results = await self._eval_description(
                ctx, current_desc, train_set, batch_label=f"iter{i}/train",
            )
            test_results = (
                await self._eval_description(
                    ctx, current_desc, test_set, batch_label=f"iter{i}/test",
                )
                if test_set
                else None
            )

            train_passed = sum(1 for r in train_results if r["pass"])
            iteration = DescOptimizeIteration(
                iteration=i,
                description=current_desc,
                train_passed=train_passed,
                train_total=len(train_set),
                test_passed=sum(1 for r in test_results if r["pass"])
                if test_results
                else None,
                test_total=len(test_set) if test_results else None,
            )
            history.append(iteration)

            # 全部通过则提前退出
            if train_passed == len(train_set):
                break

            # 最后一轮不再改进
            if i == MAX_ITERATIONS:
                break

            # 改进 description
            improve_input = _ImproveDescriptionInput(
                skill_name=skill_name,
                skill_body=skill_body,
                current_desc=current_desc,
                train_results=train_results,
                history=history,
            )
            current_desc = await self._improve_description(ctx, improve_input)

        # 选 test score 最高的（防过拟合）
        if test_set:
            best = max(history, key=lambda h: h.test_passed or 0)
        else:
            best = max(history, key=lambda h: h.train_passed)
        return best.description, history

    # ------------------------------------------------------------------
    # 单次评估：判断 description 对一组 queries 是否触发
    # ------------------------------------------------------------------

    async def _eval_description(
        self,
        ctx: SkillDevContext,
        description: str,
        queries: list[TriggerEvalQuery],
        batch_label: str = "",
    ) -> list[dict]:
        """对每个 query，并发调用模型判断当前 description 是否会触发.

        策略：
        - 并发执行 LLM 调用（Semaphore 限流），但**关闭** AGENT_THINKING /
          AGENT_OUTPUT 的实时推送，避免多任务的 chunk 在前端按粒度交织。
        - 每条 query 在 LLM 完成后，将完整的 thinking + output 缓存在本地。
        - 通过 Event 链按 **输入顺序** 依次将缓存的 thinking 与 output 作为
          单个 delta emit 到前端，前端因此感知到的是与原串行版本完全一致的
          "思考过程 + JSON 输出" 顺序卡片序列。
        - 最终 results 列表的顺序与输入 queries 顺序保持一致。
        """
        if not queries:
            return []
        start_ts = perf_counter()
        fallback_count = 0
        total = len(queries)

        semaphore = asyncio.Semaphore(_EVAL_CONCURRENCY)
        ordered_results: list[dict | None] = [None] * total
        label_prefix = f"{batch_label}/" if batch_label else ""
        # flush_events[i] 在第 i 条 query 的输出已推送到前端后被 set；
        # 第 i+1 条必须等待 flush_events[i] 才能开始推送自己的内容，
        # 由此形成严格的输入顺序展示链。
        flush_events: list[asyncio.Event] = [asyncio.Event() for _ in range(total)]

        async def _bounded(idx: int, q: TriggerEvalQuery) -> None:
            nonlocal fallback_count
            stage_name = f"desc_optimize_eval/{label_prefix}{idx}"
            async with semaphore:
                logger.info(
                    "[session=%s] [DescOptimizeStage] 开始评估单条查询: %s",
                    ctx.state.task_id, q.query,
                )
                triggered, thinking_text, output_text = await self._test_single_trigger(
                    ctx, description, q.query, stage_name=stage_name,
                )
                logger.info(
                    "[session=%s] [DescOptimizeStage] 评估单条查询完成: %s",
                    ctx.state.task_id, q.query,
                )
                if triggered is None:
                    fallback_count += 1
                    triggered = False
                passed = triggered == q.should_trigger
                ordered_results[idx] = {
                    "query": q.query,
                    "should_trigger": q.should_trigger,
                    "triggered": triggered,
                    "pass": passed,
                }

            # 等待前序 query 完成展示（首条无需等待）；
            # semaphore 已释放，等待期间不占用并发槽位，下一批 LLM 调用可继续。
            if idx > 0:
                await flush_events[idx - 1].wait()

            if thinking_text:
                await ctx.emit(
                    SkillDevEventType.AGENT_THINKING,
                    {"delta": thinking_text, "stage": stage_name},
                )
            if output_text:
                await ctx.emit(
                    SkillDevEventType.AGENT_OUTPUT,
                    {"delta": output_text, "stage": stage_name},
                )
            flush_events[idx].set()

        await asyncio.gather(*[_bounded(i, q) for i, q in enumerate(queries)])

        results: list[dict] = [r for r in ordered_results if r is not None]
        elapsed_ms = int((perf_counter() - start_ts) * 1000)
        logger.info(
            "[session=%s] [DescOptimizeStage] eval batch done. size=%d elapsed_ms=%d fallback_count=%d",
            ctx.state.task_id,
            len(queries),
            elapsed_ms,
            fallback_count,
        )
        return results

    # ------------------------------------------------------------------
    # 改进 description（内化自官方 improve_description.py 的 prompt 结构）
    # ------------------------------------------------------------------

    async def _improve_description(
        self,
        ctx: SkillDevContext,
        improve_input: _ImproveDescriptionInput,
    ) -> str:
        """调用模型基于失败案例改进 description."""
        failed_cases = [r for r in improve_input.train_results if not r["pass"]]
        if not failed_cases:
            return improve_input.current_desc

        should_count = sum(1 for r in improve_input.train_results if r["should_trigger"])
        scores_summary = (
            f"train pass {sum(1 for r in improve_input.train_results if r['pass'])}"
            f"/{len(improve_input.train_results)}; "
            f"should_trigger samples: {should_count}, "
            f"should_not_trigger samples: {len(improve_input.train_results) - should_count}"
        )

        failure_details = self._build_failure_details(failed_cases)
        history_section = self._build_history_section(improve_input.history)

        prompt = IMPROVE_DESC_PROMPT.format(
            skill_name=improve_input.skill_name,
            current_description=improve_input.current_desc,
            scores_summary=scores_summary,
            failure_details=failure_details,
            history_section=history_section,
            max_len=DESCRIPTION_MAX_WEIGHTED,
        )
        agent = ctx.create_stage_agent(
            stage_name="desc_optimize_improve",
            system_prompt=(
                "你是严格遵循输出格式的 description 优化器。"
                "必须在 <new_description> 标签中输出结果。"
            ),
            tools=["file_read"],
            whitelist_key="desc_optimize",
            max_iterations=20,
        )
        output = await ctx.run_stage_agent_streaming(
            agent,
            stage_name="desc_optimize",
            query=prompt,
        )
        new_desc = self._extract_tag_content(output, "new_description")
        if not new_desc:
            logger.warning(
                "[session=%s] [DescOptimizeStage] improve output missing "
                "<new_description>",
                ctx.state.task_id,
            )
            ctx.release_agent_tools(agent)
            return improve_input.current_desc
        new_desc = self._normalize_description_text(new_desc)
        if not new_desc:
            ctx.release_agent_tools(agent)
            return improve_input.current_desc
        new_desc = truncate_description_to_weighted_limit(
            new_desc, DESCRIPTION_MAX_WEIGHTED
        )
        ctx.release_agent_tools(agent)
        return new_desc

    # ------------------------------------------------------------------
    # 将优化后的 description 写回 SKILL.md
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_description(skill_md: Path, old_desc: str, new_desc: str) -> None:
        """替换 SKILL.md frontmatter 中的 description 字段."""
        content = skill_md.read_text(encoding="utf-8")

        match = re.match(r"^(---\n)(.*?)(\n---)", content, re.DOTALL)
        if not match:
            return

        frontmatter = match.group(2)
        # 替换 description 行（简单场景：单行 description: xxx）
        new_fm = re.sub(
            r"(description:\s*).*",
            rf"\g<1>{new_desc}",
            frontmatter,
            count=1,
        )
        new_content = match.group(1) + new_fm + match.group(3) + content[match.end():]
        skill_md.write_text(new_content, encoding="utf-8")

    async def _test_single_trigger(
        self,
        ctx: SkillDevContext,
        description: str,
        query: str,
        stage_name: str = "desc_optimize_eval",
    ) -> tuple[bool | None, str, str]:
        """对单条 query 进行是否触发判定.

        Args:
            stage_name: agent 的 conversation_id 后缀及对外 emit 时使用的 stage 标识。
                并发场景下必须为每条 query 传入唯一值，避免协程共享会话产生历史污染。

        Returns:
            ``(triggered, thinking_text, output_text)`` 三元组：

            - ``triggered``：模型对该 query 的触发判定（解析失败时为 ``None``）
            - ``thinking_text``：模型完整推理文本（供调用方按输入顺序回放到前端）
            - ``output_text``：模型完整正文输出（同上）

            实时流式推送已通过 ``emit_thinking=False`` / ``emit_output=False`` 抑制；
            调用方拿到完整文本后，自行决定何时按何种顺序 emit 给前端，从而实现
            "并行执行、串行展示" 的效果。
        """
        eval_prompt = TRIGGER_EVAL_PROMPT.format(
            description=description,
            query=query,
        )
        eval_agent = self._create_eval_agent(ctx, stage_name=stage_name)
        try:
            output, thinking = await ctx.run_stage_agent_streaming(
                eval_agent,
                stage_name=stage_name,
                query=eval_prompt,
                emit_thinking=False,
                emit_output=False,
                capture_thinking=True,
            )
        except Exception:
            logger.exception(
                "[session=%s] [DescOptimizeStage] trigger eval failed, fallback false. query=%s",
                ctx.state.task_id,
                query[:60],
            )
            ctx.release_agent_tools(eval_agent)
            return None, "", ""
        parsed = self._parse_json_candidate(output)
        if isinstance(parsed, dict):
            triggered = parsed.get("triggered")
            if isinstance(triggered, bool):
                ctx.release_agent_tools(eval_agent)
                return triggered, thinking, output
        logger.warning(
            "[session=%s] [DescOptimizeStage] trigger eval parse failed, fallback false. query=%s output=%s",
            ctx.state.task_id,
            query[:60],
            output[:200],
        )
        ctx.release_agent_tools(eval_agent)
        return None, thinking, output

    @staticmethod
    def _create_eval_agent(
        ctx: SkillDevContext,
        stage_name: str = "desc_optimize_eval",
    ):
        """创建 desc_optimize 阶段的触发判定 agent.

        stage_name 同时决定 agent 的 conversation_id（见 context.run_stage_agent_streaming），
        因此并发场景下必须传入唯一值，避免多协程共享会话造成历史污染。
        """
        return ctx.create_stage_agent(
            stage_name=stage_name,
            system_prompt=(
                "你是 JSON 判定器。只输出 JSON 对象。"
                '字段: {"triggered": boolean, "reason": string}'
            ),
            tools=["file_read"],
            whitelist_key="desc_optimize",
            max_iterations=8,
        )

    @staticmethod
    def _parse_json_candidate(text: str):
        """从文本中提取 JSON（代码块优先，其次平衡大括号）。"""
        if not text:
            return None
        code_block = re.search(r"```(?:json)?\s*(\[.*]|\{.*})\s*```", text, re.DOTALL)
        if code_block:
            try:
                return json.loads(code_block.group(1))
            except json.JSONDecodeError:
                pass

        start_obj = text.find("{")
        start_arr = text.find("[")
        starts = [i for i in (start_obj, start_arr) if i != -1]
        if not starts:
            return None
        start = min(starts)
        stack: list[str] = []
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch in "{[":
                stack.append(ch)
            elif ch in "}]":
                if not stack:
                    continue
                left = stack.pop()
                expected_right = "}" if left == "{" else "]"
                is_mismatch = ch != expected_right
                if is_mismatch:
                    continue
                if not stack:
                    candidate = text[start: i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break
        return None

    @staticmethod
    def _default_trigger_queries(skill_name: str) -> list[TriggerEvalQuery]:
        return [
            TriggerEvalQuery(
                query=f"请帮我用 {skill_name} 完成一个复杂任务，并给出可执行步骤。",
                should_trigger=True,
            ),
            TriggerEvalQuery(
                query=f"我需要一个和 {skill_name} 相关的完整解决方案，包含输入输出约束。",
                should_trigger=True,
            ),
            TriggerEvalQuery(
                query="帮我写一个快速排序的 Python 实现并解释时间复杂度。",
                should_trigger=False,
            ),
            TriggerEvalQuery(
                query="给我一段正则表达式，用来提取邮箱地址。",
                should_trigger=False,
            ),
        ]

    def _normalize_trigger_queries(
        self,
        raw,
        skill_name: str,
    ) -> list[TriggerEvalQuery]:
        """清洗并规范化 query 列表，确保正负样本都存在。"""
        items = raw if isinstance(raw, list) else []
        normalized: list[TriggerEvalQuery] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            query = str(item.get("query", "")).strip()
            should_trigger = item.get("should_trigger")
            if not query or not isinstance(should_trigger, bool):
                continue
            dedupe_key = f"{int(should_trigger)}::{query}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            normalized.append(
                TriggerEvalQuery(query=query, should_trigger=should_trigger)
            )

        has_true = any(q.should_trigger for q in normalized)
        has_false = any(not q.should_trigger for q in normalized)
        if not has_true or not has_false:
            defaults = self._default_trigger_queries(skill_name)
            for d in defaults:
                if d.should_trigger and has_true:
                    continue
                if (not d.should_trigger) and has_false:
                    continue
                normalized.append(d)
                has_true = has_true or d.should_trigger
                has_false = has_false or (not d.should_trigger)
                if has_true and has_false:
                    break
        return normalized

    @staticmethod
    def _build_failure_details(failed_cases: list[dict]) -> str:
        lines = ["失败样本（仅列出前 10 条）："]
        for idx, case in enumerate(failed_cases[:10], start=1):
            lines.append(
                f"{idx}. should_trigger={case.get('should_trigger')} "
                f"triggered={case.get('triggered')} "
                f"query={case.get('query')}"
            )
        return "\n".join(lines)

    @staticmethod
    def _build_history_section(history: list[DescOptimizeIteration]) -> str:
        if not history:
            return "历史迭代：无。"
        lines = ["历史迭代摘要："]
        for h in history[-3:]:
            if h.test_passed is None:
                score = f"train={h.train_passed}/{h.train_total}"
            else:
                score = (
                    f"train={h.train_passed}/{h.train_total}, "
                    f"test={h.test_passed}/{h.test_total}"
                )
            lines.append(f"- iter {h.iteration}: {score}")
        return "\n".join(lines)

    @staticmethod
    def _extract_tag_content(text: str, tag: str) -> str:
        pattern = rf"<{tag}>(.*?)</{tag}>"
        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if not match:
            return ""
        return match.group(1).strip()

    @staticmethod
    def _normalize_description_text(text: str) -> str:
        """清洗 description 文本，压缩多余空白并保持单行。"""
        cleaned = re.sub(r"\s+", " ", text or "").strip()
        return cleaned
