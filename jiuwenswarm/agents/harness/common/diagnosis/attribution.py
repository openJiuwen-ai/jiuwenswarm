# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""步级失败归因器：从规范化 StepView 序列判断哪一步导致失败。"""

from __future__ import annotations

import abc
import json
import random
import re
from dataclasses import dataclass
from typing import Any, Optional

from jiuwenswarm.agents.harness.common.diagnosis.trajectory_view import (
    StepView,
    render_steps,
)


@dataclass(frozen=True)
class AttributionResult:
    """单次归因结果。``step_index`` 为 1-based；None 表示无法归因。"""

    attributor: str
    step_index: Optional[int]
    total_steps: int
    rationale: str = ""
    raw_response: str = ""
    error: str = ""


class StepAttributor(abc.ABC):
    """步级归因器抽象基类。"""

    name: str = "base"

    @abc.abstractmethod
    async def attribute(
        self,
        steps: list[StepView],
        *,
        task: str,
        final_answer: str = "",
        expected_answer: str = "",
    ) -> AttributionResult:
        """根据步级视图归因失败步骤。"""


def _empty_result(attributor: str) -> AttributionResult:
    return AttributionResult(
        attributor=attributor,
        step_index=None,
        total_steps=0,
        rationale="",
        error="empty_steps",
    )


class LastStepAttributor(StepAttributor):
    """恒返回最后一步，用作无需推理的启发式对照基线。"""

    name = "last_step"

    async def attribute(
        self,
        steps: list[StepView],
        *,
        task: str,
        final_answer: str = "",
        expected_answer: str = "",
    ) -> AttributionResult:
        if not steps:
            return _empty_result(self.name)
        last = steps[-1]
        return AttributionResult(
            attributor=self.name,
            step_index=last.index,
            total_steps=len(steps),
            rationale="baseline: always attribute to the last step",
        )


class RandomAttributor(StepAttributor):
    """在 ``[1, L]`` 上均匀采样；使用实例级 RNG，不污染全局随机状态。"""

    name = "random"

    def __init__(self, seed: int = 0) -> None:
        self._rng = random.Random(seed)

    async def attribute(
        self,
        steps: list[StepView],
        *,
        task: str,
        final_answer: str = "",
        expected_answer: str = "",
    ) -> AttributionResult:
        if not steps:
            return _empty_result(self.name)
        n = len(steps)
        idx = self._rng.randint(1, n)
        return AttributionResult(
            attributor=self.name,
            step_index=idx,
            total_steps=n,
            rationale=f"uniform sample in [1, {n}]",
        )


class OracleAttributor(StepAttributor):
    """恒返回构造时传入的真实位置，作归因上界。"""

    name = "oracle"

    def __init__(self, true_index: int) -> None:
        self._true_index = true_index

    async def attribute(
        self,
        steps: list[StepView],
        *,
        task: str,
        final_answer: str = "",
        expected_answer: str = "",
    ) -> AttributionResult:
        if not steps:
            return _empty_result(self.name)
        return AttributionResult(
            attributor=self.name,
            step_index=self._true_index,
            total_steps=len(steps),
            rationale="oracle true index",
        )


_DETAILS_RE = re.compile(
    r"<details\b[^>]*>.*?</details>",
    flags=re.DOTALL | re.IGNORECASE,
)
_THINK_RE = re.compile(
    r"<think\b[^>]*>.*?</think>",
    flags=re.DOTALL | re.IGNORECASE,
)
_FENCE_RE = re.compile(
    r"^```(?:json)?\s*\n?(.*?)\n?```\s*$",
    flags=re.DOTALL | re.IGNORECASE,
)
_JSON_OBJ_RE = re.compile(r"\{[^{}]*\}", flags=re.DOTALL)


def _strip_reasoning_blocks(raw: str) -> str:
    """剥离推理模型内联的 ``<details>`` / ``<think>`` 块。"""
    if not raw:
        return raw
    text = _DETAILS_RE.sub("", raw)
    text = _THINK_RE.sub("", text)
    return text.strip()


def _strip_markdown_fence(raw: str) -> str:
    """剥最外层 markdown 代码围栏。"""
    text = raw.strip()
    m = _FENCE_RE.match(text)
    if m:
        return m.group(1).strip()
    return text


