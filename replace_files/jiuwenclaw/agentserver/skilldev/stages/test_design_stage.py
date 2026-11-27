# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""TEST_DESIGN 阶段处理器.

对齐官方 skill-creator 的测试设计流程：

1. 先生成 2-3 个真实用户场景的 test prompts（不写 assertions）
2. Assertions 应在 TEST_RUN 阶段运行期间并行起草
   （当前框架简化：在本阶段一次性生成 prompts + assertions）

evals.json 格式对齐官方 references/schemas.md：
{
  "skill_name": "example-skill",
  "evals": [
    {
      "id": 1,
      "name": "smoke_test_example",
      "case_category": "smoke_test",
      "prompt": "User's task prompt",
      "expected_output": "Description of expected result",
      "files": [],
      "expectations": ["The output includes X", "The skill used script Y"]
    }
  ]
}
"""

from __future__ import annotations

import re
import json
import logging
import asyncio
from pathlib import Path

from jiuwenclaw.agentserver.skilldev.asset_utils import load_skill_content, load_asset
from jiuwenclaw.agentserver.skilldev.context import SkillDevContext
from jiuwenclaw.agentserver.skilldev.schema import SkillDevEventType, SkillDevStage
from jiuwenclaw.agentserver.skilldev.stages.base import StageHandler, StageResult

logger = logging.getLogger(__name__)

_EVAL_COUNT = 2

TEST_DESIGN_SYSTEM_PROMPT = """你是一名专业的 Agent Skill 测试工程师。

## Skill 文件内容（已预加载，无需再用工具读取）

{skill_content_block}

根据以上 Skill 文件内容设计测试用例集。

## 测试用例类别（按覆盖度选取，不强制每类都有）

| 类别             | case_category      | 目的                           |
| ---------------- | ------------------ | ------------------------------ |
| 基础可用性       | smoke_test         | 最简输入验证 skill 能跑通      |
| 标准场景         | happy_path         | 真实用户的完整功能请求         |
| 边界/异常输入    | edge_case          | 空输入、超大输入、非常规格式   |
| 错误恢复         | error_handling     | 无效输入、缺失文件、格式错误   |
| 端到端工作流     | integration        | 多步骤、跨功能的完整流程       |

只选适用的类别，不适用的直接跳过。总数控制在 {_EVAL_COUNT}。

## prompt 设计规范

- 模拟真实用户输入：包含文件路径、个人背景、具体数据名称等细节
- 混合不同表达风格（正式 / 随意 / 简短 / 详细）
- 有些用户不会明确提到 skill 名称，但确实需要这个 skill 的功能
- smoke_test 的 prompt 要尽量简短，integration 的 prompt 应体现完整业务场景

## expectations 设计规范

- 每条是一个**可客观验证**的声明（字符串），评测者看到真实输出后能判断 pass/fail
- expectations字符串需按以下两类设计：

### 第一类：核心任务完成度验证（必填，每个用例至少 1 条）

验证 skill **是否完成了 prompt 要求的核心任务**。这类 expectation 的 pass/fail 直接反映 skill 能力。

**写法要求：**
- 必须与当前用例的 prompt 内容绑定——换一个 prompt 这条就不适用
- 通用 LLM 在不使用本 skill 的情况下，完成同一 prompt 大概率无法通过此条
- 对于含有脚本的skill，通过分析和预测agent运行结果来书写断言，无需实际运行任何脚本或代码

**好的示例（与 prompt 绑定，可验证文件内容）：**
- prompt 要求从 sales_2024.csv 提取月度趋势 → "输出文件或回复中包含 1–12 月各月的具体销售数值"
- prompt 要求将会议纪要翻译成英文并保存 → "输出的文本文件内容为英文，且保留了原始会议的主要议题"
- prompt 要求对代码仓库做安全扫描 → "回复中明确列出了至少一个具体漏洞类型或风险点（如 SQL 注入、硬编码密钥）"
- prompt 要求生成数据可视化图表 → "生成了图片文件（.png/.svg/.html），而非仅输出纯文字描述"
- prompt 要求将 JSON 数据转为 Excel → "输出的 .xlsx 文件中包含与输入 JSON 字段名一致的列标题"

