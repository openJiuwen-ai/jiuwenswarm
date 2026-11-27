# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""EVALUATE 阶段处理器.

职责分三步，对齐官方 skill-creator 的评测流程：

1. Grader 评分 — 对每个 eval run 的 transcript + outputs 逐 assertion 评分
   输出 grading.json（expectations[].text/passed/evidence 格式）

2. Benchmark 聚合 — 遍历所有 grading.json，计算 per-config 的 mean/stddev/min/max
   输出 benchmark.json（前端根据此数据渲染 Benchmark 面板）

3. Analyst 分析 — 发现 aggregate stats 隐藏的模式
   输出 notes 列表（前端展示为分析摘要）

最终推送 EVAL_READY 事件 → 进入 REVIEW 挂起点（前端展示评测结果供用户审阅）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import logging
import math
import re
from pathlib import Path
from typing import Any, Dict

from jiuwenclaw.agentserver.skilldev.asset_utils import load_skill_content, preload_files_content, load_asset
from jiuwenclaw.agentserver.skilldev.common_utils import strip_agent_output_noise
from jiuwenclaw.agentserver.skilldev.context import SkillDevContext
from jiuwenclaw.agentserver.skilldev.schema import (
    Benchmark,
    BenchmarkRun,
    GradingExpectation,
    GradingResult,
    MetricStats,
    SkillDevEventType,
    SkillDevStage,
    _now_iso,
)
from jiuwenclaw.agentserver.skilldev.stages.base import StageHandler, StageResult

logger = logging.getLogger(__name__)


@dataclass
class GradingAgentRequest:
    """Agent 评分请求上下文."""

    expectations: list
    output: str
    stage_name: str
    trace: str = ""
    run_dir: Path | None = None
    input_files: list = field(default_factory=list)
    files_created: list = field(default_factory=list)
    # 预加载的产出文件内容：{filename: content}，由 _grade_one 在创建 agent 前填充
    files_content: dict = field(default_factory=dict)
    # 未能内联的文件名列表（二进制/超限/不存在），grader 只对这些文件允许工具调用
    files_failed: list = field(default_factory=list)
    # 上次 JSON 解析失败的错误信息；非空时 prompt 开头会注入强力格式警告
    parse_error_hint: str = ""

# ---------------------------------------------------------------------------
# Grader Agent 系统 Prompt
# 核心规则吸收自官方 agents/grader.md，不是外部文件引用
# ---------------------------------------------------------------------------

GRADER_SYSTEM_PROMPT = """\
你是一个严格的评测 Grader。根据 Agent 的实际执行结果逐条判断每个 expectation 是否被真实满足。

## 评分工作流（必须按此顺序执行）

### Step 1 — 识别 expectation 类型
对每条 expectation，先判断类型：

- **FILE 类**：涉及文件存在性、文件内容、目录结构、文件数量等
  示例：“生成了 report.pdf”，“文件包含正确标题”，“在 output/ 下创建了 3 个文件”
- **OUTPUT 类**：可从 Agent 最终文本输出或执行轨迹直接判断
  示例：“返回了正确数字”，“输出中包含摘要”

### Step 2 — 收集证据（不可跳过）

**FILE 类 expectation：**

> **优先级规则（按顺序检查，满足即止）：**
>
> 1. **若 prompt 中「Agent 未在该目录创建任何额外文件」** → 所有 FILE 类 expectation 直接判 FAIL
> 2. **若 prompt 末尾「产出文件内容（已预加载）」已包含该文件内容** → 直接基于预加载内容判断，禁止再调用文件工具
> 3. **若文件在预加载列表中但标注为二进制/过大/不存在** → 才允许使用文件工具（`read_file` / `glob` / `list_files`）读取后判断
> 4. **若文件完全未出现在预加载列表且未创建文件说明** → 直接判 FAIL

- 禁止仅凭文本输出推断文件是否存在或内容是否正确

**OUTPUT 类 expectation：**
- 优先看最终输出内容
- 若涉及执行过程或轨迹，结合执行轨迹（trace.txt）判断

**工具调用失败时的降级规则（重要）：**
- 若工具调用尝试后无响应，**不得重复尝试同一工具，不得无限循环等待**
- 立即根据已有信息（文本输出 + 执行轨迹 + 预加载文件内容）做出最佳判断
- 在 reason 中注明："无法调用文件工具，依据文本/轨迹推断"
- 宁可给出一个有据可查的推断结果，也不能因为等待工具而不输出任何评分

### Step 3 — 评分

**PASS**：有具体、可验证的证据表明 expectation 被完整满足
**FAIL**：证据缺失、与 expectation 矛盾、无法确认、或 Agent 执行未完成

## 输出格式 — 必须严格遵守

确保最终输出为 **JSON** 列表，每个元素对应一条 expectation：
```json
[
  {
    "expectation": "原始 expectation 文本（逐字复制，不得改动）",
    "passed": true,
    "reason": "简要说明证据（1-2 句；FILE 类须注明调用了哪个工具及结果）"
  },
  ...
]
```

**要求：**
1. 最终输出必须是一个合法的 JSON 列表
2. 列表中的每个元素必须为字典且有三个字段：expectation, passed, reason
3. passed 字段只能是 true 或 false（布尔值，不是字符串）
4. expectation 字段必须逐字复制原始文本，不得改动
5. reason 字段须遵守以下规则：
   - **禁止出现双引号**（`"`）—— 若需引用内容，改用单引号（`'`）或【】括号
   - **禁止出现换行符** —— reason 必须是单行文本
   - 禁止使用 Markdown 语法（禁止 `**`、`` ` ``、`#` 等）
   - 长度控制在 1-2 句，50 字以内
6. 数组长度必须等于输入的 expectation 数量，顺序与输入一致

**常见格式错误（禁止出现）：**
- reason 中含双引号：`"文件包含 "hello""` → 改为 `"文件包含 'hello'"`
- reason 中含换行：`"第一行\n第二行"` → 改为 `"第一行，第二行"`
"""

