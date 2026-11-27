# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""SkillDev 模块的核心数据模型.

所有跨模块共享的数据结构定义在此，包括：
- 流程阶段枚举（SkillDevStage）
- 任务状态（SkillDevState）
- 事件类型（SkillDevEventType）及事件体（SkillDevEvent）
- 挂起点配置（SuspensionConfig / SUSPENSION_POINTS）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


# ---------------------------------------------------------------------------
# 阶段枚举
# ---------------------------------------------------------------------------


class SkillDevStage(str, Enum):
    """SkillDev Pipeline 的所有阶段.

    流程：INIT → CLARIFY → QUESTION_CLARIFY(挂起) → GENERATE → VALIDATE
        → SKIP_TESTS_CONFIRM(挂起) → TEST_DESIGN → TEST_RUN → EVALUATE → REVIEW(挂起)
        → IMPROVE → (回到 TEST_RUN 迭代)
        → DESC_OPTIMIZE_CONFIRM(挂起) → DESC_OPTIMIZE(可选) → PACKAGE → COMPLETED
    （亦可从 SKIP_TESTS_CONFIRM 选择跳过测试，直接进入 DESC_OPTIMIZE_CONFIRM）
    """

    # 主流程
    INIT = "init"
    CLARIFY = "clarify"                    # AI 分析需求，生成澄清问题
    QUESTION_CLARIFY = "question_clarify"  # 挂起点：等待用户回答澄清问题
    GENERATE = "generate"
    VALIDATE = "validate"  # 校验生成的 SKILL.md 格式（YAML frontmatter + 命名规范）
    SKIP_TESTS_CONFIRM = "skip_tests_confirm"  # 挂起点：询问是否跳过集成/评测测试
    TEST_DESIGN = "test_design"
    TEST_RUN = "test_run"
    EVALUATE = "evaluate"  # grader 评分 + aggregate_benchmark 聚合 + analyst 分析
    REVIEW = "review"  # 挂起点：等待用户审阅评测结果
    IMPROVE = "improve"

    # 打包与描述优化
    PACKAGE = "package"
    DESC_OPTIMIZE_CONFIRM = "desc_optimize_confirm"  # 挂起点：询问用户是否需要描述优化
    DESC_OPTIMIZE = "desc_optimize"  # 触发描述优化循环

    # 终态
    COMPLETED = "completed"

    # 异常
    ERROR = "error"


class SkillDevTaskMode(str, Enum):
    """任务入口模式（由请求参数自动判断）."""

    CREATE = "create"  # 纯 query 创建
    CREATE_WITH_RESOURCES = "create_with_resources"  # 携带资源包创建
    MODIFY = "modify"  # 修改/升级已有 skill


# ---------------------------------------------------------------------------
# 事件类型
# ---------------------------------------------------------------------------


class SkillDevEventType(str, Enum):
    """Pipeline 向前端推送的事件类型.

    设计原则：后端推的每个事件，前端都应能直接映射到一个 UI 动作，
    而非让前端猜测语义。
    """

    # --- 流程控制 ---
    STAGE_CHANGED = "skilldev.stage_changed"  # 阶段切换（内部标识）
    PROGRESS = "skilldev.progress"  # 阶段内进度文本（对话流展示）
    ERROR = "skilldev.error"  # 不可恢复错误

    # --- 对话流交互 ---
    AGENT_THINKING = "skilldev.agent_thinking"  # Agent 推理流（delta）
    AGENT_OUTPUT = "skilldev.agent_output"  # Agent 正文输出流（delta，区别于推理，对应前端 assistant 气泡）
    TEST_PROGRESS = "skilldev.test_progress"  # 测试执行进度
    TOOL_CALL = "skilldev.tool_call"  # 结构化工具调用事件
    TOOL_RESULT = "skilldev.tool_result"  # 结构化工具结果事件

    # --- 结构化 UI 驱动 ---
    CONFIRM_REQUEST = "skilldev.confirm_request"  # 挂起点：驱动前端弹出确认框
    TODOS_UPDATE = "skilldev.todos_update"  # 驱动右侧 Todo 列表
    ARTIFACT_READY = "skilldev.artifact_ready"  # 驱动右侧产物/附件列表

    # --- 数据载体（对话流中展示详情） ---
    SKILL_NAME_READY = "skilldev.skill_name_ready"  # 生成 skill_name
    EVAL_READY = "skilldev.eval_ready"  # 评测结果（benchmark JSON）
    VALIDATE_RESULT = "skilldev.validate_result"  # SKILL.md 校验结果
    DESC_OPT_READY = "skilldev.desc_opt_ready"  # 描述优化 before/after