def _parse_attribution_json(raw: str) -> tuple[Optional[dict[str, Any]], str]:
    """健壮解析归因 JSON；失败返回 (None, error)。不抛异常。"""
    if not isinstance(raw, str) or not raw.strip():
        return None, "empty_response"
    text = _strip_reasoning_blocks(raw)
    text = _strip_markdown_fence(text)
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj, ""
        return None, "json_not_object"
    except (json.JSONDecodeError, TypeError):
        pass
    m = _JSON_OBJ_RE.search(text)
    if not m:
        return None, "json_parse_failed"
    try:
        obj = json.loads(m.group(0))
    except (json.JSONDecodeError, TypeError) as exc:
        return None, f"json_fallback_failed:{exc}"
    if isinstance(obj, dict):
        return obj, ""
    return None, "json_fallback_not_object"


class LLMTrajectoryAttributor(StepAttributor):
    """整条轨迹交给 LLM 归因，模拟框架默认做法。

    ``uniform_hint=True`` 时额外要求均匀考虑每一步（论文消融项），
    此时 ``name`` 为 ``llm_whole_trajectory+hint``。
    """

    def __init__(
        self,
        llm: Any,
        model: str,
        *,
        uniform_hint: bool = False,
        temperature: float = 0.0,
        timeout: int = 120,
    ) -> None:
        self._llm = llm
        self._model = model
        self._uniform_hint = uniform_hint
        self._temperature = temperature
        self._timeout = timeout
        self.name = (
            "llm_whole_trajectory+hint" if uniform_hint else "llm_whole_trajectory"
        )

    def _build_prompt(
        self,
        steps: list[StepView],
        *,
        task: str,
        final_answer: str,
        expected_answer: str,
    ) -> str:
        steps_text = render_steps(steps)
        parts = [
            "You are diagnosing which step caused an agent task failure.",
            f"Task:\n{task}",
            f"Numbered steps:\n{steps_text}",
        ]
        if final_answer:
            parts.append(f"Final answer:\n{final_answer}")
        if expected_answer:
            parts.append(f"Expected answer:\n{expected_answer}")
        if self._uniform_hint:
            parts.append(
                "The steps are numbered. Consider every step uniformly; "
                "do not default to blaming the last step."
            )
        parts.append(
            "Reply with ONLY a JSON object of the form "
            '{"step": <int>, "reason": "<short>"} '
            "where step is the 1-based index of the failing step."
        )
        return "\n\n".join(parts)

    async def attribute(
        self,
        steps: list[StepView],
        *,
        task: str,
        final_answer: str = "",
        expected_answer: str = "",
    ) -> AttributionResult:
        if not steps:
            return _empty_result(self.name)
        n = len(steps)
        prompt = self._build_prompt(
            steps,
            task=task,
            final_answer=final_answer,
            expected_answer=expected_answer,
        )
        raw = ""
        try:
            resp = await self._llm.invoke(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                temperature=self._temperature,
                timeout=self._timeout,
            )
            raw = getattr(resp, "content", None)
            if raw is None:
                raw = str(resp)
            else:
                raw = raw if isinstance(raw, str) else str(raw)
        except Exception as exc:  # noqa: BLE001 — 归因失败不得向上抛
            return AttributionResult(
                attributor=self.name,
                step_index=None,
                total_steps=n,
                raw_response="",
                error=f"llm_invoke_failed:{type(exc).__name__}:{exc}",
            )

        obj, err = _parse_attribution_json(raw)
        if obj is None:
            return AttributionResult(
                attributor=self.name,
                step_index=None,
                total_steps=n,
                raw_response=raw,
                error=err or "parse_failed",
            )

        step_val = obj.get("step")
        reason = str(obj.get("reason") or "")
        try:
            step_index = int(step_val)
        except (TypeError, ValueError):
            return AttributionResult(
                attributor=self.name,
                step_index=None,
                total_steps=n,
                rationale=reason,
                raw_response=raw,
                error=f"invalid_step:{step_val!r}",
            )

        if step_index < 1 or step_index > n:
            return AttributionResult(
                attributor=self.name,
                step_index=None,
                total_steps=n,
                rationale=reason,
                raw_response=raw,
                error=f"step_out_of_range:{step_index}",
            )

        return AttributionResult(
            attributor=self.name,
            step_index=step_index,
            total_steps=n,
            rationale=reason,
            raw_response=raw,
        )
