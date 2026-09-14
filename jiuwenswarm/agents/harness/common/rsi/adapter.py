"""引擎对接适配层（内部 v3 §5 / adapter 契约 v1 §2）。

- ``RsiEngineAdapter``：6 方法抽象（build_request / run / resume / read_state / read_report / validate_input），
  ``run``/``resume`` 支持 ``on_event`` 事件 sink（内部 v3 §5.1 注入形态：run 时注入、每任务一个）。
- ``RsiEventSink`` = ``Callable[[EngineEvent], Awaitable[None]]``。
- Harness/Artifact 两实现同签名；artifact 场景仅预留签名（Xiangda）。
- 注意：当前 agent-core 引擎 ``run()`` 尚无 ``on_event`` 形参（事件化为 Zhiting ⚠️外部）；
  本模块定义契约 + 组装辅助，真实装配在 C5。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from jiuwenswarm.agents.harness.common.rsi.errors import RsiPathInvalid, RsiScenarioNotSupported
from jiuwenswarm.agents.harness.common.rsi.events import EngineEvent
from jiuwenswarm.agents.harness.common.rsi.models import (
    ArtifactType,
    RsiDatasetResult,
    RsiTaskView,
    Scenario,
)

#: 引擎事件 sink：异步回调，队列由 AgentServer 闭包捕获（内部 v3 §5.1）
RsiEventSink = Callable[[EngineEvent], Awaitable[None]]


@runtime_checkable
class EngineState(Protocol):
    """read_state 规范化返回（恢复通道，内部 v3 §5.1 / adapter 契约 §2.1.4）。"""

    status: str
    best_score: float | None
    baseline_score: float | None
    current_harness_refs_path: str
    best_harness_refs_path: str
    published_harness_refs_path: str
    retained_case_ids: list[str]
    candidate_gates: list[dict[str, Any]]
    epoch_checkpoints: list[dict[str, Any]]
    raw: dict[str, Any]


@runtime_checkable
class EngineReport(Protocol):
    """read_report 规范化返回（恢复通道，adapter 契约 §2.1.5）。"""

    status: str
    best_score: float | None
    baseline_score: float | None
    candidate_count: int
    accepted_candidate_count: int
    published_harness_refs_path: str
    raw: dict[str, Any]


class RsiEngineAdapter(Protocol):
    """场景差异收敛点：harness / artifact 各实现一份，服务域零场景分支。"""

    def build_request(self, task: RsiTaskView, *, resume: bool = False) -> Any:
        """把内部任务组装为引擎可接受请求对象（内部 v3 §5.1）。"""

    async def run(
        self,
        request: Any,
        *,
        on_event: RsiEventSink | None = None,
    ) -> Any:
        """全量执行。on_event 为可选事件 sink（run 时注入）。"""

    async def resume(
        self,
        request: Any,
        *,
        on_event: RsiEventSink | None = None,
    ) -> Any:
        """断点续跑。引擎侧校验 fingerprint；不匹配抛错 → 服务层映射 RESUME_MISMATCH。"""

    def read_state(self, task_id: str) -> EngineState:
        """恢复通道：读引擎 state 文件（启动/重启/对账）。"""

    def read_report(self, task_id: str) -> EngineReport:
        """恢复通道：读引擎 report 文件。"""

    def validate_input(
        self,
        path: str,
        *,
        scenario: str,
        artifact_type: str | None = None,
    ) -> RsiDatasetResult:
        """校验输入文件格式/内容（场景语义由引擎侧实现）。"""


# ---------------------------------------------------------------------------
# 场景分派（内部 v3 §3.1 路由：按 task 关联 scenario 选择 adapter）
# ---------------------------------------------------------------------------


def engine_event_sink_from_queue(queue: Any) -> RsiEventSink:
    """构造 sink 闭包：``q.put_nowait(evt)``（内部 v3 §4.2 事件链路）。

    Args:
        queue: asyncio.Queue（有界，maxsize≈128）。
    """

    async def _sink(event: EngineEvent) -> None:
        queue.put_nowait(event)

    return _sink


def validate_scenario(
    scenario: str,
    artifact_type: str | None = None,
    *,
    artifact_type_required: bool = False,
) -> tuple[Scenario | None, ArtifactType | None]:
    """按 web §3.5 语义校验 scenario/artifact_type 组合。

    返回规范化枚举；非法抛 ``RsiScenarioNotSupported``；ARTIFACT 缺 artifact_type
    且要求必填时抛 ``RsiBadRequest``。供 create / list / validate 复用。
    """
    from jiuwenswarm.agents.harness.common.rsi.errors import RsiBadRequest

    scenario_value = str(scenario or "").strip().upper()
    artifact_value = str(artifact_type or "").strip().upper() or None
    try:
        scenario_enum = Scenario(scenario_value)
    except ValueError as exc:
        raise RsiScenarioNotSupported(f"scenario 非法: {scenario}") from exc
    artifact_enum: ArtifactType | None = None
    if artifact_value:
        try:
            artifact_enum = ArtifactType(artifact_value)
        except ValueError as exc:
            raise RsiScenarioNotSupported(f"artifact_type 非法: {artifact_type}") from exc
    if scenario_enum is Scenario.ARTIFACT and artifact_type_required and artifact_enum is None:
        raise RsiBadRequest("artifact 场景必填 artifact_type")
    if scenario_enum is Scenario.HARNESS and artifact_enum is not None:
        raise RsiBadRequest("harness 场景不接受 artifact_type")
    return scenario_enum, artifact_enum


def default_validate_input(path: str) -> RsiDatasetResult:
    """边界版数据集校验（⚠️外部：引擎真实 ``_load_cases`` 暴露后由 adapter 覆盖）。

    当前只做：路径存在 + 可读 + JSON 顶层形状（dict/list）。格式语义归引擎侧，
    故 shape 校验通过但不给 sample_count 真值（返回 None 由调用方兜底）。
    """
    errors: list[dict[str, str]] = []
    p = Path(path).expanduser()
    if not p.is_file():
        raise RsiPathInvalid(f"本地路径不存在: {p}")
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        errors.append({"reason": f"数据集 JSON 解析失败: {exc}", "code": "DATASET_INVALID"})
        return RsiDatasetResult(valid=False, sample_count=None, errors=errors)
    if not (isinstance(data, (dict, list))):
        errors.append({"reason": "数据集顶层必须是 object 或 array", "code": "DATASET_INVALID"})
        return RsiDatasetResult(valid=False, sample_count=None, errors=errors)
    cases = data.get("cases") if isinstance(data, dict) else data
    if isinstance(cases, list):
        sample_count = len(cases)
    else:
        sample_count = 1 if isinstance(data, dict) else None
    if not isinstance(cases, list):
        errors.append({"reason": "数据集缺少 cases 数组", "code": "DATASET_INVALID"})
        return RsiDatasetResult(valid=False, sample_count=sample_count, errors=errors)
    return RsiDatasetResult(valid=True, sample_count=sample_count, errors=[])


def raise_not_ready(what: str) -> None:
    """⚠️外部/预留路径统一抛出：显式“接口已落位但依赖未就绪”。"""
    from jiuwenswarm.agents.harness.common.rsi.errors import RsiNotReady

    raise RsiNotReady(what)
