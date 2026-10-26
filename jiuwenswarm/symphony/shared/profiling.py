from __future__ import annotations

import logging
from contextlib import contextmanager
from time import perf_counter


class StageTimer:
    def __init__(self, scope: str, *, logger: logging.Logger | None = None) -> None:
        self._scope = str(scope or "").strip() or "unknown"
        self._logger = logger or logging.getLogger("index_builder")
        self._start = perf_counter()
        self._phases: list[tuple[str, float]] = []

    @contextmanager
    def phase(self, name: str):
        phase_name = str(name or "").strip() or "phase"
        phase_start = perf_counter()
        try:
            yield
        finally:
            elapsed_ms = (perf_counter() - phase_start) * 1000.0
            self._phases.append((phase_name, elapsed_ms))
            self._logger.info(
                "timing | scope=%s | phase=%s | elapsed_ms=%.2f",
                self._scope,
                phase_name,
                elapsed_ms,
            )

    def finish(self) -> None:
        total_ms = (perf_counter() - self._start) * 1000.0
        phases_summary = "; ".join(f"{name}:{elapsed:.2f}ms" for name, elapsed in self._phases) or "-"
        self._logger.info(
            "timing | scope=%s | total_ms=%.2f | phases=%s",
            self._scope,
            total_ms,
            phases_summary,
        )

