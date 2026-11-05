# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Historical POSIX fallback locations remain discoverable without filesystem writes."""
import os
import tempfile
from types import SimpleNamespace

import pytest

from jiuwenswarm.common import utils
from jiuwenswarm.server.supervisor import identity, worker_process


@pytest.mark.parametrize("instance", ["", "pod-one"])
def test_legacy_posix_paths_include_historical_temp_fallback(monkeypatch, tmp_path, instance):
    monkeypatch.delenv("JIUWENSWARM_RUNTIME_SOCKET", raising=False)
    monkeypatch.delenv("HOSTNAME", raising=False)
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_INSTANCE", instance)
    workspace = tmp_path / ("deep-" * 25)
    temp = tmp_path / "os-temp"
    monkeypatch.setattr(utils, "_workspace_base_dir", workspace)
    monkeypatch.setattr(identity, "data_scope", lambda: "a" * 64)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))
    monkeypatch.setattr(worker_process, "os", SimpleNamespace(name="posix", environ=os.environ, getuid=lambda: 42))
    paths = worker_process.legacy_socket_paths()
    prefix = "runtime-" + "a" * 16
    assert str(temp / "jiuwenswarm-runtime-42" / (prefix + ".sock")) in paths
    token = worker_process._runtime_instance_token()
    if token:
        assert str(temp / "jiuwenswarm-runtime-42" / (prefix + "-" + token + ".sock")) in paths
    assert str(workspace / "run/runtime-worker.sock") in paths
    assert len(paths) == len(set(paths))
    assert not temp.exists(), "Endpoint discovery must not create a directory"


def test_explicit_socket_override_excludes_all_historical_locations(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_SOCKET", "owned-custom-endpoint")
    assert worker_process.legacy_socket_paths() == []
