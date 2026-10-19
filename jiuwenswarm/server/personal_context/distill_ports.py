"""Host-owned distill port wiring for PersonalContext Core.

Builds the corpus and runner that ``PersonalContextHostAPI`` injects via
``set_distill_corpus`` / ``set_distill_runner``.  Does not own the distill
scheduler loop; Core starts that from ``activate_runtime``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from openjiuwen.harness.personal_context.distill import (
    CorpusPort,
    DistillRunResult,
    LlmPort,
    SqliteImCorpus,
    run_distill_job,
)


def build_distill_corpus(home: str | Path) -> SqliteImCorpus:
    """Return a SQLite IM corpus bound to one PersonalContext home."""

    return SqliteImCorpus(str(home))


def build_distill_runner(
    *,
    home: str | Path,
    corpus: CorpusPort,
    resolve_llm: Callable[[], LlmPort],
) -> Callable[..., object]:
    """Return a DistillRunnerPort-compatible callable over ``run_distill_job``.

    ``resolve_llm`` is invoked on each run so the Host can bind the current
    model selection; it must raise when no usable model is available.
    """

    home_str = str(home)

    async def _run(
        home: str,
        *,
        window_end_ms: int,
        learning_since_ms: int | None = None,
        max_messages: int = 800,
        force_full_window: bool = False,
    ) -> DistillRunResult:
        del home  # Protocol passes home; binding uses construction-time home_str.
        llm = resolve_llm()
        return await run_distill_job(
            home_str,
            window_end_ms=window_end_ms,
            learning_since_ms=learning_since_ms,
            max_messages=max_messages,
            corpus=corpus,
            llm=llm,
            force_full_window=force_full_window,
        )

    return _run


__all__ = ["build_distill_corpus", "build_distill_runner"]
