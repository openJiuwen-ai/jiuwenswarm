"""Apply injected trace header exporters at upstream model call boundaries."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import Any, Protocol

from openjiuwen.core.foundation.llm import Model

from jiuwenswarm.common.subagent_logging import model_observation

from .models import InvocationContext, TraceContext
from .runtime import get_current_invocation_context


class TraceHeaderExporter(Protocol):
    """Platform adapter that exports approved headers for one invocation."""

    def supports(self, invocation: InvocationContext) -> bool: ...

    def export(self, trace: TraceContext) -> dict[str, str]: ...


_REGISTERED_TRACE_HEADER_EXPORTERS: dict[str, TraceHeaderExporter] = {}


def register_trace_header_exporter(name: str, exporter: TraceHeaderExporter) -> None:
    """Register one platform adapter at the server composition boundary."""
    normalized = str(name or "").strip().lower()
    if not normalized:
        raise ValueError("exporter name is required")
    _REGISTERED_TRACE_HEADER_EXPORTERS[normalized] = exporter


def export_trace_headers_for_name(
    trace: TraceContext | None,
    exporter_name: str | None,
) -> dict[str, str]:
    """Export headers through the named platform adapter, if registered."""
    exporter = _REGISTERED_TRACE_HEADER_EXPORTERS.get(
        str(exporter_name or "").strip().lower()
    )
    return dict(exporter.export(trace)) if trace is not None and exporter is not None else {}


def export_trace_headers(
    invocation: InvocationContext | None,
    exporters: Iterable[TraceHeaderExporter],
) -> dict[str, str]:
    """Export the active trace through exporters selected by composition."""
    if invocation is None or invocation.trace is None:
        return {}
    for exporter in exporters:
        if exporter.supports(invocation):
            return dict(exporter.export(invocation.trace))
    return {}


class TraceAwareModel(Model):
    """Model subclass that adds headers from explicitly injected exporters."""

    def __init__(
        self,
        *args: Any,
        trace_header_exporters: Iterable[TraceHeaderExporter] = (),
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._trace_header_exporters = tuple(trace_header_exporters)

    def _with_trace_headers(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """注入 trace 头后的 kwargs（x-hag-trace-id = 裸核心段，与计费上报同值）。"""
        custom_headers = dict(kwargs.get("custom_headers") or {})
        # 调用方显式携带 x-hag-trace-id 时不覆盖、不改写
        #（与 model-proxy 的 hasTraceId 语义一致）
        if any(
            key.lower() == "x-hag-trace-id" and str(value).strip()
            for key, value in custom_headers.items()
        ):
            kwargs["custom_headers"] = custom_headers
            return kwargs
        trace_headers = export_trace_headers(
            get_current_invocation_context(),
            self._trace_header_exporters,
        )
        if not trace_headers:
            return kwargs
        kwargs["custom_headers"] = {**custom_headers, **trace_headers}
        return kwargs

    async def invoke(self, *args: Any, **kwargs: Any) -> Any:
        observed_kwargs = {"messages": args[0], **kwargs} if args else kwargs
        with model_observation(self, observed_kwargs, streaming=False) as observation:
            result = await super().invoke(*args, **self._with_trace_headers(kwargs))
            if observation is not None:
                observation.response(result)
            return result

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        observed_kwargs = {"messages": args[0], **kwargs} if args else kwargs
        with model_observation(self, observed_kwargs, streaming=True) as observation:
            async for chunk in super().stream(*args, **self._with_trace_headers(kwargs)):
                if observation is not None:
                    observation.response(chunk)
                yield chunk


__all__ = [
    "TraceAwareModel",
    "TraceHeaderExporter",
    "export_trace_headers",
    "export_trace_headers_for_name",
    "register_trace_header_exporter",
]
