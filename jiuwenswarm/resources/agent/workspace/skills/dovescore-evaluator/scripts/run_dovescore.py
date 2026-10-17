"""Run DoveScore on a source/target text pair."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)
DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_MAX_INPUT_CHARS = 20000


class EvaluationTimeoutError(TimeoutError):
    """Raised when DoveScore evaluation exceeds the configured deadline."""


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


def _emit_result(rendered: str) -> None:
    result_logger = logging.getLogger("dovescore.result")
    if not result_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        result_logger.addHandler(handler)
    result_logger.setLevel(logging.INFO)
    result_logger.propagate = False
    result_logger.info("%s", rendered)


def _read_text(value: str | None, file_path: str | None, label: str) -> str:
    if value and file_path:
        raise ValueError(f"Pass either --{label} or --{label}-file, not both.")
    if file_path:
        return _safe_workspace_path(file_path, f"{label}-file").read_text(encoding="utf-8")
    if value:
        return value
    raise ValueError(f"Missing input: pass --{label} or --{label}-file.")


def _safe_workspace_path(raw_path: str, label: str) -> Path:
    root = Path.cwd().resolve()
    path = Path(raw_path)
    if not raw_path or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Unsafe --{label} path: {raw_path!r}")
    candidate = (root / path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"--{label} path escapes the current workspace: {raw_path!r}") from exc
    return candidate


def _flatten_text(text: str) -> str:
    return " ".join(text.split())


def _check_input_size(source: str, target: str, max_input_chars: int) -> None:
    total_chars = len(source) + len(target)
    if total_chars > max_input_chars:
        raise ValueError(
            "Source and target are too large for this safety limit: "
            f"{total_chars} chars > {max_input_chars}. "
            "Pass --max-input-chars to raise the limit intentionally."
        )


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    value_module = value.__class__.__module__
    if value_module == "numpy" or value_module.startswith("numpy."):
        if hasattr(value, "tolist"):
            return _json_ready(value.tolist())
        item = getattr(value, "item", None)
        if callable(item):
            return _json_ready(item())
    return value


def _emit_json_result(result: dict[str, Any], output: str | None, pretty: bool) -> int:
    indent = 2 if pretty else None
    rendered = json.dumps(result, ensure_ascii=False, indent=indent)
    try:
        if output:
            output_path = _safe_workspace_path(output, "output")
            output_path.write_text(rendered + "\n", encoding="utf-8")
    except OSError as exc:
        logger.error("Failed to write output file: %s", exc)
        return 2
    except ValueError as exc:
        logger.error("%s", exc)
        return 2
    _emit_result(rendered)
    return 0


@contextmanager
def _evaluation_deadline(timeout_seconds: int):
    if timeout_seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    def _handle_timeout(_signum: int, _frame: Any) -> None:
        raise EvaluationTimeoutError(
            f"DoveScore evaluation timed out after {timeout_seconds} seconds."
        )

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _handle_timeout)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])


def _score_value(result: dict[str, Any], key: str) -> float | None:
    value = result.get(key)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _alignment_level(score: float | None) -> str:
    if score is None:
        return "unknown"
    if score >= 0.85:
        return "high"
    if score >= 0.60:
        return "medium"
    return "low"


def _summarize_result(result: dict[str, Any], include_details: bool) -> dict[str, Any]:
    total_score = _score_value(result, "total_score")
    event_score = _score_value(result, "event_score")
    order_score = _score_value(result, "order_score")
    descriptive_score = _score_value(result, "descriptive_score")
    summary: dict[str, Any] = {
        "metric": "dovescore",
        "total_score": total_score,
        "alignment_level": _alignment_level(total_score),
        "event_score": event_score,
        "order_score": order_score,
        "descriptive_score": descriptive_score,
        "interpretation": (
            "DoveScore evaluates whether the target is supported by the source, "
            "including factual alignment and event-order consistency."
        ),
        "note": "DoveScore is an information-alignment metric, not a fluency or style score.",
    }
    if include_details:
        summary["details"] = result
    return summary


def _demo_result() -> dict[str, Any]:
    return {
        "demo": "dovescore_contrast",
        "question": "Does the target faithfully preserve the source facts?",
        "source": (
            "The Eiffel Tower is in Paris. It was completed in 1889 for the "
            "Exposition Universelle."
        ),
        "target": (
            "The Eiffel Tower is in Paris. It was completed in 1989 for the "
            "Exposition Universelle."
        ),
        "without_skill": {
            "likely_judgment": "Looks faithful because almost every word overlaps.",
            "missed_problem": "The year changed from 1889 to 1989.",
        },
        "with_dovescore": {
            "metric": "dovescore",
            "total_score": 0.5,
            "alignment_level": "low",
            "event_score": 1.0,
            "order_score": 1.0,
            "descriptive_score": 0.0,
            "finding": "The target is fluent and similar, but one descriptive fact is unsupported.",
        },
        "takeaway": (
            "DoveScore catches source-target factual mismatches that surface similarity "
            "or quick reading can miss."
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate source-target information alignment with DoveScore."
    )
    parser.add_argument("--source", help="Reference/source text.")
    parser.add_argument("--target", help="Target text to evaluate.")
    parser.add_argument("--source-file", help="UTF-8 file containing source text.")
    parser.add_argument("--target-file", help="UTF-8 file containing target text.")
    parser.add_argument(
        "--api-key",
        default=_first_env("DOVESCORE_API_KEY", "OPENAI_API_KEY", "API_KEY", "MODEL_API_KEY"),
        help=(
            "OpenAI-compatible API key. Defaults to DOVESCORE_API_KEY, "
            "OPENAI_API_KEY, API_KEY, or MODEL_API_KEY."
        ),
    )
    parser.add_argument("--backbone", default="gpt-4o-mini", help="OpenAI model name.")
    parser.add_argument(
        "--base-url",
        default=_first_env("DOVESCORE_BASE_URL", "OPENAI_BASE_URL", "API_BASE", "MODEL_API_BASE"),
        help=(
            "OpenAI-compatible API base URL. Defaults to DOVESCORE_BASE_URL, "
            "OPENAI_BASE_URL, API_BASE, or MODEL_API_BASE."
        ),
    )
    parser.add_argument(
        "--output",
        help="Optional JSON output path under the current workspace.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(os.getenv("DOVESCORE_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))),
        help="Maximum seconds for non-demo DoveScore evaluation.",
    )
    parser.add_argument(
        "--max-input-chars",
        type=int,
        default=int(os.getenv("DOVESCORE_MAX_INPUT_CHARS", str(DEFAULT_MAX_INPUT_CHARS))),
        help="Maximum combined source and target characters for cost control.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run a deterministic UI demo without DoveScore, API key, or external calls.",
    )
    parser.add_argument(
        "--include-details",
        action="store_true",
        help="Include raw DoveScore events, descriptives, order lists, and per-fact scores.",
    )
    parser.add_argument(
        "--pretty",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Pretty-print JSON output.",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    args = parse_args()
    if args.demo:
        return _emit_json_result(_demo_result(), args.output, args.pretty)

    try:
        source = _flatten_text(_read_text(args.source, args.source_file, "source"))
        target = _flatten_text(_read_text(args.target, args.target_file, "target"))
        if args.timeout_seconds <= 0:
            raise ValueError("--timeout-seconds must be greater than 0.")
        if args.max_input_chars <= 0:
            raise ValueError("--max-input-chars must be greater than 0.")
        _check_input_size(source, target, args.max_input_chars)
    except (OSError, ValueError) as exc:
        logger.error("%s", exc)
        return 2

    try:
        from openai import OpenAI
        from DoveScore import DoveScoreEvaluator
        from DoveScore.default_decomposer import DefaultDecomposer
        from DoveScore.default_factchecker import DefaultFactChecker
        from DoveScore.default_sorter import DefaultSorter
    except ImportError as exc:
        logger.error(
            "DoveScore is not installed. Install it with "
            "`pip install openai git+https://github.com/dannalily/DoveScore.git` "
            "or install a local DoveScore checkout with "
            "`pip install -e /path/to/DoveScore`."
        )
        logger.error("Import error: %s", exc)
        return 2

    if not args.api_key:
        logger.error(
            "Missing API key. Set DOVESCORE_API_KEY, OPENAI_API_KEY, API_KEY, "
            "MODEL_API_KEY, or pass --api-key."
        )
        return 2

    client_args: dict[str, Any] = {"api_key": args.api_key, "timeout": args.timeout_seconds}
    if args.base_url:
        client_args["base_url"] = args.base_url
    client = OpenAI(**client_args)
    evaluator_args = SimpleNamespace(
        api_key=args.api_key,
        backbone=args.backbone,
        decomposer=DefaultDecomposer(args.backbone, client),
        factchecker=DefaultFactChecker(args.backbone, client),
        sorter=DefaultSorter(args.backbone, client),
    )
    evaluator = DoveScoreEvaluator(evaluator_args)
    try:
        with _evaluation_deadline(args.timeout_seconds):
            result = _summarize_result(
                _json_ready(evaluator.evaluate(source, target)),
                args.include_details,
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("DoveScore evaluation failed: %s", exc)
        return 2

    return _emit_json_result(result, args.output, args.pretty)


if __name__ == "__main__":
    raise SystemExit(main())
