# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Fast-fail guard for user inputs that cannot fit the model input budget.

This gate answers one question and nothing else: *can this single user input
ever fit, no matter how much history is compressed or dropped?* It is a
product-level policy, deliberately narrower than the SDK's prompt-too-long
(PTL) recovery:

* history is excluded from the budget because it is compressible/truncatable;
* only ``system prompt + tools + reserved output`` is subtracted from the
  window;
* the newest user input is protected by every compressor, so an input that
  exceeds the remaining budget can never be recovered downstream.

The module is pure: no IO, no SDK imports, no global state. Callers pass the
already-resolved window/token facts in :class:`InputBudget`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

DEFAULT_MAX_INPUT_RATIO = 0.95
DEFAULT_PRECISE_MAX_INPUT_RATIO = 0.99
# Default stand-in for system prompt + tool definitions when the live agent
# instance is not reachable yet (the first message of a session runs on the
# stateless root adapter). Configurable as ``fixed_overhead_tokens``; a live
# measurement is cached per model and preferred on later static paths.
DEFAULT_FIXED_OVERHEAD_TOKENS = 30_000
DEFAULT_MESSAGE_ZH = (
    "本次输入约 {tokens} tokens，超过当前模型可用输入上限（约 {limit} tokens），"
    "已拦截，未发送。请拆分为多次发送，或把长文保存到文件后告诉我路径，我按需读取。"
)
DEFAULT_MESSAGE_EN = (
    "This input is about {tokens} tokens, which exceeds the model's available "
    "input limit (~{limit} tokens). It was not sent. Please split it into "
    "multiple messages, or save the long text to a file and tell me the path "
    "so I can read it on demand."
)

# CJK ranges that should be counted as roughly one token per character. The
# SDK's string-length fallback (``len // 3``) underestimates Chinese by about
# 2-3x, which would let an oversized input through this gate.
_CJK_RANGES = (
    (0x2E80, 0x2EFF),   # CJK radicals supplement
    (0x3000, 0x303F),   # CJK punctuation
    (0x3040, 0x30FF),   # Hiragana / Katakana
    (0x3400, 0x4DBF),   # CJK unified ideographs extension A
    (0x4E00, 0x9FFF),   # CJK unified ideographs
    (0xF900, 0xFAFF),   # CJK compatibility ideographs
    (0xFF00, 0xFFEF),   # Fullwidth forms
    (0x20000, 0x2FA1F),  # CJK extensions B-F and compatibility supplement
)
_STRING_LENGTH_FALLBACK_SOURCE = "string_length_fallback"
# ``tiktoken_fallback`` borrows another model's vocabulary; the family
# tokenizer fallback is close but still not the exact model artifact. Both
# keep the conservative ratio.
_IMPRECISE_COUNTER_SOURCES = {
    _STRING_LENGTH_FALLBACK_SOURCE,
    "tiktoken_fallback",
    "family_tokenizer_fallback",
}
_CJK_PATTERN = re.compile(
    "[" + "".join(f"{chr(start)}-{chr(end)}" for start, end in _CJK_RANGES) + "]"
)


class InputTooLongError(RuntimeError):
    """Raised when one user input cannot fit the model input budget.

    The message is user-facing; the numeric fields exist for logging and for
    callers that want to render their own notice.
    """

    def __init__(
        self,
        message: str,
        *,
        tokens: int,
        limit: int,
        window: int,
        system_tokens: int,
        tools_tokens: int,
        reserved_tokens: int,
    ) -> None:
        super().__init__(message)
        self.tokens = tokens
        self.limit = limit
        self.window = window
        self.system_tokens = system_tokens
        self.tools_tokens = tools_tokens
        self.reserved_tokens = reserved_tokens


