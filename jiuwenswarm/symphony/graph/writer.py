"""Write graph build artifacts to disk."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Union

from jiuwenswarm.symphony.graph.models import GraphBuildResult


def write_json_file(path: Path, data: Dict[str, Any]) -> None:
    """Write JSON with stable formatting."""

    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_graph_build_result(
    result: GraphBuildResult,
    out_dir: Union[Path, str],
) -> None:
    """Write all graph build artifacts to an output directory."""

    output_path = Path(out_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    write_json_file(output_path / "score_manifest.json", result.manifest.to_dict())
    write_json_file(
        output_path / "skills.json",
        {"skills": [skill.to_dict() for skill in result.skills]},
    )
    write_json_file(output_path / "skill_graph.json", result.graph.to_dict())
    write_json_file(output_path / "score_lookup.json", result.lookup.to_dict())
    write_json_file(
        output_path / "llm_matches.json",
        {
            "candidates": [candidate.to_dict() for candidate in result.candidates],
            "matches": [match.to_dict() for match in result.llm_matches],
        },
    )
    write_json_file(
        output_path / "diagnostics.json",
        {"diagnostics": [diagnostic.to_dict() for diagnostic in result.diagnostics]},
    )
