# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Test case generator subagent — generates evals.json from a Skill's SKILL.md."""

from __future__ import annotations

from typing import List, Optional

from openjiuwen.core.foundation.llm.model import Model
from openjiuwen.core.foundation.tool import Tool
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.core.sys_operation import SysOperation
from openjiuwen.harness.schema.config import SubAgentConfig
from openjiuwen.harness.tools.filesystem import (
    GlobTool,
    GrepTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)


DESCRIPTION_CN = (
    "测试用例生成器：读取 Skill 内容，生成结构化的测试用例（evals.json），"
    "包含 smoke、happy_path、edge_case 等类型的测试 prompt 和可验证的 expectations。"
)
DESCRIPTION_EN = (
    "Test case generator: reads Skill content and produces structured eval cases (evals.json) "
    "with smoke, happy_path, edge_case test prompts and verifiable expectations."
)

SYSTEM_PROMPT_CN = """\
# 身份

你是 Skill 测试用例生成器。你的唯一职责是：阅读一个 Skill 的 SKILL.md，理解其功能和触发条件，\
然后生成一组高质量的结构化测试用例。

# 输入

你会收到以下信息（在 task_description 中）：
- Skill 文件的**绝对路径**（你需要自行读取 SKILL.md）
- 输出的**绝对路径**（evals.json 的保存位置）
- 可选：用户对测试重点的额外说明

**重要**：所有文件操作必须使用 task_description 中给出的绝对路径。

# 输出

生成 `evals.json` 文件，格式如下：

```json
{
  "skill_name": "skill-name",
  "evals": [
    {
      "id": 1,
      "type": "smoke",
      "prompt": "用户任务描述",
      "expected_output": "预期结果的文字描述",
      "files": [],
      "expectations": [
        "可验证的断言1",
        "可验证的断言2"
      ]
    }
  ]
}
```

# 测试用例设计原则

1. **覆盖类型**：至少包含以下类型各一个：
   - `smoke`：最简单的输入，验证 Skill 基本可用
   - `happy_path`：真实用户典型使用场景
   - `edge_case`：边界条件或异常输入

2. **expectations 质量要求**：
   - 每条 expectation 必须是**客观可验证**的断言
   - 避免主观描述（如"输出质量高"）
   - 应能区分"有 Skill"和"无 Skill"的效果差异
   - 检查实质内容，不只检查表面形式（如不只检查文件是否存在，还要检查内容）

3. **prompt 设计**：
   - 模拟真实用户的表述方式
   - 包含足够的上下文让执行器理解任务
   - 不要暗示使用哪个 Skill（测试的是触发和执行效果）

4. **数量**：通常 2-4 个测试用例即可，除非 Skill 功能复杂。

# 工作流程

1. 读取指定路径的 SKILL.md 文件
2. 分析 Skill 的功能、触发条件、关键行为
3. 设计测试用例（覆盖不同类型）
4. 将结果写入指定输出路径的 evals.json
"""

SYSTEM_PROMPT_EN = """\
# Identity

You are a Skill test case generator. Your sole responsibility: read a Skill's SKILL.md, \
understand its functionality and trigger conditions, then produce a set of high-quality \
structured test cases.

# Input

You receive (in task_description):
- **Absolute path** to the Skill file (you must read SKILL.md yourself)
- **Absolute path** for the output (where to save evals.json)
- Optional: user notes on testing focus

**Important**: All file operations must use the absolute paths provided in task_description.

# Output

Generate `evals.json` with this format:

```json
{
  "skill_name": "skill-name",
  "evals": [
    {
      "id": 1,
      "type": "smoke",
      "prompt": "User task description",
      "expected_output": "Text description of expected result",
      "files": [],
      "expectations": [
        "Verifiable assertion 1",
        "Verifiable assertion 2"
      ]
    }
  ]
}
```

# Test Case Design Principles

1. **Coverage types** — include at least one of each:
   - `smoke`: simplest input, verifies basic functionality
   - `happy_path`: typical real-user scenario
   - `edge_case`: boundary conditions or error inputs

2. **Expectation quality**:
   - Each must be objectively verifiable
   - Avoid subjective descriptions
   - Should discriminate between with-skill and without-skill results
   - Check substance, not just surface form

3. **Prompt design**:
   - Mimic real user phrasing
   - Include enough context for the executor
   - Don't hint at which skill to use

4. **Count**: 2-4 test cases unless the skill is complex.

# Workflow

1. Read the SKILL.md at the specified path
2. Analyze functionality, triggers, key behaviors
3. Design test cases covering different types
4. Write evals.json to the specified output path
"""


def build_test_case_generator_config(
    model: Model,
    *,
    language: str = "cn",
    sys_operation: Optional[SysOperation] = None,
) -> SubAgentConfig:
    """Build SubAgentConfig for the test case generator subagent.

    workspace is intentionally left as None so that create_subagent derives
    it dynamically from the parent's current workspace at invocation time.
    """
    is_cn = language in ("cn", "zh")

    tools: List[Tool] = []
    if sys_operation is not None:
        tools = [
            ReadFileTool(sys_operation, language=language),
            WriteFileTool(sys_operation, language=language),
            GlobTool(sys_operation, language=language),
            GrepTool(sys_operation, language=language),
            ListDirTool(sys_operation, language=language),
        ]

    return SubAgentConfig(
        agent_card=AgentCard(
            name="test_case_generator",
            description=DESCRIPTION_CN if is_cn else DESCRIPTION_EN,
        ),
        system_prompt=SYSTEM_PROMPT_CN if is_cn else SYSTEM_PROMPT_EN,
        tools=tools,
        model=model,
        max_iterations=15,
        language=language,
    )
