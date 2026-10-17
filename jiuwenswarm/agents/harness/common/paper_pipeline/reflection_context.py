"""Keep the reflection prompt within the model context when metrics carry raw per-item data.

agent-core ``modules/reflection/agent.py`` (``ReflectionAgent._build_task_prompt``) renders every
variant's metrics as ``k=v`` with the full ``repr`` of each value. Experiment code is free to put
anything in ``*.metrics.json``; ours stored per-question records and cross-arm tables there
(0.6-1.6 MB per variant), so with 11 variants the "Per-variant results" block alone was ~9 MB of
text (~2.4M tokens) and every reflection attempt failed with HTTP 400 (context length exceeded).

The fix leaves the agent untouched and hands ``_build_task_prompt`` a copy of the inputs whose
metrics keep scalars and small nested values as they are, and replace large nested values with a
one-line placeholder. The original files on disk are not modified.
"""

from __future__ import annotations

import json
from typing import Any

MAX_INLINE_CHARS = 400
MAX_VARIANT_CHARS = 6000
_MARKER = "_jiuwenswarm_compact_metrics"


def _describe(value: Any) -> str:
    if isinstance(value, dict):
        return f"dict with {len(value)} keys"
    if isinstance(value, list):
        return f"list of {len(value)} items"
    return f"{type(value).__name__} of {len(str(value))} chars"


def compact_metrics(metrics: dict[str, Any], *, max_inline_chars: int = MAX_INLINE_CHARS,
                    max_total_chars: int = MAX_VARIANT_CHARS) -> dict[str, Any]:
    """Scalars stay; a nested value stays only if its JSON fits ``max_inline_chars`` and the variant's
    running total stays under ``max_total_chars``; everything else becomes a short placeholder.
    """
    compact: dict[str, Any] = {}
    used = 0
    for key, value in metrics.items():
        if value is None or isinstance(value, (bool, int, float)):
            compact[key] = value
            used += len(key) + 12
            continue
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        if len(text) <= max_inline_chars and used + len(text) <= max_total_chars:
            compact[key] = value
            used += len(key) + len(text)
        else:
            compact[key] = f"<omitted: {_describe(value)}, too large for the prompt>"
            used += len(key) + 60
    return compact


def compact_reflection_inputs(inputs: Any) -> Any:
    result = inputs.result
    variants = [variant.model_copy(update={"metrics": compact_metrics(variant.metrics)})
                for variant in result.variants]
    return inputs.model_copy(update={"result": result.model_copy(update={"variants": variants})})


def install_reflection_context_fix() -> bool:
    """Wrap ``ReflectionAgent._build_task_prompt`` so it sees compacted metrics. Idempotent."""
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reflection.agent import ReflectionAgent

    current = ReflectionAgent.__dict__["_build_task_prompt"].__func__
    if getattr(current, _MARKER, False):
        return False

    def _build_task_prompt(inputs, *args, **kwargs):
        return current(compact_reflection_inputs(inputs), *args, **kwargs)

    _build_task_prompt.__wrapped__ = current
    setattr(_build_task_prompt, _MARKER, True)
    setattr(ReflectionAgent, "_build_task_prompt", staticmethod(_build_task_prompt))
    return True