@dataclass
class SkillDevEvent:
    """Pipeline 内部事件，最终被序列化为 AgentResponseChunk 推送给前端."""

    event_type: SkillDevEventType
    payload: dict[str, Any]
    task_id: str = ""


# ---------------------------------------------------------------------------
# 运行时状态（Source of Truth，驻内存）
# ---------------------------------------------------------------------------


@dataclass
class SkillDevState:
    """Pipeline 运行时状态，在请求执行期间驻内存，在阶段边界通过 StateStore checkpoint."""

    task_id: str
    stage: SkillDevStage = SkillDevStage.INIT
    mode: SkillDevTaskMode = SkillDevTaskMode.CREATE
    iteration: int = 0  # 当前改进轮次（从 0 开始）

    # 输入
    input: dict[str, Any] = field(default_factory=dict)
    skill_name: str = ""

    # 工作区目录状态
    skill_dir_empty: bool = True       # skill/ 目录是否为空（INIT 阶段写入后更新）
    ref_files_dir_empty: bool = True   # resources/ref-files 目录是否为空
    ref_skills_dir_empty: bool = True  # resources/ref-skills 目录是否为空
    tool_scripts_dir_empty: bool = True  # resources/available-tools 目录是否为空

    # 中间产物
    reference_texts: list[str] = field(default_factory=list)  # 资源文件解析后的文本
    existing_skill_md: str | None = None  # 已有 SKILL.md 内容
    clarification_questions: list[dict] = field(default_factory=list)  # CLARIFY 阶段产出的问题列表
    clarification_answers: list[dict] = field(default_factory=list)    # 用户在 QUESTION_CLARIFY 阶段回答的答案
    plan: dict[str, Any] | None = None  # 兼容字段：生成/校验阶段写入的 skill 元数据
    generate_retries: int = 0  # GENERATE ↔ VALIDATE 重试计数
    last_validate_error: str | None = None  # VALIDATE 失败原因（供 GENERATE 重试时参考）
    skipped_benchmark_tests: bool = False  # 用户在 SKIP_TESTS_CONFIRM 选择跳过测试
    evals: dict[str, Any] | None = None  # TEST_DESIGN 阶段产出
    eval_results: dict[str, Any] | None = None  # EVALUATE 阶段产出
    feedback_history: list[dict] = field(default_factory=list)  # 每轮改进的用户反馈

    # 外部工具（用户上传的工具定义列表，每项为一个 tool dict）
    external_tools: list[dict] = field(default_factory=list)

    # 描述优化
    desc_optimize_result: dict[str, Any] | None = (
        None  # run_loop 输出（best_description, history）
    )

    # 输出
    zip_path: str | None = None
    zip_size: int = 0

    # 元数据
    created_at: str = field(default_factory=lambda: _now_iso())
    updated_at: str = field(default_factory=lambda: _now_iso())
    error: str | None = None

    def touch(self) -> None:
        """更新 updated_at 时间戳."""
        self.updated_at = _now_iso()

    def to_checkpoint_dict(self) -> dict:
        """序列化为可持久化的字典（用于 StateStore）."""
        return {
            "task_id": self.task_id,
            "stage": self.stage.value,
            "mode": self.mode.value,
            "iteration": self.iteration,
            "input": self.input,
            "skill_name": self.skill_name,
            "skill_dir_empty": self.skill_dir_empty,
            "ref_files_dir_empty": self.ref_files_dir_empty,
            "ref_skills_dir_empty": self.ref_skills_dir_empty,
            "tool_scripts_dir_empty": self.tool_scripts_dir_empty,
            "reference_texts": self.reference_texts,
            "existing_skill_md": self.existing_skill_md,
            "clarification_questions": self.clarification_questions,
            "clarification_answers": self.clarification_answers,
            "plan": self.plan,
            "generate_retries": self.generate_retries,
            "last_validate_error": self.last_validate_error,
            "skipped_benchmark_tests": self.skipped_benchmark_tests,
            "evals": self.evals,
            "eval_results": self.eval_results,
            "feedback_history": self.feedback_history,
            "external_tools": self.external_tools,
            "desc_optimize_result": self.desc_optimize_result,
            "zip_path": self.zip_path,
            "zip_size": self.zip_size,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
        }

    @classmethod
    def from_checkpoint_dict(cls, data: dict) -> "SkillDevState":
        """从持久化字典恢复状态."""
        state = cls(task_id=data["task_id"])
        checkpoint_stage = data["stage"]
        if checkpoint_stage == "plan":
            checkpoint_stage = SkillDevStage.GENERATE.value
        state.stage = SkillDevStage(checkpoint_stage)
        state.mode = SkillDevTaskMode(data.get("mode", "create"))
        state.iteration = data.get("iteration", 0)
        state.input = data.get("input", {})
        state.skill_name = data.get("skill_name", "")
        state.skill_dir_empty = data.get("skill_dir_empty", True)
        state.ref_files_dir_empty = data.get("ref_files_dir_empty", True)
        state.ref_skills_dir_empty = data.get("ref_skills_dir_empty", True)
        state.tool_scripts_dir_empty = data.get("tool_scripts_dir_empty", True)
        state.reference_texts = data.get("reference_texts", [])
        state.existing_skill_md = data.get("existing_skill_md")
        state.clarification_questions = data.get("clarification_questions", [])
        state.clarification_answers = data.get("clarification_answers", [])
        state.plan = data.get("plan")
        state.generate_retries = data.get("generate_retries", 0)
        state.last_validate_error = data.get("last_validate_error")
        state.skipped_benchmark_tests = data.get("skipped_benchmark_tests", False)
        state.evals = data.get("evals")
        state.eval_results = data.get("eval_results")
        state.feedback_history = data.get("feedback_history", [])
        state.external_tools = data.get("external_tools", [])
        state.desc_optimize_result = data.get("desc_optimize_result")
        state.zip_path = data.get("zip_path")
        state.zip_size = data.get("zip_size", 0)
        state.created_at = data.get("created_at", _now_iso())
        state.updated_at = data.get("updated_at", _now_iso())
        state.error = data.get("error")
        return state

    def to_status_dict(self) -> dict:
        """序列化为前端可展示的状态摘要."""
        return {
            "task_id": self.task_id,
            "stage": self.stage.value,
            "mode": self.mode.value,
            "iteration": self.iteration,
            "skill_name": self.skill_name,
            "plan": self.plan,
            "eval_results": self.eval_results,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# 挂起点配置
# ---------------------------------------------------------------------------


@dataclass
class SuspensionConfig:
    """挂起点的声明式配置.

    Pipeline 到达挂起点时：
    1. 推送 CONFIRM_REQUEST 事件（前端据此弹出确认框）
    2. Checkpoint 当前状态并暂停

    恢复时（前端通过 skilldev.respond 统一入口）：
    1. 调用 on_resume 更新状态
    2. 跳转到 next_stage
    """

    confirm_type: str  # 标识确认类型（前端用于区分弹框样式）
    title: str  # 弹框标题
    message: str  # 弹框描述文字
    actions: list[
        dict[str, str]
    ]  # 按钮列表 [{"id": "confirm", "label": "确认", "style": "primary"}]
    extract_data: Callable  # (state) → dict，从 state 提取展示给前端的数据
    on_resume: Callable  # (state, data) → None，根据用户响应更新 state
    next_stage: SkillDevStage | Callable  # 下一阶段（可以是函数，根据 data 动态决定）


# ---------------------------------------------------------------------------
# 各挂起点的 extract_data / on_resume / next_stage 实现
# ---------------------------------------------------------------------------


def _question_clarify_extract_data(state: SkillDevState) -> dict:
    return {"questions": state.clarification_questions}


def _question_clarify_on_resume(state: SkillDevState, data: dict) -> None:
    state.clarification_answers = data.get("answers", [])


def _review_extract_data(state: SkillDevState) -> dict:
    return {
        "benchmark": (state.eval_results or {}).get("benchmark"),
        "report": (state.eval_results or {}).get("report"),
        "iteration": state.iteration,
    }


def _review_on_resume(state: SkillDevState, data: dict) -> None:
    if data.get("feedback"):
        state.feedback_history.append(
            {
                "iteration": state.iteration,
                "feedback": data["feedback"],
            }
        )


def _review_next_stage(data: dict) -> SkillDevStage:
    action = data.get("action", "improve")
    return (
        SkillDevStage.IMPROVE
        if action == "improve"
        else SkillDevStage.DESC_OPTIMIZE_CONFIRM
    )


def _desc_opt_extract_data(state: SkillDevState) -> dict:
    plan = state.plan or {}
    return {"current_description": plan.get("description", "")}


def _desc_optimize_confirm_on_resume(state: SkillDevState, data: dict) -> None:
    pass


def _desc_optimize_confirm_next_stage(data: dict) -> SkillDevStage:
    action = data.get("action", "skip")
    return (
        SkillDevStage.DESC_OPTIMIZE
        if action == "optimize"
        else SkillDevStage.PACKAGE
    )


def _skip_tests_confirm_extract_data(state: SkillDevState) -> dict:
    plan = state.plan or {}
    desc = plan.get("description") or ""
    return {
        "skill_name": state.skill_name,
        "current_description": desc[:512] if desc else "",
    }


def _skip_tests_confirm_on_resume(state: SkillDevState, data: dict) -> None:
    action = data.get("action", "run_tests")
    state.skipped_benchmark_tests = action == "skip_tests"


def _skip_tests_confirm_next_stage(data: dict) -> SkillDevStage:
    action = data.get("action", "run_tests")
    if action == "skip_tests":
        return SkillDevStage.DESC_OPTIMIZE_CONFIRM
    return SkillDevStage.TEST_DESIGN


SUSPENSION_POINTS: dict[SkillDevStage, SuspensionConfig] = {
    SkillDevStage.QUESTION_CLARIFY: SuspensionConfig(
        confirm_type="question_clarify",
        title="请回答以下问题",
        message="AI 需要了解更多信息以生成精准的 Skill",
        actions=[
            {"id": "submit", "label": "提交回答", "style": "primary"},
        ],
        extract_data=_question_clarify_extract_data,
        on_resume=_question_clarify_on_resume,
        next_stage=SkillDevStage.GENERATE,
    ),
    SkillDevStage.REVIEW: SuspensionConfig(
        confirm_type="review",
        title="评测结果审阅",
        message="请审阅评测结果并决定下一步。选择「通过，继续」后，可先选择是否优化触发描述，再打包为 .skill。",
        actions=[
            {"id": "accept", "label": "通过，继续", "style": "primary"},
            {"id": "improve", "label": "继续改进", "style": "secondary"},
        ],
        extract_data=_review_extract_data,
        on_resume=_review_on_resume,
        next_stage=_review_next_stage,
    ),
    SkillDevStage.DESC_OPTIMIZE_CONFIRM: SuspensionConfig(
        confirm_type="desc_optimize_confirm",
        title="描述优化",
        message="打包前可选择是否优化 SKILL.md 中的 description，以提高触发准确率；跳过将直接进入打包。",
        actions=[
            {"id": "optimize", "label": "优化", "style": "primary"},
            {"id": "skip", "label": "跳过", "style": "secondary"},
        ],
        extract_data=_desc_opt_extract_data,
        on_resume=_desc_optimize_confirm_on_resume,
        next_stage=_desc_optimize_confirm_next_stage,
    ),
    SkillDevStage.SKIP_TESTS_CONFIRM: SuspensionConfig(
        confirm_type="skip_tests_confirm",
        title="测试流程",
        message="SKILL.md 已通过校验。你可以选择运行完整测试与评测流程，或跳过测试并进入打包前的描述优化确认。",
        actions=[
            {"id": "run_tests", "label": "运行测试", "style": "primary"},
            {"id": "skip_tests", "label": "跳过测试", "style": "secondary"},
        ],
        extract_data=_skip_tests_confirm_extract_data,
        on_resume=_skip_tests_confirm_on_resume,
        next_stage=_skip_tests_confirm_next_stage,
    ),
}


# ---------------------------------------------------------------------------
# 评测相关数据结构（对齐官方 skill-creator 的 JSON schema）
# ---------------------------------------------------------------------------


@dataclass
class EvalCase:
    """单个测试用例."""

    id: int
    prompt: str  # 模拟真实用户输入
    expected_output: str = ""  # 预期结果的人可读描述
    files: list[str] = field(default_factory=list)  # 输入文件路径
    expectations: list[str] = field(default_factory=list)  # 可客观验证的声明

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "expected_output": self.expected_output,
            "files": self.files,
            "expectations": self.expectations,
        }


