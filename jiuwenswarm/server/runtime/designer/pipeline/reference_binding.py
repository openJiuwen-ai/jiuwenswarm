# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Reference-image binding for Designer video calls.

Reference-to-video backends bind each attached image by its position in the
prompt ("Image 1", "@Image 1"). The position is only known once the files are
collected for the call, so the binding is rendered here from the attached
slots and appended to the final prompt. Agents and the toolbar write the
narrative only; they never author image numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from jiuwenswarm.server.runtime.designer.pipeline.content_language import (
    current_content_language,
    phrase,
    strip_phrase,
)

ReferenceRole = Literal["character", "scene", "upload", "other"]

REFERENCE_BINDING_KEY = "reference_binding"

_PHRASES: dict[str, dict[str, str]] = {
    "en": {
        "character": "{word} is the identity reference for {label}.",
        "scene": "{word} is the scene: {label}.",
        "scene_unlabeled": "{word} is the scene environment.",
        "upload": "{word} is the uploaded reference image {label}.",
        "other": "{word} is the connected reference image {label}.",
    },
    "zh": {
        "character": "{word} 是{label}的形象参考。",
        "scene": "{word} 是场景：{label}。",
        "scene_unlabeled": "{word} 是场景环境。",
        "upload": "{word} 是上传的参考图「{label}」。",
        "other": "{word} 是相连的参考图「{label}」。",
    },
}


@dataclass(frozen=True)
class ReferenceSlot:
    """One image attached to a video call, at its 1-based attach position."""

    index: int
    role: ReferenceRole
    label: str
    path: Path
    node_id: str | None = None


class ClipReferenceError(RuntimeError):
    """The clip declares a reference whose image does not exist.

    This is an internal inconsistency (for example a scene node that never
    produced its plate), so the video call stops instead of guessing.
    """


def image_word(index: int, family: str) -> str:
    """Positional reference token as the backend family expects it."""
    if family == "seedance":
        return f"@Image {index}"
    return f"Image {index}"


def reindex_slots(slots: list[ReferenceSlot], paths: list[str] | list[Path]) -> list[ReferenceSlot]:
    """Slots for the files actually sent, in send order.

    A file that was not collected as a slot (for example one restored from a
    regenerate packet) is bound as a generic connected reference.
    """
    by_path = {str(slot.path.resolve()): slot for slot in slots}
    out: list[ReferenceSlot] = []
    for index, raw in enumerate(paths, start=1):
        path = Path(raw).resolve()
        known = by_path.get(str(path))
        if known is not None:
            out.append(replace(known, index=index, path=path))
        else:
            out.append(ReferenceSlot(index=index, role="other", label=path.stem, path=path))
    return out


def scene_slot(slots: list[ReferenceSlot] | None) -> ReferenceSlot | None:
    return next((slot for slot in slots or [] if slot.role == "scene"), None)


def render_reference_binding(
    slots: list[ReferenceSlot] | None,
    *,
    family: str,
    lang: str | None = None,
) -> str:
    """One natural sentence per attached image, in attach order."""
    lang = lang or current_content_language()
    lines: list[str] = []
    for slot in slots or []:
        word = image_word(slot.index, family)
        label = strip_phrase(slot.label)
        if slot.role == "scene" and not label:
            lines.append(phrase(_PHRASES, lang, "scene_unlabeled", word=word))
            continue
        lines.append(phrase(_PHRASES, lang, slot.role, word=word, label=label or slot.path.stem))
    return "\n".join(lines)


def strip_reference_binding(prompt: str, cfg: dict[str, Any]) -> str:
    """``prompt`` without the binding this node's previous call appended."""
    text = str(prompt or "").strip()
    previous = str(cfg.get(REFERENCE_BINDING_KEY) or "").strip()
    if previous and previous in text:
        text = text.replace(previous, "").strip()
    return text


def append_reference_binding(
    prompt: str,
    slots: list[ReferenceSlot] | None,
    *,
    cfg: dict[str, Any],
    family: str,
    lang: str | None = None,
) -> str:
    """Final prompt with the binding as its last paragraph.

    The binding from a previous call is removed first, so a restored or
    re-sent prompt never carries two bindings. The rendered binding is stored
    on ``cfg`` for that purpose.
    """
    text = strip_reference_binding(prompt, cfg)
    binding = render_reference_binding(slots, family=family, lang=lang)
    cfg[REFERENCE_BINDING_KEY] = binding
    if not binding:
        return text
    return f"{text}\n\n{binding}".strip()
