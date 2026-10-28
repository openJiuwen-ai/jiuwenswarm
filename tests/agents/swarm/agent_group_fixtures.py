# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Minimal AgentGroup package builder shared by swarm tests.

The loader itself and its tests live in openjiuwen
(``openjiuwen.agent_teams.experts``); swarm tests only need a valid package.
"""

from __future__ import annotations

import json
from pathlib import Path


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _minimal_group(tmp_path: Path) -> Path:
    group = tmp_path / "group"
    _write_json(
        group / "manifest.json",
        {
            "name": "group",
            "package_type": "agent_group",
            "agents": ["leader", "member1"],
        },
    )
    for name in ("leader", "member1"):
        _write_json(
            group / "agents" / name / "manifest.json",
            {
                "package_type": "agent_template",
                "name": f"{name} display name",
                "description": f"{name} description",
                "persona": {"dir": "." if name == "leader" else "./persona"},
            },
        )
        persona = group / "agents" / name / "persona" / f"{name}.md"
        persona.parent.mkdir(parents=True, exist_ok=True)
        persona.write_text(f"# {name}\n", encoding="utf-8")
    (group / "agents" / "leader" / "AGENT.md").write_text(
        "# Leader rules\n",
        encoding="utf-8",
    )
    return group