@dataclass
class EvalSet:
    """完整的测试集."""

    skill_name: str
    evals: list[EvalCase] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "skill_name": self.skill_name,
            "evals": [e.to_dict() for e in self.evals],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EvalSet":
        return cls(
            skill_name=data.get("skill_name", ""),
            evals=[EvalCase(**e) for e in data.get("evals", [])],
        )


@dataclass
class GradingExpectation:
    """单条 assertion 的评分结果."""

    text: str  # assertion 原文
    passed: bool  # 是否通过
    evidence: str = ""  # 具体证据引用


@dataclass
class GradingResult:
    """单次运行的评分结果（grading.json）."""

    expectations: list[GradingExpectation] = field(default_factory=list)
    pass_rate: float = 0.0
    passed_count: int = 0
    failed_count: int = 0

    def to_dict(self) -> dict:
        return {
            "expectations": [
                {"text": e.text, "passed": e.passed, "evidence": e.evidence}
                for e in self.expectations
            ],
            "summary": {
                "passed": self.passed_count,
                "failed": self.failed_count,
                "total": self.passed_count + self.failed_count,
                "pass_rate": self.pass_rate,
            },
        }


@dataclass
class RunTiming:
    """单次运行的耗时数据（timing.json）."""

    total_tokens: int = 0
    duration_ms: int = 0
    total_duration_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "total_tokens": self.total_tokens,
            "duration_ms": self.duration_ms,
            "total_duration_seconds": self.total_duration_seconds,
        }


