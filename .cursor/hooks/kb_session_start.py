#!/usr/bin/env python3
"""sessionStart: bootstrap docs/ai if needed; inject KB pointer."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path


def _read_text(path: Path, limit: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if len(text) > limit:
        return text[:limit] + "\n…(truncated)…"
    return text


def _bootstrap(ai: Path, templates: Path) -> None:
    """Create minimal docs/ai scaffold from tracked templates (local-only tree)."""
    if not templates.is_dir():
        return
    try:
        ai.mkdir(parents=True, exist_ok=True)
        (ai / "META").mkdir(exist_ok=True)
        (ai / "_templates").mkdir(exist_ok=True)
        (ai / "_sessions").mkdir(exist_ok=True)
        (ai / "experiments").mkdir(exist_ok=True)
        for name in ("00-pre-dev", "01-dev", "02-post-dev"):
            (ai / name).mkdir(exist_ok=True)
    except OSError:
        return

    copies = {
        "GUIDE.md": ai / "GUIDE.md",
        "preferences.md": ai / "META" / "preferences.md",
        "experiments-README.md": ai / "experiments" / "README.md",
        "SUMMARY.md": ai / "_templates" / "SUMMARY.md",
        "EVOLUTION.md": ai / "_templates" / "EVOLUTION.md",
        "QA.md": ai / "_templates" / "QA.md",
        "evaluate_output.md": ai / "_templates" / "evaluate_output.md",
    }
    for src_name, dest in copies.items():
        src = templates / src_name
        if src.is_file() and not dest.exists():
            try:
                shutil.copy2(src, dest)
            except OSError:
                pass

    handoff = ai / "HANDOFF.md"
    if not handoff.exists():
        try:
            handoff.write_text(
                "# HANDOFF\n\n"
                "New workspace: fill this file after the first meaningful session.\n"
                "Read META/preferences.md and META/phases.md.\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    phases = ai / "META" / "phases.md"
    if not phases.exists():
        try:
            phases.write_text(
                "# 阶段指针\n\n"
                "| 字段 | 值 |\n|------|-----|\n"
                "| **phase** | `00-pre-dev` |\n"
                "| **active_topic** | _(set me)_ |\n"
                "| **updated** | _(set me)_ |\n",
                encoding="utf-8",
            )
        except OSError:
            pass


def main() -> None:
    try:
        json.load(sys.stdin)
    except Exception:
        pass

    root = Path.cwd()
    ai = root / "docs" / "ai"
    templates = root / ".cursor" / "kb-templates"
    _bootstrap(ai, templates)

    phases = _read_text(ai / "META" / "phases.md", 2000)
    handoff = _read_text(ai / "HANDOFF.md", 3500)

    context = f"""# Local knowledge base (docs/ai) — auto injected

You MUST follow `.cursor/rules` about `docs/ai/`.
Cold-start: read HANDOFF + META/phases + META/preferences + active topic QA.
Flush decisions/QA into the current phase dir without waiting to be asked.

## META/phases.md
{phases or "(missing)"}

## HANDOFF.md (excerpt)
{handoff or "(missing)"}
"""

    print(
        json.dumps(
            {
                "additional_context": context,
                "env": {"JIUWENSWARM_KB_ROOT": str(ai)},
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