### 第二类：质量/格式细节（可选，补充验证，每个用例 0–2 条）

在第一类已覆盖核心任务的基础上，验证输出的附加质量要求。

**写法要求：**
- 必须与当前 prompt 的具体要求有关，不写通用格式检查
- 可以验证文件内部内容，但断言需具体且可判定（指明字段名、关键词、结构特征）

**可接受的示例：**
- prompt 明确指定了章节 → "输出的 Markdown 文件包含与 prompt 中提到的三个章节对应的二级标题"
- prompt 要求脚本接受路径参数 → "生成的 Python 脚本中包含对 input_path 参数的处理逻辑"
- prompt 要求提取特定实体 → "输出 JSON 中的 summary 字段包含原文中的公司名称"

**反模式（禁止写入）：**
- 存在性检查："生成了文件"、"输出了内容"（不验证正确性）
- 任意数量阈值："包含至少 1 条记录"（几乎必然 pass，无区分性）
- 与 prompt 无关的通用格式："文件是合法的 JSON 格式"（不验证任务完成度）
- 工具调用过程检查："使用了 scripts/ 目录下的 parse.py"（评测者看不到执行过程）
- 主观判断："输出质量较高"、"翻译流畅自然"
- 泛化断言："skill 成功运行"、"任务已完成"

## 输入文件说明

工作区路径：{workspace}。
禁止在工作区外进行操作。

如果某个测试用例需要真实输入文件（如待处理的 PDF、CSV），在 files 字段中列出路径并在对应地址写入文件：
- 路径格式：`evals/skill-tests/{skill_name}/files/<filename>`
- 在 expected_output 中说明该文件应如何被处理

## 输出格式（严格 JSON，不包含任何解释文字或 markdown 代码块，expectations条目内容无双引号等其他特殊符号，便于JSON解析）