@dataclass
class MetricStats:
    """某指标的统计摘要."""

    mean: float = 0.0
    stddev: float = 0.0
    min: float = 0.0
    max: float = 0.0

    def to_dict(self) -> dict:
        return {
            "mean": self.mean,
            "stddev": self.stddev,
            "min": self.min,
            "max": self.max,
        }


@dataclass
class BenchmarkRun:
    """benchmark.json 中的一条 run 记录."""

    eval_id: int
    eval_name: str
    configuration: str  # "with_skill" | "baseline"
    run_number: int = 1
    pass_rate: float = 0.0
    time_seconds: float = 0.0
    tokens: int = 0
    expectations: list[dict] = field(default_factory=list)
    prompt: str = ""

    def to_dict(self) -> dict:
        return {
            "eval_id": self.eval_id,
            "eval_name": self.eval_name,
            "configuration": self.configuration,
            "run_number": self.run_number,
            "result": {
                "pass_rate": self.pass_rate,
                "time_seconds": self.time_seconds,
                "tokens": self.tokens,
            },
            "expectations": self.expectations,
            "prompt": self.prompt,
        }


@dataclass
class Benchmark:
    """完整的 benchmark 结果."""

    skill_name: str
    runs: list[BenchmarkRun] = field(default_factory=list)
    run_summary: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: _now_iso())

    def to_dict(self) -> dict:
        return {
            "metadata": {"skill_name": self.skill_name, "timestamp": self.timestamp},
            "runs": [r.to_dict() for r in self.runs],
            "run_summary": self.run_summary,
            "notes": self.notes,
        }