# ---------------------------------------------------------------------------
# Analyst Agent 系统 Prompt
# 核心规则吸收自官方 agents/analyzer.md，不是外部文件引用
# ---------------------------------------------------------------------------

ANALYST_SYSTEM_PROMPT = """\
你是一个 Skill 改进顾问。根据评测数据和当前 Skill 文件内容，生成**具体的、可落地的改进建议**。

## 分析方法（按此顺序思考，不要跳步骤）

### Step 1：理解 Skill 当前设计
阅读 prompt 中「当前 Skill 文件内容」部分，明确：
- Skill 的触发场景和核心指令
- 执行步骤与输出格式要求
- 有无 scripts/ references/ 等辅助文件

### Step 2：定位每条失败 expectation 的根因
对「各测试用例详情」中每条 FAIL 的 expectation：
1. 阅读 grader 给出的 reason
2. 结合该 case 的用户 prompt，判断失败类型：
   - **Skill 指令缺失**：SKILL.md 未覆盖该场景或输出要求，需补充步骤/约束
   - **Skill 指令歧义**：措辞不清导致模型走错路径，需重写该段指令
   - **Flaky 行为**：部分 run pass 部分 fail，行为不稳定，需加强确定性约束
   - **能力上限**：任务超出模型能力，Skill 无法解决，建议调整 expectation 或任务边界
   - **测试设计问题**：expectation 本身主观/无法客观验证，根因在测试而非 Skill

### Step 3：对比 with_skill vs baseline
- 哪些指标 with_skill 明显优于 baseline？列出有效贡献
- 哪些指标 with_skill **劣于** baseline？这是 Skill 引入的负向影响，必须优先修复
- fully_passed_count 是否有改善？

### Step 4：写出可落地的建议
每条建议必须说明：
- **改哪里**：SKILL.md 的哪个段落 / scripts/ 哪个文件 / references/ 哪个文档
- **改成什么**：具体的修改方向或示例措辞，不允许"优化相关指令"这类空话
- **预期收益**：改完后哪条 expectation 预期会改善

## 输出格式：严格 JSON 数组

```json
[
  {
    "category": "Skill指令缺失 | Skill指令歧义 | Flaky行为 | 性能问题 | 能力上限 | 测试设计问题 | 其他",
    "issue": "具体问题——引用 grader reason 和 eval prompt 说明现象",
    "suggestion": "具体改法——指出文件位置和修改内容",
    "priority": "high | medium | low"
  }
]
```

**约束：**
- 最多输出 8 条，按 priority 从高到低排序。
- 若无修改建议，输出“[无建议]”。
- 能力上限类只需说明边界，不要给出无法实现的建议
- 建议必须基于实际的 grader reason，禁止凭空推测
- issue和suggestion 字段须遵守以下规则：
   - **禁止出现双引号**（`"`）—— 若需引用内容，改用单引号（`'`）或【】括号
   - **禁止出现换行符** —— reason 必须是单行文本
   - 禁止使用 Markdown 语法（禁止 `**`、`` ` ``、`#` 等）
   - 长度控制在 1-2 句，50 字以内
"""


