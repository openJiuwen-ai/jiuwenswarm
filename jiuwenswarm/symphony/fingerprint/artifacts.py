"""Write fingerprint extraction artifacts to disk."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Union

from jiuwenswarm.symphony.fingerprint.models import FingerprintExtractionResult


def write_json_file(path: Path, data: Dict[str, Any]) -> None:
    """Write a JSON file with consistent formatting.

    Args:
        path: Target file path.
        data: Dictionary to serialize as JSON.
    """
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_extraction_result(
    result: FingerprintExtractionResult,
    out_dir: Union[Path, str],
) -> None:
    """Write minimal extraction artifacts to the output directory.

    Creates the output directory if it doesn't exist and writes:
    - fingerprints.json: Normalized Skill fingerprints.
    - diagnostics.json: Extraction diagnostics.
    - normalization_decisions.json: Normalization decision trace.
    - io_name_vocab.json: Final I/O name vocabulary.
    - llm_token_usage.json: LLM request token usage grouped by stage.

    Args:
        result: The extraction result to write.
        out_dir: Output directory path.
    """
    output_path = Path(out_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    write_json_file(
        output_path / "fingerprints.json",
        {
            "fingerprints": [
                fingerprint.to_dict()
                for fingerprint in result.fingerprints
            ]
        },
    )
    write_json_file(
        output_path / "io_name_vocab.json",
        result.io_name_vocab,
    )
    write_json_file(
        output_path / "diagnostics.json",
        {
            "diagnostics": [
                diagnostic.to_dict()
                for diagnostic in result.diagnostics
            ]
        },
    )
    write_json_file(
        output_path / "normalization_decisions.json",
        {
            "normalization_decisions": [
                decision.to_dict()
                for decision in result.normalization_decisions
            ]
        },
    )
    write_json_file(
        output_path / "llm_token_usage.json",
        result.llm_token_usage,
    )