@dataclass
class TriggerEvalQuery:
    """描述优化阶段的单个触发测试查询."""

    query: str
    should_trigger: bool

    def to_dict(self) -> dict:
        return {"query": self.query, "should_trigger": self.should_trigger}


@dataclass
class DescOptimizeIteration:
    """描述优化的单轮迭代结果."""

    iteration: int
    description: str
    train_passed: int = 0
    train_total: int = 0
    test_passed: int | None = None
    test_total: int | None = None

    def to_dict(self) -> dict:
        d = {
            "iteration": self.iteration,
            "description": self.description,
            "train_passed": self.train_passed,
            "train_total": self.train_total,
        }
        if self.test_passed is not None:
            d["test_passed"] = self.test_passed
            d["test_total"] = self.test_total
        return d


# ---------------------------------------------------------------------------
# 阶段展示配置（后端驱动，决定哪些阶段对用户可见、如何分组）
# ---------------------------------------------------------------------------


@dataclass
class _StageGroup:
    """一组后端阶段的展示配置."""

    id: str
    label: str
    stages: frozenset[SkillDevStage]
    modes: frozenset[SkillDevTaskMode] | None = None  # None = 所有模式都展示


# 后端定义的阶段分组。前端只负责渲染，不决定内容。
# 挂起点（QUESTION_CLARIFY / SKIP_TESTS_CONFIRM / REVIEW / DESC_OPTIMIZE_CONFIRM）归入其所属的逻辑阶段。
_STAGE_GROUPS: list[_StageGroup] = [
    _StageGroup(
        id="plan",
        label="需求澄清",
        stages=frozenset(
            {
                SkillDevStage.INIT,
                SkillDevStage.CLARIFY,
                SkillDevStage.QUESTION_CLARIFY,
            }
        ),
    ),
    _StageGroup(
        id="generate",
        label="技能生成与校验",
        stages=frozenset({SkillDevStage.GENERATE, SkillDevStage.VALIDATE}),
    ),
    _StageGroup(
        id="test",
        label="测试与评测",
        stages=frozenset(
            {
                SkillDevStage.SKIP_TESTS_CONFIRM,
                SkillDevStage.TEST_DESIGN,
                SkillDevStage.TEST_RUN,
                SkillDevStage.EVALUATE,
            }
        ),
    ),
    _StageGroup(
        id="review",
        label="评测审阅",
        stages=frozenset({SkillDevStage.REVIEW}),
    ),
    _StageGroup(
        id="improve",
        label="优化改进",
        stages=frozenset({SkillDevStage.IMPROVE}),
    ),
    _StageGroup(
        id="desc_optimize",
        label="描述优化",
        stages=frozenset(
            {SkillDevStage.DESC_OPTIMIZE_CONFIRM, SkillDevStage.DESC_OPTIMIZE}
        ),
    ),
    _StageGroup(
        id="package",
        label="打包",
        stages=frozenset({SkillDevStage.PACKAGE}),
    ),
]


