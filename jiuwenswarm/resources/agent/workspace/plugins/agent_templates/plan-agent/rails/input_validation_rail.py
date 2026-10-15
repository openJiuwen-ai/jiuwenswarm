"""规划总管输入校验 Rail。

定位：在每次模型调用前注入「模块一 7 个输入文件清单」+「PlanningFeedback 格式契约」
两个安全边界 prompt section，让 LLM 在调 call_planning_skill 之前先自查 input_dir / feedback_file
的契约，避免把脏数据塞给子进程。

设计要点：
    - 软约束（不阻断 LLM 输出）：与 health-life-advisor 的 safety_guard_rail 一致，
      框架只暴露 before_model_call / after_model_call 钩子；硬约束（路径存在性、
      JSON 解析）由 call_planning_skill tool 自己做。
    - 跟 health-life-advisor 的 safety_guard_rail 一样，**只注入**信息，不修改用户
      输入 / 工具调用 / 决策结果。
    - AGENT_NAME = "plan-supervisor"：框架按 agent id 路由 rail。
    - 复用 load_inputs._INPUT_FILES 命名约定（7 个文件名 + 必填字段），与 planning
      skill 的 load_inputs.py 保持单一事实源。
"""
from __future__ import annotations

from typing import Any, Optional

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail


# 7 个模块一输入文件名（与 skills/planning/scripts/load_inputs._INPUT_FILES 一致）
REQUIRED_INPUT_FILES: list[str] = [
    "research_question.json",
    "hypotheses.json",
    "gap_report.json",
    "key_papers.json",
    "resource_constraints.json",
    "research_frontier.json",
    "domain.json",
]

# 各文件顶层必填字段（与 skills/planning/scripts/load_inputs._load_xxx 对齐）
REQUIRED_FIELDS_PER_FILE: dict[str, list[str]] = {
    "research_question.json":    ["topic", "scope", "success_criteria"],
    "hypotheses.json":           ["id", "claim", "verifiable"],  # 每条
    "gap_report.json":           ["research_question", "existing_state", "missing_capability"],
    "key_papers.json":           ["id", "title", "method_key"],  # 每条
    "resource_constraints.json": ["gpu_hours", "memory_gb", "time_budget_days"],
    "research_frontier.json":    ["frontier_text"],
    "domain.json":               ["domain_name"],
}

# PlanningFeedback 合法 category（与 load_inputs._FEEDBACK_CATEGORIES 一致）
FEEDBACK_CATEGORIES: set[str] = {"data", "compute", "baseline", "metric", "schema", "method",
                                 "experiment", "budget"}

# 注入给 LLM 的系统提示
INPUT_VALIDATION_PROMPT = """## 规划模块输入契约（强制执行）

调用 call_planning_skill 之前，必须自查以下两点：

### 1. input_dir 必须含 7 个模块一产物

```
{required_files_tree}
```

调 skill 前建议：
- 先用 `ls <input_dir>` 或 glob 验证 7 个文件都在
- 缺哪个文件就不要调 skill，让用户去补；不要传空目录 / 错路径
- input_dir 不存在 → tool 内部会直接返回 error（已 hard-check），但浪费一次 subprocess 启动

各文件顶层必填字段（缺一不可）：

```
{required_fields_tree}
```

### 2. feedback_file 必须是合法 PlanningFeedback JSON（仅在 REPLAN 时检查）

合法顶层字段：reason / affected_experiment_ids / blockers / suggested_changes
- `affected_experiment_ids`: 非空 list[str]
- `blockers`: 非空 list[str]，每条形如 `[category] description`，category ∈ {allowed_categories}
- `suggested_changes`: 非空 list[str]

合法 category 集合：{allowed_categories}

调 skill 前**不要**在 feedback_file 里塞非合同字段；模块三反馈进来时如有冗余字段，
先把它们清掉再灌进 skill。
"""


def _build_input_validation_prompt() -> str:
    """组装实际的 prompt（包含具体的 7 个文件清单 + 字段清单）。"""
    files_md = "\n".join(f"- `{name}`" for name in REQUIRED_INPUT_FILES)
    fields_md_lines: list[str] = []
    for fname, fields in REQUIRED_FIELDS_PER_FILE.items():
        fields_md_lines.append(f"- `{fname}`: {', '.join(fields)}")
    fields_md = "\n".join(fields_md_lines)
    return INPUT_VALIDATION_PROMPT.format(
        required_files_tree=files_md,
        required_fields_tree=fields_md,
        allowed_categories=sorted(FEEDBACK_CATEGORIES),
    )


class InputValidationRail(DeepAgentRail):
    """规划总管输入校验 Rail：注入输入契约 prompt，引导 LLM 调 tool 前自查。

    与 health-life-advisor 的 safety_guard_rail 同模式（继承 DeepAgentRail，
    before_model_call 注入 prompt）。区别：本 rail 不做高危关键词检测（plan-supervisor
    不涉及医疗 / 心理等领域），只做"输入契约"软约束。
    """

    def __init__(self) -> None:
        super().__init__()
        self._agent: Optional[Any] = None
        self._injected_prompt: str = _build_input_validation_prompt()

    def init(self, agent: Any) -> None:
        """注册到 agent 时缓存运行时对象（与 health-life-advisor safety_guard_rail 同模式）。"""
        self._agent = agent

    def uninit(self, agent: Any) -> None:
        self._agent = None

    @staticmethod
    def _inject_prompt(ctx: AgentCallbackContext, content: str, title: str) -> None:
        """向 prompt 装配器注入 system prompt section（与 health-life-advisor safety_guard_rail 同实现）。"""
        assembler = getattr(ctx, "prompt_assembler", None) or getattr(ctx, "prompt", None)
        if assembler is None:
            return
        add_section = getattr(assembler, "add_section", None)
        if callable(add_section):
            try:
                add_section(title=title, content=content)
            except TypeError:
                add_section(title, content)

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """每次模型调用前注入输入契约 prompt section。"""
        self._inject_prompt(ctx, self._injected_prompt, "规划模块输入契约")

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        """清理本 rail 注入的临时状态（与 health-life-advisor safety_guard_rail 同模式）。"""
        return
