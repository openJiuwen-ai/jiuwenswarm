# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Saved Design asset references must survive common path initialization (#6375)."""

import json
import os
import subprocess
import sys
from pathlib import Path


def test_startup_keeps_saved_design_asset_paths_readable(tmp_path: Path) -> None:
    root = tmp_path / "data"
    # Deliberately frozen legacy references: deriving these from today's path
    # helpers would let a migration move both the producer and the expectation.
    assets = root / "agent" / "designer" / "outputs" / "design_saved"
    assets.mkdir(parents=True)
    references = []
    for name in ("brief.md", "frame.png", "clip.mp4"):
        asset = assets / name
        asset.write_bytes(f"saved {name}".encode())
        references.extend((str(asset), asset.relative_to(root).as_posix()))
    record = root / "agent" / "designer" / "saved_references.json"
    original = json.dumps({"uris": references}).encode()
    record.write_bytes(original)

    # A cold import is essential: the original migration ran at import time,
    # before startup, and moved files while persisted URIs still used old paths.
    code = """
import json
from pathlib import Path
from jiuwenswarm.common.utils import get_user_workspace_dir, get_agent_root_dir
root = get_user_workspace_dir()
# After the profile layout migration, get_agent_root_dir() is
# {DATA_DIR}/profile/agent and {DATA_DIR}/agent is left as a
# compatibility symlink. Path equality would fail; resolve() still
# identifies the same directory so persisted legacy URIs stay valid.
assert get_agent_root_dir().resolve() == (root / 'agent').resolve()
record = root / 'agent' / 'designer' / 'saved_references.json'
for uri in json.loads(record.read_text())['uris']:
    target = Path(uri)
    if not target.is_absolute():
        target = root / target
    assert target.read_bytes() == ('saved ' + target.name).encode(), uri
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[3],
        env={**os.environ, "JIUWENSWARM_DATA_DIR": str(root), "JIUWENSWARM_EDITION": "personal"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert record.read_bytes() == original