{{
  "skill_name": "{skill_name}",
  "evals": [
    {{
      "id": 1,
      "name": "smoke_test",
      "case_category": "smoke_test",
      "prompt": "最简可用性验证的用户输入...",
      "expected_output": "预期结果的人类可读描述",
      "files": [],
      "expectations": [
        "可客观验证的声明 1",
        "可客观验证的声明 2"
      ]
    }},
    {{
      "id": 2,
      "name": "happy_path_with_details",
      "case_category": "happy_path",
      "prompt": "包含具体细节的完整功能请求...",
      "expected_output": "预期结果描述...",
      "files": [],
      "expectations": [...]
    }}
  ]
}}
"""


class TestDesignStageHandler(StageHandler):
    """TEST_DESIGN 阶段：Agent 设计测试用例，输出 evals.json."""

    # 单次 design 失败后的最大重试次数（不含首次）
    _MAX_DESIGN_RETRIES = 2
    # 单次 design 调用的超时秒数（防止网络挂起导致整个 stage 无限等待）
    _DESIGN_TIMEOUT_SECONDS = 300

    async def execute(self, ctx: SkillDevContext) -> StageResult:
        """主流程：设计测试用例并保存 evals.json."""
        await ctx.emit(SkillDevEventType.PROGRESS, {"message": "正在设计测试用例..."})

        skill_name = self._get_skill_name(ctx)

        for attempt in range(1, self._MAX_DESIGN_RETRIES + 1):
            # 生成测试案例（带超时保护，防止网络挂起）
            try:
                evals = await asyncio.wait_for(
                    self._design_evals(ctx, skill_name, attempt),
                    timeout=self._DESIGN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError as e:
                logger.warning(
                    "[session=%s] [TestDesignStage] 测试用例设计超时（>%ds），准备重试 (%d/%d)",
                    ctx.state.task_id,
                    self._DESIGN_TIMEOUT_SECONDS, attempt, self._MAX_DESIGN_RETRIES,
                )
                if attempt < self._MAX_DESIGN_RETRIES:
                    wait = 2 ** (attempt - 1)
                    await asyncio.sleep(wait)
                    continue
                msg = f"测试用例设计超时，{self._MAX_DESIGN_RETRIES} 次重试后仍失败"
                await ctx.emit(SkillDevEventType.ERROR, {"message": msg, "stage": "test_design"})
                raise RuntimeError(msg) from e

            # 验证 evals schema，确保每个 case 都有 name 字段
            evals = self._validate_and_normalize_evals(
                evals,
                skill_name,
                session_id=ctx.state.task_id,
            )

            # 验证已生成正确格式的测试用例
            count = len(evals.get("evals", []))

            if count > 0:
                break

            if attempt < self._MAX_DESIGN_RETRIES:
                wait = 2 ** (attempt - 1)  # 指数退避：1s, 2s
                logger.warning(
                    "[session=%s] [TestDesignStage] 测试案例生成失败，%ds 后重试 (%d/%d)",
                    ctx.state.task_id,
                    wait, attempt, self._MAX_DESIGN_RETRIES,
                )
                await asyncio.sleep(wait)
            else:
                msg = f"[TestDesignStage] {self._MAX_DESIGN_RETRIES} 次重试后仍失败"
                logger.error(
                    "[session=%s] [TestDesignStage] %s 次重试后仍失败",
                    ctx.state.task_id,
                    self._MAX_DESIGN_RETRIES,
                )
                await ctx.emit(SkillDevEventType.ERROR, {"message": msg, "stage": "test_design"})
                raise RuntimeError(msg)

        # 更新state，储存生成的测试案例
        ctx.state.evals = evals
        # 测试案例写入本地
        try:
            self._write_evals_file(ctx, skill_name, evals)
        except Exception as e:
            msg = f"测试用例文件写入失败：{e}"
            await ctx.emit(SkillDevEventType.ERROR, {"message": msg, "stage": "test_design"})
            raise RuntimeError(msg) from e

        await ctx.emit(SkillDevEventType.PROGRESS, {"message": f"已设计 {count} 个测试用例"})

        return StageResult(next_stage=SkillDevStage.TEST_RUN)
    
    def _get_skill_name(self, ctx: SkillDevContext) -> str:
        """解析skill名称"""
        if ctx.state.plan and isinstance(ctx.state.plan, dict):
            return ctx.state.plan.get("skill_name", "skill")
        return "skill"
    
    def _write_evals_file(self, ctx: SkillDevContext, skill_name: str, evals: dict) -> None:
        """写入 evals/<skill_name>/evals.json."""
        evals_dir = ctx.workspace / "evals" / skill_name
        evals_dir.mkdir(parents=True, exist_ok=True)

        content = json.dumps(evals, ensure_ascii=False, indent=2)
        (evals_dir / "evals.json").write_text(content, encoding="utf-8")

        logger.info("[session=%s] [TestDesignStage] evals.json 写入: %s", ctx.state.task_id, evals_dir)

    def _validate_and_normalize_evals(
        self, evals: dict, skill_name: str, session_id: str = ""
    ) -> dict:
        """验证和规范化 evals，确保符合 schema.
        
        - 确保每个 eval case 都有 "name" 字段
        - 修复缺失的字段或类型错误
        - 记录任何异常
        
        Args:
            evals: Agent 生成的原始 evals dict
            skill_name: skill 名称
            
        Returns:
            规范化后的 evals dict
        """
        if not isinstance(evals, dict):
            logger.error("[session=%s] [TestDesignStage] evals 不是 dict，而是 %s", session_id, type(evals))
            return {"skill_name": skill_name, "evals": []}
        
        evals_list = evals.get("evals", [])
        if not isinstance(evals_list, list):
            logger.error("[session=%s] [TestDesignStage] evals['evals'] 不是 list", session_id)
            return {"skill_name": skill_name, "evals": []}
        
        normalized = {"skill_name": skill_name, "evals": []}
        
        for idx, case in enumerate(evals_list, start=1):
            if not isinstance(case, dict):
                logger.warning("[session=%s] [TestDesignStage] eval case #%s 不是 dict，跳过", session_id, idx)
                continue
            
            # 确保 name 字段存在
            if "name" not in case or not case["name"]:
                # 尝试从 case_category 或 id 生成 name
                case_id = case.get("id", idx)
                category = case.get("case_category", "test")
                case["name"] = f"{category}_{case_id}"
                logger.warning(
                    "[session=%s] [TestDesignStage] eval case #%s 缺少 'name' 字段，已自动生成: %s",
                    session_id,
                    idx,
                    case["name"],
                )
            
            # 验证必需字段
            required_fields = ["id", "name", "case_category", "prompt", "expectations"]
            for field in required_fields:
                if field not in case:
                    logger.warning(
                        "[session=%s] [TestDesignStage] eval '%s' 缺少字段 '%s'",
                        session_id,
                        case["name"],
                        field,
                    )
                    # 提供默认值
                    if field == "id":
                        case["id"] = idx
                    elif field == "case_category":
                        case["case_category"] = "custom"
                    elif field == "prompt":
                        case["prompt"] = ""
                    elif field == "expectations":
                        case["expectations"] = []
            
            # 确保 expectations 是列表
            if not isinstance(case.get("expectations"), list):
                logger.warning(
                    "[session=%s] [TestDesignStage] eval '%s' 的 expectations 不是 list",
                    session_id,
                    case["name"],
                )
                case["expectations"] = []
            
            # 确保 files 字段存在（可以是空列表）
            if "files" not in case:
                case["files"] = []
            
            # 确保 expected_output 字段存在
            if "expected_output" not in case:
                case["expected_output"] = ""
            
            normalized["evals"].append(case)
        
        if len(normalized["evals"]) == 0:
            logger.error("[session=%s] [TestDesignStage] 规范化后没有有效的 eval case", session_id)
        else:
            logger.info(
                "[session=%s] [TestDesignStage] 规范化完成，共 %s 个 eval case",
                session_id,
                len(normalized["evals"]),
            )
        
        return normalized

    async def _design_evals(self, ctx: SkillDevContext, skill_name: str, attempt: int) -> dict:
        """调用 Agent 设计测试用例（流式，实时推送 LLM 推理过程）."""
        try:
            # 代码预加载 skill 文件内容，直接注入 system prompt，无需 agent 调用文件工具
            asset = load_asset(ctx.workspace)
            skill_dir_str = asset.get("skill_dir", "")
            skill_dir = Path(skill_dir_str) if skill_dir_str else ctx.workspace / "skill"

            skill_content_block, skill_failed = load_skill_content(skill_dir)
            if skill_failed:
                logger.warning(
                    "[session=%s] [TestDesignStage] skill 文件部分未能预加载: %s",
                    ctx.state.task_id, skill_failed,
                )
            if not skill_content_block:
                logger.warning("[TestDesignStage] skill 内容为空，asset.json 可能尚未更新")
                skill_content_block = f"（skill 内容未找到，工作区路径：{ctx.workspace}/skill/）"

            system_prompt = TEST_DESIGN_SYSTEM_PROMPT.format(
                _EVAL_COUNT=_EVAL_COUNT,
                skill_name=skill_name,
                skill_content_block=skill_content_block,
                workspace=ctx.workspace
            )

            # stage_name 含 iteration + attempt，保证每次运行 conversation_id 唯一，
            # 避免 IMPROVE 多轮迭代后历史消息不断累积导致 prefill 爆炸
            iteration = getattr(ctx.state, "iteration", 0)
            stage_key = f"test_design/iter_{iteration}/attempt_{attempt}"

            agent = ctx.create_stage_agent(
                stage_name=stage_key,
                whitelist_key="test_design",
                system_prompt=system_prompt,
                tools=["file_read", "web_search_free", "shell", "file_write"],
                max_iterations=30,
            )
            prompt = (
                f"Skill 文件内容已在 system prompt 中预加载。无需再去调用文件工具读取文件内容。"
                f"请直接为 skill_name='{skill_name}' 设计测试用例，输出 JSON，不要包含任何解释文字。"
            )
            raw, trace = await ctx.run_stage_agent_streaming(
                agent, stage_name=stage_key, query=prompt, capture_trace=True
            )
            if trace:
                evals_dir = ctx.workspace / "evals" / skill_name
                evals_dir.mkdir(parents=True, exist_ok=True)
                (evals_dir / "test_design_trace.txt").write_text(trace, encoding="utf-8")

            output = self._parse_evals_json(raw)
            ctx.release_agent_tools(agent)
            return output

        except ValueError as e:
            logger.error("[session=%s] [TestDesignStage] evals JSON 解析失败，使用占位数据: %s", ctx.state.task_id, e)
            msg = f"测试用例初始化失败：Agent 运行错误或输出无法解析为合法 JSON 格式（{e}）"
            await ctx.emit(SkillDevEventType.ERROR, {"message": msg, "stage": "test_design"})
            raise RuntimeError(msg) from e
        except Exception as e:
            logger.exception("[session=%s] [TestDesignStage] 未预期的错误: %s", ctx.state.task_id, e)
            msg = f"测试设计 Agent 执行失败：{e}"
            await ctx.emit(SkillDevEventType.ERROR, {"message": msg, "stage": "test_design"})
            raise RuntimeError(msg) from e
    
    def _parse_evals_json(self, raw: str | object) -> dict:
        """从 Agent 返回值中健壮地提取 evals JSON.

        兼容三种返回形态：
          1. 已经是 dict（Agent 直接返回结构化对象）
          2. 纯 JSON 字符串
          3. ```json ... ``` 包裹的字符串
        """
        if isinstance(raw, dict):
            return raw

        if not isinstance(raw, str):
            raise ValueError(f"Agent.invoke() 返回了未预期的类型: {type(raw)}")

        # 尝试直接解析
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        # 提取 ```json ... ``` 块
        match = re.search(r"```(?:json)?\s*(\{.*?})\s*```", raw, re.DOTALL)
        if match:
            return json.loads(match.group(1))

        # 提取裸 JSON 对象（最后兜底）
        match = re.search(r"\{.*}", raw, re.DOTALL)
        if match:
            return json.loads(match.group(0))

        raise ValueError(f"无法从 Agent 输出中解析 evals JSON，输出片段：{raw[:300]}")

    def _build_external_tools_hint(self, ctx: SkillDevContext) -> str:
        """若存在外部工具，追加一段提示帮助 agent 设计出能验证工具调用的测试用例.

        无外部工具时返回空字符串，不影响原有 prompt。
        """
        tools = ctx.state.external_tools
        if not tools:
            return ""

        lines = [
            "\n\n## 可用的外部工具（设计测试用例时请参考）\n",
            "当 Skill 涉及调用以下外部工具时。设计测试用例时请注意：",
            "- prompt 应包含能触发工具调用的真实用户场景",
            "- expectations 应能区分“调用了工具并使用其返回数据”与“未调用工具、仅凭通用知识回答”两种情况\n",
        ]
        for tool in tools:
            name = tool.get("name", "未命名工具")
            lines.append(f"### {name}")
            desc = tool.get("description")
            if desc:
                lines.append(f"描述：{desc}")
            url = tool.get("url")
            if url:
                method = tool.get("method", "GET").upper()
                lines.append(f"请求：{method} {url}")
            params = tool.get("parameters")
            if params:
                props = params.get("properties", {})
                if props:
                    lines.append(f"主要参数：{', '.join(props.keys())}")
            lines.append("")

        return "\n".join(lines)