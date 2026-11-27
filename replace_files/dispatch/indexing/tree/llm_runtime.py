# pylint: disable=line-too-long,protected-access,raise-missing-from,too-many-boolean-expressions
# pylint: disable=add-staticmethod-or-classmethod-decorator,broad-except
from __future__ import annotations

import hashlib
from contextlib import suppress
from typing import Optional, TYPE_CHECKING

from ...shared.rich_compat import Console, Panel

from .schema import parse_json_from_response

try:
    from openai import APIConnectionError, APIError, APITimeoutError, AuthenticationError
except ModuleNotFoundError:  # pragma: no cover
    APIConnectionError = APIError = APITimeoutError = AuthenticationError = None

if TYPE_CHECKING:
    from .builder import TreeBuilder


console = Console()


class TreeLLMRuntime:
    """Owns model limits, retries, cache observability, and JSON parsing retries."""

    def __init__(self, builder: "TreeBuilder") -> None:
        self._builder = builder

    def auto_batch_size(self) -> int:
        builder = self._builder
        if builder._batch_size_cache is not None:
            return builder._batch_size_cache
        ctx_window, _ = self.model_limits()
        available = ctx_window - builder.PROMPT_OVERHEAD_TOKENS - builder.OUTPUT_RESERVE_TOKENS
        batch_size = available // builder.AVG_TOKENS_PER_SKILL
        builder._batch_size_cache = max(50, min(batch_size, 1000))
        return builder._batch_size_cache

    def get_max_output_tokens(self) -> int:
        builder = self._builder
        if builder._max_output_tokens_cache is not None:
            return builder._max_output_tokens_cache
        _, max_out = self.model_limits()
        max_output_override = int(getattr(builder._manager_config.build, "max_output_tokens", 0) or 0)
        if max_output_override > 0:
            builder._max_output_tokens_cache = max_output_override
        else:
            builder._max_output_tokens_cache = min(int(max_out), 4096)
        return builder._max_output_tokens_cache

    def merged_extra_body(self) -> dict:
        merged = {
            "thinking": {"type": "disabled"},
            "chat_template_kwargs": {"enable_thinking": False},
            "temperature": 0.0,
            "top_p": 1.0,
        }
        if self._builder._llm_seed is not None:
            with suppress(Exception):
                merged["seed"] = int(self._builder._llm_seed)
        return merged

    def model_limits(self) -> tuple[int, int]:
        builder = self._builder
        ctx_cfg = int(getattr(builder._manager_config.build, "context_window", 0) or 0)
        out_cfg = int(getattr(builder._manager_config.build, "max_output_tokens", 0) or 0)
        if ctx_cfg > 0 and out_cfg > 0:
            return ctx_cfg, out_cfg

        model_name = (builder.model or "").lower()
        if "gpt-4.1" in model_name or "gpt-4o" in model_name or "claude" in model_name or "doubao" in model_name:
            return 128000, 32768
        if "gpt-5" in model_name:
            return 200000, 65536
        return builder.DEFAULT_CONTEXT_WINDOW, builder.DEFAULT_MAX_OUTPUT_TOKENS

    def normalize_prompt_for_fingerprint(self, prompt: str) -> str:
        normalized_lines = []
        for line in prompt.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            normalized_lines.append(line.rstrip())
        return "\n".join(normalized_lines).strip()

    def prompt_fingerprint(self, prompt: str) -> str:
        builder = self._builder
        pieces = [builder._prompt_fingerprint_version, builder.model or "", self.normalize_prompt_for_fingerprint(prompt)]
        digest_input = "\n".join(str(piece) for piece in pieces)
        return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:16]

    def extract_cache_hit(self, response) -> Optional[bool]:
        for mapping in self._response_metadata_candidates(response):
            parsed = self.extract_cache_hit_from_mapping(mapping)
            if parsed is not None:
                return parsed
        return None

    def extract_cache_hit_from_mapping(self, mapping: dict) -> Optional[bool]:
        aliases = {"cache_hit", "cachehit", "is_cached", "cached", "x-litellm-cache-hit", "litellm_cache_hit"}
        pending = [mapping]
        while pending:
            candidate = pending.pop(0)
            if not isinstance(candidate, dict):
                continue
            for raw_key, raw_value in candidate.items():
                key = str(raw_key).strip().lower()
                if key in aliases:
                    coerced = self._coerce_cache_flag(raw_value)
                    if coerced is not None:
                        return coerced
                if isinstance(raw_value, dict):
                    pending.append(raw_value)
        return None

    def _response_metadata_candidates(self, response) -> list[dict]:
        candidates: list[dict] = []
        for attr_name in ("_hidden_params", "_response_headers"):
            value = getattr(response, attr_name, None)
            if isinstance(value, dict):
                candidates.append(value)
        model_dump = getattr(response, "model_dump", None)
        if callable(model_dump):
            try:
                dumped = model_dump()
            except Exception:
                dumped = None
            if isinstance(dumped, dict):
                candidates.append(dumped)
        return candidates

    @staticmethod
    def _coerce_cache_flag(value) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "hit", "yes"}:
                return True
            if normalized in {"0", "false", "miss", "no"}:
                return False
        return None

    def record_cache_observation(self, cache_hit: Optional[bool]) -> None:
        builder = self._builder
        bucket_name = "unknown"
        if cache_hit is True:
            bucket_name = "hits"
        elif cache_hit is False:
            bucket_name = "misses"
        attr_name = f"_cache_{bucket_name}"
        setattr(builder, attr_name, getattr(builder, attr_name) + 1)

    def print_cache_stats(self) -> None:
        builder = self._builder
        if not builder._cache_observability:
            return
        known_total = builder._cache_hits + builder._cache_misses
        observed_hit_rate = (builder._cache_hits / known_total * 100.0) if known_total else 0.0
        lower_bound_hit_rate = (builder._cache_hits / builder._llm_calls * 100.0) if builder._llm_calls else 0.0
        metrics = {
            "LLM calls": builder._llm_calls,
            "Retry calls": builder._retry_calls,
            "Cache hits/misses/unknown": f"{builder._cache_hits}/{builder._cache_misses}/{builder._cache_unknown}",
            "Observed hit rate (known only)": f"{observed_hit_rate:.1f}%",
            "Estimated hit rate lower bound": f"{lower_bound_hit_rate:.1f}%",
            "Unique prompt fingerprints": len(builder._prompt_fingerprints),
        }
        lines = [f"{label}: {value}" for label, value in metrics.items()]
        console.print(Panel("\n".join(lines), title="[bold cyan]Cache Stats[/bold cyan]", border_style="cyan"))

    def call_llm(self, prompt: str, is_retry: bool = False, retry_left: int | None = None) -> str:
        builder = self._builder
        if builder._client is None:
            raise RuntimeError("openai is required to build the tree. Please install the openai package first.")
        mcfg = builder._manager_config
        if retry_left is None:
            retry_left = int(mcfg.build.num_retries)
        max_tokens = self.get_max_output_tokens()
        prompt_fingerprint = self.prompt_fingerprint(prompt)
        with builder._counter_lock:
            builder._llm_calls += 1
            if is_retry:
                builder._retry_calls += 1
            if builder._cache_observability:
                builder._prompt_fingerprints.add(prompt_fingerprint)
            if builder._progress and builder._progress_task is not None:
                builder._progress.update(builder._progress_task, llm=builder._llm_calls)
        try:
            with builder._llm_semaphore:
                response = builder._client.chat.completions.create(
                    model=builder.model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=max_tokens,
                    timeout=mcfg.build.timeout,
                    extra_body=self.merged_extra_body(),
                )
            finish_reason = response.choices[0].finish_reason
            if finish_reason == "length":
                builder._thread_local.truncated = True
                console.print(Panel(
                    "[bold red]OUTPUT TRUNCATED![/bold red]\n"
                    f"The LLM response was cut off at {max_tokens} tokens (finish_reason='length').\n"
                    "This will cause incomplete JSON parsing and skill loss.\n"
                    "Consider reducing batch size or increasing max_tokens.",
                    title="[bold red]Truncation Warning[/bold red]",
                    border_style="red",
                ))
            else:
                builder._thread_local.truncated = False
            with builder._counter_lock:
                builder._consecutive_failures = 0
                if builder._cache_observability:
                    self.record_cache_observation(None)
            return response.choices[0].message.content or "{}"
        except Exception as e:
            if AuthenticationError is not None and isinstance(e, AuthenticationError):
                console.print("[red]Authentication failed - check API key[/red]")
                raise
            err_text = str(e).lower()
            is_context_exceeded = any(
                marker in err_text
                for marker in ("context length", "maximum context", "too many tokens", "max context")
            )
            if is_context_exceeded:
                console.print(f"[red]Context window exceeded: {e}[/red]")
                if builder._batch_size_cache and builder._batch_size_cache > 50:
                    builder._batch_size_cache = max(50, builder._batch_size_cache // 2)
                    console.print(f"[yellow]Reduced batch size to {builder._batch_size_cache}[/yellow]")
                with builder._counter_lock:
                    builder._consecutive_failures += 1
                    if builder._consecutive_failures >= builder.MAX_CONSECUTIVE_FAILURES:
                        raise RuntimeError(
                            f"Circuit breaker: {builder._consecutive_failures} consecutive LLM failures"
                        )
                return "{}"
            is_transient = (
                (APITimeoutError is not None and isinstance(e, APITimeoutError))
                or (APIConnectionError is not None and isinstance(e, APIConnectionError))
                or (APIError is not None and isinstance(e, APIError))
                or "timed out" in err_text
                or "timeout" in err_text
            )
            if is_transient and retry_left > 0:
                return self.call_llm(prompt, is_retry=True, retry_left=retry_left - 1)
            console.print(f"[red]LLM call failed: {e}[/red]")
            with builder._counter_lock:
                builder._consecutive_failures += 1
                if builder._consecutive_failures >= builder.MAX_CONSECUTIVE_FAILURES:
                    raise RuntimeError(
                        f"Circuit breaker: {builder._consecutive_failures} consecutive LLM failures"
                    )
            return "{}"

    def call_llm_json(self, prompt: str, max_retries: int = 3, is_retry: bool = False) -> dict:
        builder = self._builder
        attempts_remaining = max_retries
        attempt_index = 0
        while attempts_remaining > 0:
            builder._thread_local.truncated = False
            response = self.call_llm(prompt, is_retry=is_retry or attempt_index > 0)
            parsed = parse_json_from_response(response, default={})
            if isinstance(parsed, dict):
                return parsed
            if getattr(builder._thread_local, "truncated", False):
                console.print("[yellow]Skipping retry because the model output was truncated[/yellow]")
                return {}
            console.print(
                f"[yellow]Expected a JSON object but received {type(parsed).__name__} "
                f"(attempt {attempt_index + 1}/{max_retries})[/yellow]"
            )
            attempt_index += 1
            attempts_remaining -= 1
        console.print("[red]All retries exhausted, returning empty dict[/red]")
        return {}