@dataclass(frozen=True)
class InputGuardConfig:
    """Parsed ``react.input_guard_config`` block.

    ``enabled`` defaults to false so an unconfigured deployment keeps its
    current behaviour. Malformed values fall back to the defaults instead of
    failing startup: this gate must never turn a config typo into an outage.
    """

    enabled: bool = False
    max_input_ratio: float = DEFAULT_MAX_INPUT_RATIO
    # A precise tokenizer leaves much less counting error, so it can use a
    # tighter margin without rejecting inputs that would fit.
    precise_max_input_ratio: float = DEFAULT_PRECISE_MAX_INPUT_RATIO
    max_input_tokens: int = 0
    reserved_output_tokens: int = 0
    # Opt-in: reserve the session model's ``max_tokens`` for output. Off by
    # default because that value is often the model's maximum capability, not
    # the reserve of the actual request.
    use_model_max_tokens_as_reserve: bool = False
    # Only used when the live system prompt / tool token counts are not
    # available and no measurement has been cached yet.
    fixed_overhead_tokens: int = DEFAULT_FIXED_OVERHEAD_TOKENS
    message_zh: str = DEFAULT_MESSAGE_ZH
    message_en: str = DEFAULT_MESSAGE_EN

    def message(self, language: str, *, tokens: int, limit: int) -> str:
        template = (
            self.message_en
            if str(language or "").strip().lower().startswith("en")
            else self.message_zh
        )
        return _format_template(template, tokens=tokens, limit=limit)


@dataclass(frozen=True)
class InputBudget:
    """Window facts needed to judge one input.

    ``usable`` is what remains for the input after fixed costs. History is
    intentionally not part of this budget: it can be compressed or dropped.
    """

    window: int
    system_tokens: int = 0
    tools_tokens: int = 0
    reserved_tokens: int = 0

    @property
    def usable(self) -> int:
        return max(self.window - self.system_tokens - self.tools_tokens - self.reserved_tokens, 0)


@dataclass(frozen=True)
class InputGuardDecision:
    """Outcome of one evaluation.

    ``reason`` is one of ``"disabled"``, ``"pass"``, ``"reject"`` or
    ``"budget_exhausted"``. ``budget_exhausted`` means the fixed costs already
    fill the window; that is a model/configuration problem, so the input is
    not blamed and the request proceeds to the SDK safety net.
    """

    rejected: bool
    tokens: int
    limit: int
    usable: int
    window: int
    system_tokens: int
    tools_tokens: int
    reserved_tokens: int
    reason: str


def parse_input_guard_config(raw: Any) -> InputGuardConfig:
    """Parse the YAML/API value into a validated config.

    Accepts ``None``, a mapping, or an object exposing ``model_dump``. Unknown
    keys are ignored for forward compatibility.
    """
    mapping = _as_mapping(raw)
    if not mapping:
        return InputGuardConfig()
    return InputGuardConfig(
        enabled=_parse_bool(mapping.get("enabled"), False),
        max_input_ratio=_parse_ratio(mapping.get("max_input_ratio"), DEFAULT_MAX_INPUT_RATIO),
        precise_max_input_ratio=_parse_ratio(
            mapping.get("precise_max_input_ratio"), DEFAULT_PRECISE_MAX_INPUT_RATIO
        ),
        max_input_tokens=_parse_non_negative_int(mapping.get("max_input_tokens"), 0),
        reserved_output_tokens=_parse_non_negative_int(mapping.get("reserved_output_tokens"), 0),
        use_model_max_tokens_as_reserve=_parse_bool(
            mapping.get("use_model_max_tokens_as_reserve"), False
        ),
        fixed_overhead_tokens=_parse_non_negative_int(
            mapping.get("fixed_overhead_tokens"), DEFAULT_FIXED_OVERHEAD_TOKENS
        ),
        message_zh=_parse_template(mapping.get("message_zh"), DEFAULT_MESSAGE_ZH),
        message_en=_parse_template(mapping.get("message_en"), DEFAULT_MESSAGE_EN),
    )


def estimate_text_tokens(text: str, token_counter: Any = None) -> int:
    """Estimate tokens for one text, preferring a precise counter.

    The SDK may hand back a string-length fallback (``len // 3``) when no
    tokenizer artifact is configured. That fallback underestimates CJK by
    roughly 3x, so it is replaced by a CJK-aware estimate. Precise counters
    (native tokenizer / tiktoken) are used as-is.
    """
    if not isinstance(text, str) or not text:
        return 0
    if is_precise_counter(token_counter):
        try:
            counted = int(token_counter.count(text))
        except Exception:  # noqa: BLE001 - estimation must never block a request
            counted = 0
        if counted > 0:
            return counted
    return _heuristic_text_tokens(text)