def compute_todos(
    current_stage: SkillDevStage,
    mode: SkillDevTaskMode | None = None,
) -> list[dict[str, str]]:
    """根据当前阶段和任务模式，计算面向用户的 Todo 列表.

    后端是步骤定义的唯一权威来源。前端只做渲染。
    """
    groups = _STAGE_GROUPS
    if mode is not None:
        groups = [g for g in groups if g.modes is None or mode in g.modes]

    if current_stage == SkillDevStage.COMPLETED:
        return [{"id": g.id, "label": g.label, "status": "completed"} for g in groups]
    if current_stage == SkillDevStage.ERROR:
        return [{"id": g.id, "label": g.label, "status": "cancelled"} for g in groups]

    found_current = False
    result: list[dict[str, str]] = []
    for g in groups:
        if current_stage in g.stages:
            status = "in_progress"
            found_current = True
        elif found_current:
            status = "pending"
        else:
            status = "completed"
        result.append({"id": g.id, "label": g.label, "status": status})
    return result


# ---------------------------------------------------------------------------
# SKILL.md 校验相关常量
# ---------------------------------------------------------------------------

ALLOWED_FRONTMATTER_KEYS = frozenset(
    {
        "name",
        "description",
        "license",
        "allowed-tools",
        "metadata",
        "compatibility",
    }
)

SKILL_NAME_MAX_LEN = 64
SKILL_DESC_MAX_LEN = 1024
MAX_GENERATE_RETRIES = 3  # GENERATE ↔ VALIDATE 最大重试次数


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """返回当前中国时区（Asia/Shanghai）时间的 ISO 8601 字符串."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def generate_task_id() -> str:
    """生成唯一 task_id，格式：sd_{timestamp}_{random}."""
    import secrets

    ts = int(time.time())
    rand = secrets.token_hex(4)
    return f"sd_{ts}_{rand}"


# def determine_task_mode(params: dict) -> SkillDevTaskMode:
#     """根据请求参数自动判断任务模式."""
#     resources = params.get("resources", [])
#     has_existing = any(r.get("type") == "existing_skill" for r in resources)
#     if has_existing:
#         return SkillDevTaskMode.MODIFY
#     if resources:
#         return SkillDevTaskMode.CREATE_WITH_RESOURCES
#     return SkillDevTaskMode.CREATE
