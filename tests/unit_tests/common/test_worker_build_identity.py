# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Build instance identity and installation ownership are different contracts."""
import json
import sys

import pytest

from jiuwenswarm.common.worker_build_identity import read_build_identity, write_build_identity
from jiuwenswarm.server.supervisor.identity import deployment_scope


def test_rebuild_with_identical_inputs_still_has_a_distinct_backend_id(tmp_path):
    (tmp_path / "jiuwenswarm").mkdir()
    (tmp_path / "jiuwenswarm" / "backend.py").write_text("value = 1\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='probe'\n")
    first = write_build_identity(tmp_path / "first.json", tmp_path)
    second = write_build_identity(tmp_path / "second.json", tmp_path, identity_input=tmp_path / "first.json")
    assert first["backend_build_id"] != second["backend_build_id"]
    assert first["source_digest"] == second["source_digest"]
    assert first["dependency_digest"] == second["dependency_digest"]
    assert second["input_build_id"] == first["backend_build_id"]
    (tmp_path / "jiuwenswarm" / "backend.py").write_text("value = 2\n")
    with pytest.raises(RuntimeError, match="does not match"):
        write_build_identity(tmp_path / "third.json", tmp_path, identity_input=tmp_path / "first.json")


def test_invalid_manifest_cannot_silently_reuse_an_old_worker(tmp_path):
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"schema": 2, "backend_build_id": "a" * 64}))
    with pytest.raises(RuntimeError, match="Invalid Worker build identity"):
        read_build_identity(path)
    path.write_text(json.dumps({"schema": 1, "fingerprint": "b" * 64}))
    assert read_build_identity(path)["fingerprint"] == "b" * 64


def test_frozen_installation_is_stable_across_extraction_directories(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "installed" / "backend.exe"))
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "extraction-one"))
    first = deployment_scope()
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "extraction-two"))
    assert deployment_scope() == first
    monkeypatch.setattr(sys, "executable", str(tmp_path / "other" / "backend.exe"))
    assert deployment_scope() != first


def test_controlled_wheel_uses_build_manifest_and_still_observes_installed_dependencies(monkeypatch, tmp_path):
    from jiuwenswarm.server.supervisor import dependency_identity, identity, identity_cache

    manifest = tmp_path / "build.json"
    value = {"schema": 2, "backend_build_id": "a" * 64, "source_digest": "b" * 64,
             "dependency_digest": "c" * 64, "packaging_digest": "d" * 64}
    manifest.write_text(json.dumps(value))
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(identity, "_identity_path", lambda: manifest)
    monkeypatch.setattr(identity_cache, "source_fingerprint",
        lambda *args: pytest.fail("A controlled wheel must not recursively rescan package data at startup"))
    dependency = ["installed-sdk-one"]
    monkeypatch.setattr(dependency_identity, "environment_identity", lambda: dependency[0])
    first = identity.code_identity()
    dependency[0] = "installed-sdk-two"
    second = identity.code_identity()
    assert second != first
    value["backend_build_id"] = "e" * 64
    manifest.write_text(json.dumps(value))
    assert identity.code_identity() != second
