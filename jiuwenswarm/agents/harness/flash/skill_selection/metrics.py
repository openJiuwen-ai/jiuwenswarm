"""Per-invocation metrics in default and retrieval modes; no prompt/body logging."""
from dataclasses import dataclass, field
from contextvars import ContextVar
import logging
import time
from uuid import uuid4

from .diagnostics import emit

logger = logging.getLogger(__name__)
KEY = 'flash.skill_selection.metrics'
FIELDS = ('input_tokens', 'cache_tokens', 'output_tokens', 'reasoning_tokens')


@dataclass
class RequestMetrics:
    request_id: str = field(default_factory=lambda: uuid4().hex)
    started: float = field(default_factory=time.perf_counter)
    model_started: float | None = None
    selection_started: float | None = None
    query_sha256: str | None = None
    mode: str = 'unknown'
    fallback: bool = False
    model_calls: int = 0
    model_errors: int = 0
    usage_samples: int = 0
    model_ms: float = 0.0
    totals: dict = field(default_factory=lambda: dict.fromkeys(FIELDS, 0))
    first_load_ms: float | None = None
    closed: bool = False
    contexts: list = field(default_factory=list, repr=False)

    def snapshot(self):
        complete = self.model_calls > 0 and self.usage_samples == self.model_calls
        totals = self.totals if self.usage_samples else dict.fromkeys(FIELDS)
        return dict(mode=self.mode, fallback=self.fallback,
                    model_calls=self.model_calls, model_errors=self.model_errors,
                    usage_samples=self.usage_samples, usage_complete=complete,
                    model_ms=self.model_ms, first_load_ms=self.first_load_ms,
                    **totals, uncached_input_tokens=(max(0, self.totals['input_tokens'] - self.totals['cache_tokens'])
                                                    if self.usage_samples else None))


def metrics_for(ctx):
    return ctx.extra.setdefault(KEY, RequestMetrics())


class MetricsBridge:
    """Join outer DeepAgent lifecycle and inner ReAct callbacks without SDK edits.

    Context inheritance distinguishes concurrent calls. A unique session entry
    also handles the SDK's pre-existing task-loop worker; ambiguous sessions are
    never combined. The table is scoped to this optional rail instance.
    """
    def __init__(self):
        self.active = {}
        self.current = ContextVar('flash_request_metrics', default=None)

    @staticmethod
    def session_key(ctx):
        session = getattr(ctx, 'session', None)
        getter = getattr(session, 'get_session_id', None)
        return ('session', getter()) if callable(getter) else ('object', id(session))

    def begin(self, ctx):
        metrics = metrics_for(ctx)
        key = self.session_key(ctx)
        self.active.setdefault(key, {})[metrics.request_id] = metrics
        ctx.extra[KEY + '.token'] = self.current.set((key, metrics))
        self.attach(ctx)

    def attach(self, ctx):
        metrics = ctx.extra.get(KEY)
        if metrics is None:
            key = self.session_key(ctx)
            current = self.current.get()
            if current and current[0] == key and not current[1].closed:
                metrics = current[1]
            else:
                active = self.active.get(key, {})
                metrics = next(iter(active.values())) if len(active) == 1 else RequestMetrics()
            ctx.extra[KEY] = metrics
        if all(related is not ctx for related in metrics.contexts):
            metrics.contexts.append(ctx)
        return metrics

    def finish(self, ctx, outcome):
        metrics = ctx.extra.get(KEY)
        if metrics is None:
            return [ctx]
        contexts = list(metrics.contexts) or [ctx]
        finish(ctx, outcome)
        key = self.session_key(ctx)
        active = self.active.get(key, {})
        active.pop(metrics.request_id, None)
        if not active:
            self.active.pop(key, None)
        token = ctx.extra.pop(KEY + '.token', None)
        if token is not None:
            try:
                self.current.reset(token)
            except ValueError:
                # Async-generator cancellation can finalize in another context.
                # The closed flag prevents reuse in the original context too.
                pass
        metrics.contexts.clear()
        for related in contexts:
            related.extra.pop(KEY, None)
        return contexts


def trace(ctx, metrics):
    getter = getattr(getattr(ctx, 'session', None), 'get_session_id', None)
    return {'request_id': metrics.request_id, 'session_id': getter() if callable(getter) else None,
            'query_sha256': metrics.query_sha256}


def record_model(ctx, *, failed=False):
    metrics = ctx.extra.get(KEY)
    if metrics is None or metrics.closed or metrics.model_started is None:
        return
    call_ms = (time.perf_counter() - metrics.model_started) * 1000
    metrics.model_ms += call_ms
    metrics.model_started = None
    metrics.model_calls += 1
    metrics.model_errors += int(failed)
    response = getattr(ctx.inputs, 'response', None)
    usage = getattr(response, 'usage_metadata', None)
    values = None
    if usage is not None:
        values = {key: usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None) for key in FIELDS}
        if all(isinstance(value, (int, float)) and value >= 0 for value in values.values()):
            metrics.usage_samples += 1
            for key, value in values.items():
                metrics.totals[key] += value
    emit(logger, 'request_progress', trace(ctx, metrics), **metrics.snapshot(),
         call_stage=ctx.extra.get('flash.skill_selection.model_stage'), call_ms=call_ms, call_usage=values)


def record_load(ctx):
    metrics = ctx.extra.get(KEY)
    if metrics is None or metrics.first_load_ms is not None or ctx.inputs.tool_name != 'skill_tool':
        return
    output = getattr(ctx.inputs, 'tool_result', None)
    if not getattr(output, 'success', False):
        return
    metrics.first_load_ms = (time.perf_counter() - (metrics.selection_started or metrics.started)) * 1000
    emit(logger, 'selection_stage', trace(ctx, metrics), **metrics.snapshot())


def finish(ctx, outcome):
    metrics = ctx.extra.pop(KEY, None)
    if metrics is not None and not metrics.closed:
        if metrics.model_started is not None:
            # The SDK skips AFTER_MODEL_CALL for cancellation. Count the attempt
            # as missing usage, never as a free or completed model call.
            metrics.model_ms += (time.perf_counter() - metrics.model_started) * 1000
            metrics.model_started = None
            metrics.model_calls += 1
            metrics.model_errors += 1
            outcome = 'interrupted_or_failed'
        metrics.closed = True
        emit(logger, 'request_summary', trace(ctx, metrics), outcome=outcome,
             elapsed_ms=(time.perf_counter() - metrics.started) * 1000, **metrics.snapshot())
