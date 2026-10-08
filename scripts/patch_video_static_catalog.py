#!/usr/bin/env python3
"""Patch designer_node_catalog.json video scenario to original static node ids."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "designer_catalog_skills_reports_trajectory"
JSON_PATH = PKG / "designer_node_catalog.json"
TXT_PATH = PKG / "designer_node_catalog.txt"

VIDEO_TEMPLATE = [
    "n_brief",
    "n_character",
    "n_storyboard",
    "n_frame_1",
    "n_frame_2",
    "n_frame_3",
    "n_clip_1",
    "n_clip_2",
    "n_clip_3",
    "n_final",
]

STATIC = [
    ("n_brief", "项目 brief", "text", "Project brief agent", [], ["text"]),
    ("n_character", "角色图", "image", "Character still agent", ["n_brief"], ["image"]),
    ("n_storyboard", "分镜表", "table", "Storyboard table agent", ["n_brief"], ["table"]),
    ("n_frame_1", "视频片段1首帧", "image", "Shot-1 keyframe agent", ["n_character", "n_storyboard"], ["image"]),
    ("n_frame_2", "视频片段2首帧", "image", "Shot-2 keyframe agent", ["n_character", "n_storyboard"], ["image"]),
    ("n_frame_3", "视频片段3首帧", "image", "Shot-3 keyframe agent", ["n_character", "n_storyboard"], ["image"]),
    ("n_clip_1", "视频片段1", "video", "Shot-1 clip agent", ["n_frame_1"], ["video"]),
    ("n_clip_2", "视频片段2", "video", "Shot-2 clip agent", ["n_frame_2"], ["video"]),
    ("n_clip_3", "视频片段3", "video", "Shot-3 clip agent", ["n_frame_3"], ["video"]),
    ("n_final", "最终视频", "video", "Final assembly agent", ["n_clip_1", "n_clip_2", "n_clip_3"], ["video"]),
]


def main() -> None:
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    data.setdefault("scenario_templates", {})["video"] = VIDEO_TEMPLATE
    nodes = [n for n in data.get("nodes", []) if n.get("scenario") != "video"]
    for nid, label, mod, desc, inputs, outputs in STATIC:
        tools = ["call_model", "read_upstream"]
        if mod in {"image", "video", "audio"}:
            tools.append(f"call_{mod}_model")
        nodes.append(
            {
                "id": nid,
                "scenario": "video",
                "label": label,
                "modality": mod,
                "role": "assemble"
                if nid == "n_final"
                else ("intent" if nid == "n_brief" else "asset"),
                "description": desc,
                "typical_inputs": inputs,
                "typical_outputs": outputs,
                "parameters": [
                    {
                        "name": "model",
                        "type": "string",
                        "default": "",
                        "description": "Settings model id chosen by supervisor",
                    },
                ],
                "model_hints": {
                    "quality": ["settings-configured"],
                },
                "tags": ["video", "static-original", "agent"],
                "rerunnable": True,
                "preview": {"kind": mod, "interactive_3d": False},
                "agent": True,
                "tools": tools,
            }
        )
    data["nodes"] = nodes
    data["node_count"] = len(nodes)
    data["scenarios"] = sorted({n["scenario"] for n in nodes})
    data["description"] = (
        "Designer node catalog. Video scenario uses the original static graph "
        "(n_brief…n_final). Each node is an agent with Settings model tools; "
        "supervisor/manager orchestrate cost/quality and feedback."
    )
    JSON_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = [
        "JiuwenSwarm Designer Node Catalog",
        f"nodes: {data['node_count']}",
        "video template (original static): " + " -> ".join(VIDEO_TEMPLATE),
        "",
        "VIDEO (static original / agent-per-node):",
    ]
    for n in nodes:
        if n["scenario"] == "video":
            lines.append(
                f"- [{n['id']}] {n['label']} ({n['modality']}) tools={n.get('tools')}"
            )
    lines.append("")
    lines.append("Other scenarios keep catalog-driven agent graphs.")
    TXT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Updated {JSON_PATH} node_count={data['node_count']}")


if __name__ == "__main__":
    main()