class EvaluateStageHandler(StageHandler):
    """EVALUATE 阶段：Grader 评分 → Benchmark 聚合 → Analyst 分析."""

    async def execute(self, ctx: SkillDevContext) -> StageResult:
        iteration = ctx.state.iteration
        iter_dir = ctx.workspace / "evals" / f"iteration-{iteration}"

        # --- Step 1: Grader 评分 ---
        await ctx.emit(
            SkillDevEventType.PROGRESS, {"message": "正在对测试结果进行评分..."}
        )
        try:
            await self._grade_all_evals(ctx, iter_dir)
        except Exception as e:
            msg = f"EVALUATE 评分执行失败：{e}"
            await ctx.emit(SkillDevEventType.ERROR, {"message": msg, "stage": "evaluate"})
            raise RuntimeError(msg) from e

        # --- Step 2: Benchmark 聚合 ---
        await ctx.emit(
            SkillDevEventType.PROGRESS, {"message": "正在聚合 benchmark 统计..."}
        )
        benchmark = self._aggregate_benchmark(ctx, iter_dir)

        # --- Step 3: Analyst 分析 ---
        await ctx.emit(SkillDevEventType.PROGRESS, {"message": "正在分析评测模式..."})
        try:
            analyst_notes = await self._analyze_patterns(ctx, benchmark)
        except Exception as e:
            msg = f"EVALUATE Analyst 分析失败：{e}"
            await ctx.emit(SkillDevEventType.ERROR, {"message": msg, "stage": "evaluate"})
            raise RuntimeError(msg) from e
        benchmark.notes = analyst_notes

        # 持久化
        benchmark_dict = benchmark.to_dict()
        report_md = self._render_benchmark_md(benchmark)
        try:
            (iter_dir / "benchmark.json").write_text(
                json.dumps(benchmark_dict, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (iter_dir / "benchmark.md").write_text(report_md, encoding="utf-8")
        except OSError as e:
            msg = f"EVALUATE benchmark 文件写入失败：{e}"
            await ctx.emit(SkillDevEventType.ERROR, {"message": msg, "stage": "evaluate"})
            raise RuntimeError(msg) from e

        ctx.state.eval_results = {"benchmark": benchmark_dict, "report": report_md}

        # 推送给前端 — 前端根据 benchmark JSON 渲染评测面板
        await ctx.emit(
            SkillDevEventType.EVAL_READY,
            {
                "benchmark": benchmark_dict,
                "iteration": iteration,
            },
        )
        return StageResult(next_stage=SkillDevStage.REVIEW)

    # ------------------------------------------------------------------
    # Step 1: Grader
    # ------------------------------------------------------------------

    # 并发 grading 任务数上限，避免同时发起过多 LLM 调用
    _GRADE_CONCURRENCY = 4
    # 单次 grading 失败后的最大重试次数（不含首次）
    _MAX_GRADE_RETRIES = 3
    # 单次 grading 调用的超时秒数（防止网络挂起导致整个 stage 无限等待）
    _GRADE_TIMEOUT_SECONDS = 300

    async def _grade_all_evals(self, ctx: SkillDevContext, iter_dir: Path) -> None:
        """为每个 eval 的 with_skill / baseline 结果并行执行评分.

        每个 (case, variant) 对独立创建 agent 实例，避免共享 conversation_id 导致的
        历史污染。通过 Semaphore 控制并发数，防止同时发起过多 LLM 请求。
        """
        evals = (ctx.state.evals or {}).get("evals", [])

        # 收集所有需要评分的 (case, variant, run_dir) 三元组
        tasks: list[tuple[dict, str, Path]] = []
        for case in evals:
            eval_name = case.get("name", f"eval-{case.get('id', 0)}")
            case_dir = iter_dir / eval_name
            if not case_dir.exists():
                continue
            for variant in ("with_skill", "baseline"):
                run_dir = case_dir / variant
                result_file = run_dir / "result.json"
                tasks.append((case, variant, run_dir))

        if not tasks:
            logger.warning("[session=%s] [EvaluateStage] 没有可评分的 run，跳过 grading", ctx.state.task_id)
            return

        semaphore = asyncio.Semaphore(self._GRADE_CONCURRENCY)

        completed = 0

        async def _bounded(case: dict, variant: str, run_dir: Path) -> None:
            nonlocal completed
            eval_name = case.get("name", f"eval-{case.get('id', 0)}")
            async with semaphore:
                # 开始信号：前端据此创建 per-case 评分卡片
                await ctx.emit(
                    SkillDevEventType.PROGRESS,
                    {
                        "message": f"开始评分: {eval_name}/{variant}",
                        "eval_name": eval_name,
                        "variant": variant,
                    },
                )
                await self._grade_one(ctx, case, variant, run_dir)
                completed += 1
                # 完成信号：前端据此结束卡片流式状态
                await ctx.emit(
                    SkillDevEventType.PROGRESS,
                    {
                        "message": f"已评分 {completed}/{len(tasks)} 个测试结果",
                        "eval_name": eval_name,
                        "variant": variant,
                        "case_done": True,
                        "completed": completed,
                        "total": len(tasks),
                    },
                )

        await asyncio.gather(*[_bounded(c, v, d) for c, v, d in tasks])

    async def _grade_one(
        self, ctx: SkillDevContext, case: dict, variant: str, run_dir: Path
    ) -> None:
        """为单个 (case, variant) 执行评分并写入 grading.json."""
        eval_name = case.get("name", f"eval-{case.get('id', 0)}")
        expectations = case.get("expectations", [])

        result_data = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        output = result_data.get("output", "")
        files_created = result_data.get("files_created", [])
        trace_file = run_dir / "trace.txt"
        trace = trace_file.read_text(encoding="utf-8") if trace_file.exists() else ""

        input_files = case.get("files", [])

        # 清洗输出：去除泄漏的 <think> 推理块和未执行的 <tool_call> 文本标签
        output = strip_agent_output_noise(output)

        # 代码预读 files_created 内容，注入 grader prompt，省去 agent 调用 file_read 工具
        files_content, files_failed = preload_files_content(run_dir, files_created)

        # 带重试的 grading：每次重试创建新 agent（新 stage_name → 新 conversation_id）
        # 避免复用失败会话导致模型沿着错误路径继续
        agent_results: dict | None = None
        base_stage_key = f"evaluate_grader/{eval_name}/{variant}"
        last_parse_error: str = ""   # 上次 JSON 解析失败的原因；非空时下次 attempt 注入格式警告
        last_failure_reason: str = ""  # 最后一次失败的原因，用于最终 raise 的错误信息

        for attempt in range(1, self._MAX_GRADE_RETRIES + 1):
            stage_key = base_stage_key if attempt == 1 else f"{base_stage_key}/retry{attempt}"
            agent = ctx.create_stage_agent(
                stage_name=stage_key,
                whitelist_key="evaluate",
                system_prompt=GRADER_SYSTEM_PROMPT,
                tools=["file_read", "file_grep", "file_listdir", "file_glob", "shell"],
                max_iterations=30,
            )
            grading_request = GradingAgentRequest(
                expectations=expectations,
                output=output,
                trace=trace,
                stage_name=stage_key,
                run_dir=run_dir,
                input_files=input_files,
                files_created=files_created,
                files_content=files_content,
                files_failed=files_failed,
                parse_error_hint=last_parse_error,
            )
            try:
                agent_results = await asyncio.wait_for(
                    self._grade_expectations_with_agent(ctx, agent, grading_request),
                    timeout=self._GRADE_TIMEOUT_SECONDS,
                )
                is_failure = _is_grading_failure(agent_results, expectations)
                last_parse_error = ""  # 本次成功，清除错误标记
            except asyncio.TimeoutError:
                # 单次 grading 超时（网络挂起等）→ 视为本次 attempt 失败，触发重试
                logger.warning(
                    "[session=%s] [EvaluateStage] 评分超时（>%ds），准备重试 (%d/%d): %s/%s",
                    ctx.state.task_id,
                    self._GRADE_TIMEOUT_SECONDS, attempt, self._MAX_GRADE_RETRIES,
                    eval_name, variant,
                )
                await ctx.emit(
                    SkillDevEventType.PROGRESS,
                    {
                        "message": (
                            f"评分超时（>{self._GRADE_TIMEOUT_SECONDS}s），"
                            f"准备重试 ({attempt}/{self._MAX_GRADE_RETRIES}): "
                            f"{eval_name}/{variant}"
                        ),
                        "eval_name": eval_name,
                        "variant": variant,
                    },
                )
                agent_results = None
                is_failure = True
                last_parse_error = ""  # 超时不属于格式问题，清除
                last_failure_reason = f"评分超时（>{self._GRADE_TIMEOUT_SECONDS}s）"
            except ValueError as parse_err:
                # LLM 输出无法解析为合法 JSON → 视为本次 attempt 失败，触发重试
                # 网络 / 超时异常不在此处捕获，会直接传播到 _grade_all_evals → execute()
                logger.warning(
                    "[session=%s] [EvaluateStage] 评分结果解析失败，准备重试 (%d/%d): %s/%s: %s",
                    ctx.state.task_id,
                    attempt, self._MAX_GRADE_RETRIES, eval_name, variant, parse_err,
                )
                await ctx.emit(
                    SkillDevEventType.PROGRESS,
                    {
                        "message": f"评分失败，准备重试 ({attempt}/{self._MAX_GRADE_RETRIES}): {eval_name}/{variant}",
                        "eval_name": eval_name,
                        "variant": variant,
                    },
                )
                agent_results = None
                is_failure = True
                last_parse_error = str(parse_err)[:300]  # 传给下次 attempt 的格式警告
                last_failure_reason = f"评分失败（{str(parse_err)[:100]}）"

            if not is_failure:
                break  # 有效结果，不再重试

            if attempt < self._MAX_GRADE_RETRIES:
                wait = 2 ** (attempt - 1)  # 指数退避：1s, 2s
                logger.warning(
                    "[session=%s] [EvaluateStage] grading 结果无效，%ds 后重试 (%d/%d): %s/%s",
                    ctx.state.task_id,
                    wait, attempt, self._MAX_GRADE_RETRIES, eval_name, variant,
                )
                await asyncio.sleep(wait)
            else:
                logger.error(
                    "[session=%s] [EvaluateStage] grading %d 次重试后仍失败: %s/%s",
                    ctx.state.task_id,
                    self._MAX_GRADE_RETRIES, eval_name, variant,
                )
        ctx.release_agent_tools(agent)
        if agent_results is None:
            raise RuntimeError(
                f"评分 {self._MAX_GRADE_RETRIES} 次重试后仍失败"
                f"（{last_failure_reason}）: {eval_name}/{variant}"
            )

        grading = self._convert_agent_results_to_grading_result(agent_results)
        grading_dict = grading.to_dict() if hasattr(grading, "to_dict") else grading
        (run_dir / "grading.json").write_text(
            json.dumps(grading_dict, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("[session=%s] [EvaluateStage] grading 完成: %s/%s", ctx.state.task_id, eval_name, variant)

    async def _grade_expectations_with_agent(
        self,
        ctx: SkillDevContext,
        agent,
        request: GradingAgentRequest,
    ) -> Dict[str, Any]:
        """使用 Agent 验证 expectations（流式，实时推送推理过程）."""
        # 若上次因 JSON 格式错误失败，在 prompt 最开头注入强力警告
        if request.parse_error_hint:
            prompt = (
                "⚠️【JSON 格式错误警告 - 本次为重试】⚠️\n"
                "上次输出因 JSON 格式错误无法解析，错误信息：\n"
                f"{request.parse_error_hint}\n\n"
                "本次必须严格遵守以下规则，否则评分无效：\n"
                "1. reason 字段禁止出现双引号（用单引号替代）\n"
                "2. reason 字段禁止换行，必须是单行文本\n"
                "3. 禁止在 JSON 外包裹任何解释文字（直接输出 JSON 数组）\n"
                "4. 禁止在字符串值内嵌套引号或特殊转义符\n\n"
            )
        else:
            prompt = ""
        prompt += "请根据以下执行结果，逐条判断 expectations 是否满足：\n\n"
        prompt += "## 待评估的 Expectations：\n"
        for i, exp in enumerate(request.expectations, 1):
            prompt += f"{i}. {exp}\n"
        if request.input_files:
            prompt += "\n## 本次测试的输入文件：\n"
            prompt += "\n".join(f"- {f}" for f in request.input_files) + "\n"
        prompt += f"\n## 最终输出内容：\n{request.output}\n"
        if request.trace:
            prompt += f"\n## 执行轨迹（工具调用记录前3000字）：\n{request.trace}\n"
        if request.run_dir is not None:
            prompt += f"\n## 本次运行的工作目录：{request.run_dir}\n"
            if request.files_created:
                prompt += (
                    f"\n## Agent输出文件\n本次运行Agent 已在该目录中创建的文件："
                    f"{', '.join(request.files_created)}\n"
                )
            else:
                prompt += "Agent 未在该目录创建任何额外文件。\n"
            prompt += (
                "完整输出见 result.json，完整执行轨迹见 trace.txt。\n"
            )

        # 注入预加载的产出文件内容，grader 直接基于内容评分，无需再调用文件工具
        if request.files_content:
            prompt += "\n## 预加载输出文件\n产出文件内容（已预加载，对 FILE 类 expectation 请直接基于以下内容判断，无需调用工具）：\n"
            for fname, content in request.files_content.items():
                prompt += f"\n--- 文件标题：{fname} ---\n文件内容预加载：\n{content}\n文件内容预加载结束\n"
            if request.files_failed:
                prompt += (
                    f"\n【注意】以下文件未能预加载，仅在 expectation 涉及具体文件内容时才允许调用文件工具读取：\n"
                    + "\n".join(
                        f"- {f['path']}（{f['reason']}）" for f in request.files_failed
                    ) + "\n"
                    + "其余已预加载的文件禁止再次调用文件工具。\n"
                )
            else:
                prompt += "\n【重要】所有产出文件内容已完整预加载，**禁止调用任何文件工具**，直接基于上方内容评分。\n"

        prompt += "\n请按照 JSON 格式逐条输出评分结果。"

        # 不捕获 LLM 调用异常（网络断开、超时等）—— 让其向上传播，
        # 使 pipeline 进入 ERROR 状态并通知前端；
        # 只在 JSON 解析层面做容错。
        response = await ctx.run_stage_agent_streaming(
            agent,
            stage_name=request.stage_name,
            query=prompt,
            emit_thinking=False, # 防止前端消息混杂
            capture_trace=True
        )
        response, trace = response if isinstance(response, tuple) else (response, "")

        if trace and request.run_dir is not None:
            (request.run_dir / "eval_trace.txt").write_text(trace, encoding="utf-8")

        logger.info("[session=%s] [EvaluateStage] Agent 评分响应: %s...", ctx.state.task_id, response[:200])
        # 解析失败直接向上抛出 ValueError，由 _grade_one 的重试循环捕获
        return self._parse_agent_grading_response(response, request.expectations)

    def _parse_agent_grading_response(self, response: str, expectations: list) -> Dict[str, Any]:
        """解析 Agent 的评分响应"""
        results = {
            "expectations": [],
            "summary": {"passed": 0, "total": len(expectations)}
        }

        # 尝试从响应中提取 JSON
        parsed = self._parse_evals_json(response)

        if isinstance(parsed, list):
            passed_count = 0
            for item in parsed:
                if isinstance(item, dict):
                    passed = item.get("passed", False)
                    results["expectations"].append({
                        "expectation": item.get("expectation", ""),
                        "passed": passed,
                        "reason": item.get("reason", "")
                    })
                    if passed:
                        passed_count += 1

            results["summary"]["passed"] = passed_count
            return results

        # Fallback：简单解析
        for exp in expectations:
            results["expectations"].append({
                "expectation": exp,
                "passed": False,
                "reason": "无法解析 Agent 响应"
            })

        return results

    def _parse_evals_json(self, raw: str | object) -> dict:
        """从 Agent 返回值中健壮地提取 evals JSON.

        兼容四种返回形态：
          1. 已经是 dict/list（Agent 直接返回结构化对象）
          2. 纯 JSON 字符串
          3. ```json ... ``` 包裹的字符串
          4. 夹杂文本的响应（用括号计数法提取第一个完整 JSON 结构）
        """
        if isinstance(raw, (dict, list)):
            return raw

        if not isinstance(raw, str):
            raise ValueError(f"Agent.invoke() 返回了未预期的类型: {type(raw)}")

        # 尝试直接解析
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        # 提取 ```json ... ``` / ``` ... ``` 代码块
        code_block = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
        if code_block:
            try:
                return json.loads(code_block.group(1).strip())
            except json.JSONDecodeError:
                pass

        # 用括号计数法提取第一个完整 JSON 数组
        candidate = _extract_outermost_json(raw, "[", "]")
        if candidate is not None:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

        # 用括号计数法提取第一个完整 JSON 对象
        candidate = _extract_outermost_json(raw, "{", "}")
        if candidate is not None:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
        raise ValueError(f"无法从 Agent 输出中解析 JSON：{raw[:300]}")

    def _convert_agent_results_to_grading_result(self, agent_results: Dict) -> GradingResult:
        """将 Agent 的评分结果转换为 GradingResult schema"""
        expectations = []
        passed_count = 0

        for exp_result in agent_results.get("expectations", []):
            passed = exp_result.get("passed", False)
            if passed:
                passed_count += 1

            expectation = GradingExpectation(
                text=exp_result.get("expectation", ""),
                passed=passed,
                evidence=exp_result.get("reason", "")
            )
            expectations.append(expectation)

        total = len(expectations)
        pass_rate = passed_count / total if total > 0 else 0

        return GradingResult(
            expectations=expectations,
            pass_rate=round(pass_rate, 4),
            passed_count=passed_count,
            failed_count=total - passed_count,
        )

    # ------------------------------------------------------------------
    # Step 2: Benchmark 聚合
    # 逻辑内化自官方 aggregate_benchmark.py
    # ------------------------------------------------------------------

    def _aggregate_benchmark(self, ctx: SkillDevContext, iter_dir: Path) -> Benchmark:
        """遍历所有 grading.json + timing.json，聚合为 Benchmark."""
        evals = (ctx.state.evals or {}).get("evals", [])
        skill_name = (ctx.state.plan or {}).get("skill_name", "")

        variant_runs: dict[str, list[BenchmarkRun]] = {
            "with_skill": [],
            "baseline": []
        }
        for case in evals:
            eval_name = case.get("name", f"eval-{case.get('id', 0)}")
            case_dir = iter_dir / eval_name
            if not case_dir.exists():
                logger.info("[session=%s] [EvaluateStage] Case directory not found: %s", ctx.state.task_id, case_dir)
                continue

            for variant in ["with_skill", "baseline"]:
                variant_dir = case_dir / variant
                grading_file = variant_dir / "grading.json"
                timing_file = variant_dir / "timing.json"

                if not grading_file.exists():
                    logger.info(
                        "[session=%s] [EvaluateStage] Grading file not found: %s",
                        ctx.state.task_id,
                        grading_file,
                    )
                    continue

                try:
                    grading = json.loads(grading_file.read_text(encoding="utf-8"))
                    timing = (
                        json.loads(timing_file.read_text(encoding="utf-8"))
                        if timing_file.exists()
                        else {}
                    )

                    run = BenchmarkRun(
                        eval_id=case.get("id", 0),
                        eval_name=eval_name,
                        configuration=variant,
                        pass_rate=grading.get("summary", {}).get("pass_rate", 0.0),
                        time_seconds=timing.get("duration_seconds", 0.0),
                        tokens=timing.get("tokens_used", 0),
                        expectations=grading.get("expectations", []),
                        prompt=case.get("prompt", ""),
                    )
                    variant_runs[variant].append(run)
                    logger.info(
                        "[session=%s] [EvaluateStage] Loaded %s/%s: pass_rate=%s, time=%ss",
                        ctx.state.task_id,
                        variant,
                        eval_name,
                        run.pass_rate,
                        run.time_seconds,
                    )
                except Exception as e:
                    logger.error(
                        "[session=%s] [EvaluateStage] Failed to parse %s: %s",
                        ctx.state.task_id,
                        variant_dir,
                        e,
                    )
                    continue

        # 聚合统计
        variant_stats = {}
        for variant in ["with_skill", "baseline"]:
            runs = variant_runs[variant]
            if not runs:
                logger.warning(
                    "[session=%s] [EvaluateStage] No runs found for variant: %s",
                    ctx.state.task_id,
                    variant,
                )
                continue

            fully_passed_count = sum(
                1 for r in runs
                if r.expectations and all(e.get("passed") for e in r.expectations)
            )
            variant_stats[variant] = {
                "pass_rate": _calc_stats([r.pass_rate for r in runs]).to_dict(),
                "time_seconds": _calc_stats([r.time_seconds for r in runs]).to_dict(),
                "tokens": _calc_stats([float(r.tokens) for r in runs]).to_dict(),
                "run_count": len(runs),
                "fully_passed_count": fully_passed_count
            }

            logger.info(
                "[session=%s] [EvaluateStage]%s: pass_rate=%.1f%% +/- %.1f%%, runs=%d",
                ctx.state.task_id,
                variant,
                variant_stats[variant]["pass_rate"]["mean"] * 100,
                variant_stats[variant]["pass_rate"]["stddev"] * 100,
                len(runs),
            )

        # 计算 delta
        delta = self._calculate_delta(variant_stats, ctx.state.task_id)
        # 打印 delta 摘要
        if delta:
            logger.info("[session=%s] [EvaluateStage] Delta: %s", ctx.state.task_id, delta)
            variant_stats["delta"] = delta

        # 合并所有 runs 并返回
        all_runs = variant_runs["with_skill"] + variant_runs["baseline"]
        return Benchmark(
            skill_name=skill_name,
            runs=all_runs,
            run_summary=variant_stats,
            timestamp=_now_iso(),
        )

    def _calculate_delta(self, variant_stats: dict, session_id: str) -> dict:
        """计算 with_skill vs baseline 的改进指标.
        
        Args:
            variant_stats: {
                "with_skill": {"pass_rate": {...}, "time_seconds": {...}, "tokens": {...}},
                "baseline": {...}
            }
        
        Returns:
            delta: {
                "pass_rate": "+6.7%",
                "time_seconds": "+0.50s",
                "tokens": "+1500"
            }
        """
        delta = {}

        if "with_skill" not in variant_stats or "baseline" not in variant_stats:
            logger.warning("[session=%s] [EvaluateStage] Missing with_skill or baseline in variant_stats", session_id)
            return delta

        ws = variant_stats["with_skill"]
        bs = variant_stats["baseline"]

        # Pass Rate 对比
        pass_rate_delta = (ws["pass_rate"]["mean"] - bs["pass_rate"]["mean"]) * 100
        delta["pass_rate"] = f"{pass_rate_delta:+.1f}%"

        # Time Seconds 对比
        time_delta = ws["time_seconds"]["mean"] - bs["time_seconds"]["mean"]
        delta["time_seconds"] = f"{time_delta:+.2f}s"

        # Tokens 对比
        tokens_delta = ws["tokens"]["mean"] - bs["tokens"]["mean"]
        delta["tokens"] = f"{tokens_delta:+.0f}"

        return delta

    # ------------------------------------------------------------------
    # Step 3: Analyst
    # ------------------------------------------------------------------

    async def _analyze_patterns(
        self, ctx: SkillDevContext, benchmark: Benchmark
    ) -> list[str]:
        """分析 benchmark 结果，发现隐藏模式.

        待实现: 接入 create_stage_agent，用 ANALYST_SYSTEM_PROMPT 调用 Agent
              把 benchmark JSON 作为上下文，输出 notes 列表。
        """
        # 创建分析 agent（所有数据已在 prompt 中，无需文件工具）
        analyst_agent = ctx.create_stage_agent(
            stage_name="evaluate_analyst",
            whitelist_key="evaluate",
            system_prompt=ANALYST_SYSTEM_PROMPT,
            tools=[],
            max_iterations=30,
        )

        prompt = self._build_analyst_prompt(ctx, benchmark)

        # 不捕获 LLM 调用异常，让网络错误向上传播到 execute()
        response = await ctx.run_stage_agent_streaming(
            analyst_agent, stage_name="evaluate_analyst", query=prompt
        )
        logger.info("[session=%s] [EvaluateStage] Analyst 响应: %s...", ctx.state.task_id, response[:200])
        try:
            suggestions = self._parse_analyst_suggestions(response)
        except Exception as e:
            logger.error("[session=%s] [EvaluateStage] Analyst 结果解析失败: %s", ctx.state.task_id, e)
            suggestions = [{"category": "分析失败", "suggestion": str(e), "priority": "low"}]
        ctx.release_agent_tools(analyst_agent)
        return suggestions

    def _build_analyst_prompt(self, ctx: SkillDevContext, benchmark: Benchmark) -> str:
        """构造 analyst prompt，注入三层信息：

        1. 当前 Skill 文件内容（代码读取，无工具调用）
        2. 整体聚合统计（run_summary + delta）
        3. 各 eval case 逐条详情：用户 prompt + with_skill/baseline 各 expectation 的
           passed 状态与 grader reason
        """
        parts: list[str] = []

        # ── Section 1: 当前 Skill 文件内容 ───────────────────────────────────
        asset = load_asset(ctx.workspace)
        skill_dir_str = asset.get("skill_dir", "")
        skill_dir = Path(skill_dir_str) if skill_dir_str else ctx.workspace / "skill"

        skill_content, skill_failed = load_skill_content(skill_dir)
        if skill_failed:
            logger.warning(
                "[session=%s] [EvaluateStage] skill 文件部分未能预加载: %s",
                ctx.state.task_id, skill_failed,
            )
        parts.append(
            "## 当前 Skill 文件内容\n"
            + (skill_content if skill_content else "（未找到 skill 文件）")
        )

        # ── Section 2: 整体统计（去除 time/token，analyst 只关注质量维度）─────
        _skip = {"time_seconds", "tokens"}
        analyst_summary = {
            variant: {k: v for k, v in stats.items() if k not in _skip}
            for variant, stats in benchmark.run_summary.items()
        }
        parts.append(
            "## 整体评测统计\n"
            + json.dumps(analyst_summary, ensure_ascii=False, indent=2)
        )

        # ── Section 3: 各 case 逐条详情 ──────────────────────────────────────
        # 按 eval_name 分组，同一 case 的 with_skill / baseline 并列展示
        case_runs: dict[str, dict[str, BenchmarkRun]] = {}
        for run in benchmark.runs:
            case_runs.setdefault(run.eval_name, {})[run.configuration] = run

        case_lines: list[str] = ["## 各测试用例详情"]
        for eval_name, variants in sorted(case_runs.items()):
            any_run = next(iter(variants.values()))
            case_lines.append(f"\n### Case: {eval_name}")
            if any_run.prompt:
                case_lines.append(f"用户 Prompt：{any_run.prompt}")

            for config in ("with_skill", "baseline"):
                run = variants.get(config)
                if run is None:
                    continue
                pass_pct = f"{run.pass_rate * 100:.0f}%"
                case_lines.append(f"\n**{config}（pass_rate: {pass_pct}）**")
                for exp in run.expectations:
                    if not isinstance(exp, dict):
                        continue
                    status = "PASS" if exp.get("passed") else "FAIL"
                    text = exp.get("expectation") or exp.get("text", "")
                    reason = exp.get("reason") or exp.get("evidence", "")
                    case_lines.append(f"- [{status}] {text}")
                    if reason:
                        case_lines.append(f"  Grader reason: {reason}")

        parts.append("\n".join(case_lines))
        parts.append("请根据以上信息输出改进建议，并返回严格 **JSON** 数组。")
        return "\n\n".join(parts)

    def _parse_analyst_suggestions(self, response: str) -> list[Dict[str, str]]:
        """解析 Analyst 的建议"""
        # 尝试提取 JSON 数组（括号计数法）
        try:
            json_str = _extract_outermost_json(response, "[", "]")
            if json_str and json_str.strip() not in ("[无建议]", "[]"):
                parsed = json.loads(json_str)
                if isinstance(parsed, list):
                    suggestions = []
                    for item in parsed:
                        if isinstance(item, dict):
                            suggestions.append({
                                "category": item.get("category", "其他"),
                                "issue": item.get("issue", ""),
                                "suggestion": item.get("suggestion", ""),
                                "priority": item.get("priority", "medium"),
                            })
                    # JSON 解析成功，无论列表是否为空都直接返回，
                    # 空列表表示 analyst 认为无改进建议，属于合法结果
                    return suggestions
        except json.JSONDecodeError:
            pass

        # 无建议：JSON 解析失败或明确表示无建议
        return [{"category": "无建议", "issue": "", "suggestion": "", "priority": "low"}]

    # ------------------------------------------------------------------
    # Markdown 报告（给人看，也存入 workspace）
    # ------------------------------------------------------------------

    @staticmethod
    def _render_benchmark_md(benchmark: Benchmark) -> str:
        """把 Benchmark 渲染为 Markdown 报告."""
        rs = benchmark.run_summary
        configs = [k for k in rs if k != "delta"]

        lines = [
            f"# Skill Benchmark: {benchmark.skill_name}",
            "",
            f"**Date**: {benchmark.timestamp}",
            "",
            "## Summary",
            "",
        ]

        if len(configs) >= 2:
            a_name, b_name = configs[0], configs[1]
            a, b = rs[a_name], rs[b_name]
            delta = rs.get("delta", {})
            lines.append(f"| Metric | {a_name} | {b_name} | Delta |")
            lines.append("|--------|---------|---------|-------|")
            lines.append(
                f"| 平均完成度 | {a['pass_rate']['mean'] * 100:.0f}% ± {a['pass_rate']['stddev'] * 100:.0f}% "
                f"| {b['pass_rate']['mean'] * 100:.0f}% ± {b['pass_rate']['stddev'] * 100:.0f}% "
                f"| {delta.get('pass_rate', '—')} |"
            )
            lines.append(
                f"| 用时 (s)  | {a['time_seconds']['mean']:.1f}s ± {a['time_seconds']['stddev']:.1f}s "
                f"| {b['time_seconds']['mean']:.1f}s ± {b['time_seconds']['stddev']:.1f}s "
                f"| {delta.get('time_seconds', '—')} |"
            )
            lines.append(
                f"| 完全通过率 | {a.get('fully_passed_count', 0)} / {a['run_count']} 完全通过 "
                f"| {b.get('fully_passed_count', 0)} / {b['run_count']} 完全通过 "
                f"| {a.get('fully_passed_count', 0)-b.get('fully_passed_count', 0)} |"
            )

        if benchmark.notes:
            priority_labels = {"high": "🔴 高", "medium": "🟡 中", "low": "🔵 低"}
            lines.extend(["", "## Analyst Notes", ""])
            for note in benchmark.notes:
                if not isinstance(note, dict):
                    lines.append(f"- {note}")
                    continue
                category = note.get("category", "")
                issue = note.get("issue", "")
                suggestion = note.get("suggestion", "")
                priority = note.get("priority", "low")
                priority_label = priority_labels.get(priority, priority)

                header = f"**[{priority_label}]** " + (f"**{category}**" if category else "")
                lines.append(f"### {header}")
                if issue:
                    lines.append(f"> **问题**：{issue}")
                    lines.append("")
                if suggestion:
                    lines.append(f"💡 **建议**：{suggestion}")
                lines.append("")

        return "\n".join(lines)


def _extract_outermost_json(text: str, open_char: str, close_char: str) -> str | None:
    """用括号计数法从文本中提取第一个完整的 JSON 结构（数组或对象）.

    比贪婪正则更健壮：能正确处理嵌套括号，且不会把第一个 `[` 和最后一个 `]`
    之间的无关内容全部纳入，避免 json.loads 解析失败。

    Args:
        text:       待搜索的原始字符串
        open_char:  开括号（'[' 或 '{'）
        close_char: 闭括号（']' 或 '}'）

    Returns:
        找到的完整 JSON 子串；未找到则返回 None
    """
    start = text.find(open_char)
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape_next = False

    for i in range(start, len(text)):
        ch = text[i]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return text[start: i + 1]

    return None


def _is_grading_failure(agent_results: dict, expectations: list) -> bool:
    """判断 grading 结果是否为兜底失败值，用于决定是否需要重试.

    以下两种情况视为失败：
    1. 没有 expectations 列表（agent 未返回任何评分）
    2. 所有 expectations 的 reason 均为已知的失败占位文本
    """
    failure_reasons = {"Agent 评分失败", "无法解析 Agent 响应"}
    result_exps = agent_results.get("expectations", [])
    if not result_exps and expectations:
        return True
    return bool(result_exps) and all(
        e.get("reason", "") in failure_reasons for e in result_exps
    )


# ---------------------------------------------------------------------------
# 统计工具（内化自官方 aggregate_benchmark.py 的 calculate_stats）
# ---------------------------------------------------------------------------


def _calc_stats(values: list[float]) -> MetricStats:
    if not values:
        return MetricStats()
    n = len(values)
    mean = sum(values) / n
    stddev = math.sqrt(sum((x - mean) ** 2 for x in values) / (n - 1)) if n > 1 else 0.0
    return MetricStats(
        mean=round(mean, 4),
        stddev=round(stddev, 4),
        min=round(min(values), 4),
        max=round(max(values), 4),
    )

