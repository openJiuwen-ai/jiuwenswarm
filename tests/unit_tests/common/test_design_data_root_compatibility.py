# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Saved Design asset references must survive common path initialization (#6375)."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _can_create_symlinks(base: Path) -> bool:
    """Probe whether this platform/session may create symlinks.

    Windows without developer mode denies SeCreateSymbolicLinkPrivilege, and
    the profile migration skips the legacy compat links by design on such
    hosts. The assertions below must degrade accordingly instead of failing.
    """
    target = base / "_compat_probe_target"
    target.mkdir()
    link = base / "_compat_probe_link"
    try:
        link.symlink_to("_compat_probe_target", target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    finally:
        # Cleanup must never raise: the probe's contract is to return a
        # boolean, and unlinking a directory symlink can fail on Windows
        # (rmdir is the fallback for that link kind).
        try:
            link.unlink(missing_ok=True)
        except OSError:
            try:
                link.rmdir()
            except OSError:
                pass
        shutil.rmtree(target, ignore_errors=True)
    return True


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
    compat_link = _can_create_symlinks(tmp_path)

    # A cold import is essential: the original migration ran at import time,
    # before startup, and moved files while persisted URIs still used old paths.
    code = """
import json
import os
from pathlib import Path
from jiuwenswarm.common.utils import get_user_workspace_dir, get_agent_root_dir
root = get_user_workspace_dir()
# After the profile layout migration, get_agent_root_dir() is
# {DATA_DIR}/profile/agent and {DATA_DIR}/agent is left as a
# compatibility symlink. Path equality would fail; resolve() still
# identifies the same directory so persisted legacy URIs stay valid.
# Platforms that deny symlinks (e.g. Windows without developer mode) skip
# the compat link by design; the guarantee there degrades to "migrated
# data stays intact under profile/agent", asserted via the new layout.
if os.environ.get('EXPECT_COMPAT_LINK') != '0':
    assert get_agent_root_dir().resolve() == (root / 'agent').resolve()
    record = root / 'agent' / 'designer' / 'saved_references.json'
    for uri in json.loads(record.read_text())['uris']:
        target = Path(uri)
        if not target.is_absolute():
            target = root / target
        assert target.read_bytes() == ('saved ' + target.name).encode(), uri
else:
    base = get_agent_root_dir()
    record = base / 'designer' / 'saved_references.json'
    for uri in json.loads(record.read_text())['uris']:
        target = base / 'designer' / 'outputs' / 'design_saved' / Path(uri).name
        assert target.read_bytes() == ('saved ' + target.name).encode(), uri
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[3],
        env={
            **os.environ,
            "JIUWENSWARM_DATA_DIR": str(root),
            "JIUWENSWARM_EDITION": "personal",
            "EXPECT_COMPAT_LINK": "1" if compat_link else "0",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    if not compat_link:
        # Probe-mismatch guard: when the probe reports symlinks unavailable,
        # the migration must not have left a legacy link behind — otherwise
        # the strong assertions above were skipped for a wrong reason.
        assert not (root / "agent").exists(), (
            "probe mismatch: legacy compat link exists but the probe "
            "reported symlinks unavailable"
        )
    # Without the compat link the record itself only exists at the migrated
    # location; with it, the legacy path must read the same bytes.
    final_record = (
        record
        if compat_link
        else root / "profile" / "agent" / "designer" / "saved_references.json"
    )
    assert final_record.read_bytes() == original
