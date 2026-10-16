# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A cache snapshot must not acknowledge a later process's file revision."""

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("side", ["server", "gateway.embedded"])
@pytest.mark.parametrize("phase", ["read", "write"])
def test_later_process_update_is_visible_after_cache_publication(tmp_path, monkeypatch, side, phase):
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JIUWENSWARM_HOME", str(tmp_path / "home"))
    metadata = importlib.import_module(f"jiuwenswarm.{side}.runtime.session.session_metadata")
    session_id = f"cache-race-{phase}"
    metadata.init_session_metadata(
        session_id=session_id, title="initial", mode="agent.work.normal", work_mode="work",
    )
    metadata.remove_session_metadata_cache(session_id)
    path = metadata.get_agent_sessions_dir() / session_id / "metadata.json"
    remote_title = "updated by another process"

    def remote_update():
        result = subprocess.run(
            [sys.executable, "-c",
             "import json,sys; from pathlib import Path; path=Path(sys.argv[1]); "
             "data=json.loads(path.read_text(encoding='utf-8')); data['title']=sys.argv[2]; "
             "temporary=path.with_suffix('.remote'); "
             "temporary.write_text(json.dumps(data),encoding='utf-8'); temporary.replace(path)",
             str(path), remote_title],
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        assert result.returncode == 0, result.stderr

    if phase == "read":
        read_text = Path.read_text
        injected = False

        def read_before_remote_update(target, *args, **kwargs):
            nonlocal injected
            value = read_text(target, *args, **kwargs)
            if target == path and not injected:
                injected = True
                remote_update()
            return value

        with monkeypatch.context() as read_patch:
            read_patch.setattr(Path, "read_text", read_before_remote_update)
            first = metadata.get_session_metadata(session_id, enable_writeback=False)
        assert injected
        assert first["title"] == "initial"
    else:
        refresh = metadata._refresh_written_cache

        def refresh_after_remote_update(sid, written, options):
            remote_update()
            refresh(sid, written, options)

        monkeypatch.setattr(metadata, "_refresh_written_cache", refresh_after_remote_update)
        metadata.update_session_metadata(session_id=session_id, title="local update", sync_write=True)

    assert json.loads(path.read_text(encoding="utf-8"))["title"] == remote_title
    for _ in range(2):
        assert metadata.get_session_metadata(session_id, enable_writeback=False)["title"] == remote_title
