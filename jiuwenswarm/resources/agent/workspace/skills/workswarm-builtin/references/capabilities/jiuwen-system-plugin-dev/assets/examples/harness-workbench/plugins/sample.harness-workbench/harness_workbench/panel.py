# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Write the Claude Code sandbox panel into a managed WebUI resource directory."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .activity import WorkbenchSnapshot

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_ASSETS = _PACKAGE_ROOT / "assets"
_PANEL_PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
  <meta charset="utf-8">
  <title>Claude Code</title>
  <link rel="stylesheet" href="claude-panel.css">
</head>
<body>
  <div class="shell">
    <header class="bar">
      <strong>Claude Code</strong>
      <span>sandbox</span>
    </header>
    <main class="body" id="root"></main>
  </div>
  <script type="application/json" id="payload">{payload}</script>
  <script src="claude-panel.js"></script>
  <script>
    parent.postMessage({{ "type": "webui.surface", "version": 1, "event": "ready" }}, "*");
  </script>
</body>
</html>
"""


def write_claude_panel(directory: Path, snapshot: WorkbenchSnapshot) -> None:
    """Materialize zh panel assets next to the generated payload page."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("claude-panel.css", "claude-panel.js"):
        shutil.copyfile(_ASSETS / name, directory / name)
    payload = json.dumps(snapshot.document(), ensure_ascii=False, separators=(",", ":"))
    safe = payload.replace("</", "<\\/")
    page = _PANEL_PAGE.format(payload=safe)
    (directory / "zh.html").write_text(page, encoding="utf-8")
    (directory / "en.html").write_text(page, encoding="utf-8")
