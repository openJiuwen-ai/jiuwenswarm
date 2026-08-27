#!/usr/bin/env python3
"""sessionEnd: append a lightweight session log under docs/ai/_sessions/."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}

    root = Path.cwd()
    out_dir = root / "docs" / "ai" / "_sessions"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        print("{}")
        return

    sid = str(data.get("session_id") or "unknown")[:12]
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{ts}_{sid}.json"
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass

    print("{}")


if __name__ == "__main__":
    main()