def evaluate_input_guard(
    query: str,
    *,
    config: InputGuardConfig,
    budget: InputBudget,
    token_counter: Any = None,
) -> InputGuardDecision:
    """Return the decision for one input. Never raises.

    A precise counter gets ``precise_max_input_ratio``; the heuristic gets the
    more conservative ``max_input_ratio``. The absolute cap applies on top.
    """
    if not config.enabled:
        return _decision(False, 0, 0, budget, "disabled")

    tokens = estimate_text_tokens(query, token_counter)
    usable = budget.usable
    if usable <= 0:
        return _decision(False, tokens, 0, budget, "budget_exhausted")

    ratio = (
        config.precise_max_input_ratio
        if is_precise_counter(token_counter)
        else config.max_input_ratio
    )
    limit = int(usable * ratio)
    if config.max_input_tokens > 0:
        limit = min(limit, config.max_input_tokens)
    limit = max(limit, 0)

    rejected = tokens > limit
    return _decision(rejected, tokens, limit, budget, "reject" if rejected else "pass")


def _decision(
    rejected: bool,
    tokens: int,
    limit: int,
    budget: InputBudget,
    reason: str,
) -> InputGuardDecision:
    return InputGuardDecision(
        rejected=rejected,
        tokens=tokens,
        limit=limit,
        usable=budget.usable,
        window=budget.window,
        system_tokens=budget.system_tokens,
        tools_tokens=budget.tools_tokens,
        reserved_tokens=budget.reserved_tokens,
        reason=reason,
    )


def is_precise_counter(counter: Any) -> bool:
    """Whether ``counter`` reports model-accurate tokens.

    The string-length fallback (``len // 3``) and a tiktoken fallback that
    borrows another model's vocabulary are treated as imprecise. Custom
    counters that do not report a source keep their own result.
    """
    if counter is None or not callable(getattr(counter, "count", None)):
        return False
    return getattr(counter, "measurement_source", "") not in _IMPRECISE_COUNTER_SOURCES


def _heuristic_text_tokens(text: str) -> int:
    """CJK-aware estimate: one token per CJK character, ~4 chars per token otherwise."""
    if text.isascii():
        return (len(text) + 3) // 4
    cjk = sum(1 for _ in _CJK_PATTERN.finditer(text))
    other = len(text) - cjk
    return cjk + (other + 3) // 4


def _format_template(template: str, *, tokens: int, limit: int) -> str:
    try:
        return template.format(tokens=f"{max(int(tokens), 0):,}", limit=f"{max(int(limit), 0):,}")
    except (KeyError, IndexError, ValueError):
        return DEFAULT_MESSAGE_ZH.format(tokens=f"{max(int(tokens), 0):,}", limit=f"{max(int(limit), 0):,}")


def _parse_template(value: Any, default: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return default
    try:
        value.format(tokens="0", limit="0")
    except (KeyError, IndexError, ValueError):
        return default
    return value


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            dumped = dump(exclude_none=True)
        except TypeError:
            dumped = dump()
        if isinstance(dumped, Mapping):
            return dumped
    return {}


def _parse_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _parse_ratio(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return default
    return ratio if 0.0 < ratio <= 1.0 else default


def _parse_non_negative_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


__all__ = [
    "DEFAULT_FIXED_OVERHEAD_TOKENS",
    "DEFAULT_MAX_INPUT_RATIO",
    "DEFAULT_MESSAGE_EN",
    "DEFAULT_MESSAGE_ZH",
    "DEFAULT_PRECISE_MAX_INPUT_RATIO",
    "InputBudget",
    "InputGuardConfig",
    "InputGuardDecision",
    "InputTooLongError",
    "estimate_text_tokens",
    "evaluate_input_guard",
    "is_precise_counter",
    "parse_input_guard_config",
]
