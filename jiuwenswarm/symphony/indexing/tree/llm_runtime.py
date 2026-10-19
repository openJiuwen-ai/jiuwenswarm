from __future__ import annotations

import hashlib
from contextlib import suppress
from typing import Optional, TYPE_CHECKING

from shared.rich_compat import Console, Panel

from .schema import parse_json_from_response

try:
    from openai import APIConnectionError, APIError, APITimeoutError, AuthenticationError
except ModuleNotFoundError:
    APIConnectionError = APIError = APITimeoutError = AuthenticationError = None

if TYPE_CHECKING:
    from .builder import TreeBuilder


console = Console()


def _builder_attr(builder: "TreeBuilder", name: str, default=None):
    return getattr(builder, name, default)


def _set_builder_attr(builder: "TreeBuilder", name: str, value) -> None:
    setattr(builder, name, value)


class TreeLLMRuntime:
    """Owns model limits, retries, cache observability, and JSON parsing retries."""

    def __init__(self, builder: "TreeBuilder") -> None:
        self._builder = builder

    def auto_batch_size(self) -> int:
        builder = self._builder
        batch_size_cache = _builder_attr(builder, "_batch_size_cache")
        if batch_size_cache is not None:
            return batch_size_cache
        ctx_window, _ = self.model_limits()
        available = ctx_window - builder.PROMPT_OVERHEAD_TOKENS - builder.OUTPUT_RESERVE_TOKENS
        batch_size = available // builder.AVG_TOKENS_PER_SKILL
        batch_size_cache = max(50, min(batch_size, 1000))
        _set_builder_attr(builder, "_batch_size_cache", batch_size_cache)
        return batch_size_cache

    def get_max_output_tokens(self) -> int:
        builder = self._builder
        max_output_tokens_cache = _builder_attr(builder, "_max_output_tokens_cache")
        if max_output_tokens_cache is not None:
            return max_output_tokens_cache
        _, max_out = self.model_limits()
        max_output_tokens_cache = min(int(max_out), 4096)
        _set_builder_attr(builder, "_max_output_tokens_cache", max_output_tokens_cache)
        return max_output_tokens_cache

    def merged_extra_body(self) -> dict:
        merged = {
            "thinking": {"type": "disabled"},
            "chat_template_kwargs": {"enable_thinking": False},
            "temperature": 0.0,
            "top_p": 1.0,
        }
        llm_seed = _builder_attr(self._builder, "_llm_seed")
        if llm_seed is not None:
            with suppress(Exception):
                merged["seed"] = int(llm_seed)
        return merged

    def model_limits(self) -> tuple[int, int]:
        builder = self._builder
        model_name = (builder.model or "").lower()
        extended_context_markers = ("gpt-4.1", "gpt-4o", "claude", "doubao")
        if any(marker in model_name for marker in extended_context_markers):
            return 128000, 32768
        if "gpt-5" in model_name:
            return 200000, 65536
        return builder.DEFAULT_CONTEXT_WINDOW, builder.DEFAULT_MAX_OUTPUT_TOKENS

    @staticmethod
    def normalize_prompt_for_fingerprint(prompt: str) -> str:
        normalized_lines = []
        for line in prompt.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            normalized_lines.append(line.rstrip())
        return "\n".join(normalized_lines).strip()

    def prompt_fingerprint(self, prompt: str) -> str:
        builder = self._builder
        pieces = ["v1", builder.model or "", self.normalize_prompt_for_fingerprint(prompt)]
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

    @staticmethod
    def _response_metadata_candidates(response) -> list[dict]:
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
        cache_hits = int(_builder_attr(builder, "_cache_hits", 0) or 0)
        cache_misses = int(_builder_attr(builder, "_cache_misses", 0) or 0)
        cache_unknown = int(_builder_attr(builder, "_cache_unknown", 0) or 0)
        llm_calls = int(_builder_attr(builder, "_llm_calls", 0) or 0)
        retry_calls = int(_builder_attr(builder, "_retry_calls", 0) or 0)
        prompt_fingerprints = _builder_attr(builder, "_prompt_fingerprints", set()) or set()
        known_total = cache_hits + cache_misses
        observed_hit_rate = (cache_hits / known_total * 100.0) if known_total else 0.0
        lower_bound_hit_rate = (cache_hits / llm_calls * 100.0) if llm_calls else 0.0
        metrics = {
            "LLM calls": llm_calls,
            "Retry calls": retry_calls,
            "Cache hits/misses/unknown": f"{cache_hits}/{cache_misses}/{cache_unknown}",
            "Observed hit rate (known only)": f"{observed_hit_rate:.1f}%",
            "Estimated hit rate lower bound": f"{lower_bound_hit_rate:.1f}%",
            "Unique prompt fingerprints": len(prompt_fingerprints),
        }
        lines = [f"{label}: {value}" for label, value in metrics.items()]
        console.print(Panel("\n".join(lines), title="[bold cyan]Cache Stats[/bold cyan]", border_style="cyan"))

    def call_llm(self, prompt: str, is_retry: bool = False, retry_left: int | None = None) -> str:
        builder = self._builder
        client = _builder_attr(builder, "_client")
        if client is None:
            raise RuntimeError("openai is required to build the tree. Please install the openai package first.")
        mcfg = _builder_attr(builder, "_manager_config")
        if retry_left is None:
            retry_left = int(mcfg.build.num_retries)
        max_tokens = self.get_max_output_tokens()
        prompt_fingerprint = self.prompt_fingerprint(prompt)
        counter_lock = _builder_attr(builder, "_counter_lock")
        with counter_lock:
            llm_calls = int(_builder_attr(builder, "_llm_calls", 0) or 0) + 1
            _set_builder_attr(builder, "_llm_calls", llm_calls)
            if is_retry:
                retry_calls = int(_builder_attr(builder, "_retry_calls", 0) or 0) + 1
                _set_builder_attr(builder, "_retry_calls", retry_calls)
            _builder_attr(builder, "_prompt_fingerprints").add(prompt_fingerprint)
            progress = _builder_attr(builder, "_progress")
            progress_task = _builder_attr(builder, "_progress_task")
            if progress and progress_task is not None:
                progress.update(progress_task, llm=llm_calls)
        try:
            with _builder_attr(builder, "_llm_semaphore"):
                response = client.chat.completions.create(
                    model=builder.model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=max_tokens,
                    timeout=mcfg.build.timeout,
                    stream=False,
                    extra_body=self.merged_extra_body(),
                )
            finish_reason = response.choices[0].finish_reason
            thread_local = _builder_attr(builder, "_thread_local")
            if finish_reason == "length":
                thread_local.truncated = True
                console.print(
                    Panel(
                        "[bold red]OUTPUT TRUNCATED![/bold red]\n"
                        f"The LLM response was cut off at {max_tokens} tokens (finish_reason='length').\n"
                        "This will cause incomplete JSON parsing and skill loss.\n"
                        "Consider reducing batch size or increasing max_tokens.",
                        title="[bold red]Truncation Warning[/bold red]",
                        border_style="red",
                    )
                )
            else:
                thread_local.truncated = False
            content = response.choices[0].message.content
            if not content:
                raise RuntimeError("empty response from skill index build model")
            with counter_lock:
                _set_builder_attr(builder, "_consecutive_failures", 0)
                self.record_cache_observation(None)
            return content
        except Exception as e:
            if AuthenticationError is not None and isinstance(e, AuthenticationError):
                console.print("[red]Authentication failed - check API key[/red]")
                raise
            err_text = str(e).lower()
            is_context_exceeded = any(
                marker in err_text for marker in ("context length", "maximum context", "too many tokens", "max context")
            )
            if is_context_exceeded:
                console.print(f"[red]Context window exceeded: {e}[/red]")
                batch_size_cache = _builder_attr(builder, "_batch_size_cache")
                if batch_size_cache and batch_size_cache > 50:
                    batch_size_cache = max(50, batch_size_cache // 2)
                    _set_builder_attr(builder, "_batch_size_cache", batch_size_cache)
                    console.print(f"[yellow]Reduced batch size to {batch_size_cache}[/yellow]")
                with counter_lock:
                    consecutive_failures = int(_builder_attr(builder, "_consecutive_failures", 0) or 0) + 1
                    _set_builder_attr(builder, "_consecutive_failures", consecutive_failures)
                    if consecutive_failures >= builder.max_consecutive_failures:
                        raise RuntimeError(f"Circuit breaker: {consecutive_failures} consecutive LLM failures") from e
                raise RuntimeError(
                    f"Skill index build model call exceeded the model context window: {e}"
                ) from e
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
            with counter_lock:
                consecutive_failures = int(_builder_attr(builder, "_consecutive_failures", 0) or 0) + 1
                _set_builder_attr(builder, "_consecutive_failures", consecutive_failures)
                if consecutive_failures >= builder.max_consecutive_failures:
                    raise RuntimeError(f"Circuit breaker: {consecutive_failures} consecutive LLM failures") from e
            raise RuntimeError(f"Skill index build model call failed: {e}") from e

    def call_llm_json(self, prompt: str, max_retries: int = 3, is_retry: bool = False) -> dict:
        builder = self._builder
        attempts_remaining = max_retries
        attempt_index = 0
        thread_local = _builder_attr(builder, "_thread_local")
        while attempts_remaining > 0:
            thread_local.truncated = False
            response = self.call_llm(prompt, is_retry=is_retry or attempt_index > 0)
            if getattr(thread_local, "truncated", False):
                raise RuntimeError(
                    "Skill index build model output was truncated before a complete JSON object was produced."
                )
            parse_failed = object()
            parsed = parse_json_from_response(response, default=parse_failed)
            if isinstance(parsed, dict):
                return parsed
            console.print(
                f"[yellow]Expected a JSON object but received {type(parsed).__name__} "
                f"(attempt {attempt_index + 1}/{max_retries})[/yellow]"
            )
            attempt_index += 1
            attempts_remaining -= 1
        raise RuntimeError(
            f"Skill index build model did not return a valid JSON object after {max_retries} attempts."
        )
