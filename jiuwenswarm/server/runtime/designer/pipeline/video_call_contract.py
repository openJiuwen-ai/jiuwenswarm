# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Deterministic preparation of authored video prompts, without semantic review."""

from __future__ import annotations

from typing import Any

from jiuwenswarm.server.runtime.designer.pipeline.content_language import current_content_language


def prepare_video_prompt(
    prompt: str,
    *,
    cfg: dict[str, Any] | None = None,
    graph: dict[str, Any] | None = None,
) -> str:
    """Preserve authored prose and append explicitly assigned dialogue if absent.

    This checks the prompt's type and presence, not its narrative, language,
    wardrobe, staging, or similarity to storyboard prose. Dialogue uses exact
    containment only; it never causes replacement of the authored prompt.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Video prompt must be a non-empty string")
    text = prompt.strip()
    metadata = (graph or {}).get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    analysis = metadata.get("script_analysis")
    analysis = analysis if isinstance(analysis, dict) else {}
    names = {
        str(c.get("id")): str(c.get("name") or c.get("id"))
        for c in analysis.get("characters") or []
        if isinstance(c, dict) and c.get("id")
    }
    by_character = cfg.get("speech_by_character")
    if by_character is not None and not isinstance(by_character, dict):
        raise ValueError("speech_by_character must be an object")
    dialogue = list((by_character or {}).items())
    if not dialogue:
        line = cfg.get("speech_line")
        if line is None:
            line = ""
        if not isinstance(line, str):
            raise ValueError("Video dialogue must be a string")
        # speech_line_from_by_character serializes entries as "id: line | id: line".
        parts = [part.partition(": ") for part in line.split(" | ")]
        if parts and all(separator and speaker in names for speaker, separator, _ in parts):
            dialogue = [(speaker, words) for speaker, _, words in parts]
        else:
            dialogue = [("", line)]
    for speaker, line in dialogue:
        if not isinstance(line, str):
            raise ValueError("Video dialogue must be a string")
        line = line.strip()
        if not line or line in text:
            continue
        who = names.get(str(speaker), str(speaker))
        if current_content_language() == "zh":
            label = f"{who}：" if who else ""
            text += f'\n\n对白：{label}“{line}”'
        else:
            label = f"{who}: " if who else ""
            text += f'\n\nDialogue: {label}"{line}"'
    return text
