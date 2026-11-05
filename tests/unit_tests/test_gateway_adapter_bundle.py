"""Regression checks for the frozen lazy-adapter startup gate."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import verify_gateway_adapter_bundle as verifier


def test_verifier_requires_a_frozen_process(monkeypatch):
    monkeypatch.setattr(verifier.sys, "frozen", False, raising=False)
    with pytest.raises(RuntimeError, match="frozen executable"):
        verifier.verify_gateway_adapter_bundle()


@pytest.mark.parametrize("missing", [False, True])
def test_verifier_resolves_lazy_exports_before_backend_import(monkeypatch, missing, tmp_path):
    monkeypatch.setattr(verifier.sys, "frozen", True, raising=False)
    exports = SimpleNamespace(__all__=["Adapter"], Adapter=object())
    if missing:
        del exports.Adapter
    monkeypatch.setattr(verifier, "gateway_adapter", exports)
    monkeypatch.setattr(verifier, "files", lambda _: tmp_path)
    for extension in ("adx", "agent_client", "agentos"):
        (tmp_path / extension).mkdir()
        for name in ("extension.yaml", "extension.py"):
            (tmp_path / extension / name).touch()
    imports = []
    monkeypatch.setattr(verifier, "import_module", imports.append)
    if missing:
        with pytest.raises(AttributeError):
            verifier.verify_gateway_adapter_bundle()
        assert not imports
    else:
        verifier.verify_gateway_adapter_bundle()
        assert imports[0] == "jiuwenswarm.server.agent_ws_server"
        assert "agent_gateway.channel_manager.im_platforms.welink.welink_runtime" in imports
        assert "openjiuwen.harness.persistent_agent" in imports
        (tmp_path / "adx" / "extension.py").unlink()
        with pytest.raises(RuntimeError, match="adx/extension.py"):
            verifier.verify_gateway_adapter_bundle()


def test_spec_collects_lazy_gateway_adapters():
    source = (Path(__file__).resolve().parents[2] / "scripts/jiuwenswarm.spec").read_text(encoding="utf-8")
    compact = "".join(source.split())
    assert 'collect_submodules("jiuwenswarm.server.runtime.gateway_adapter")' in compact
    assert compact.count("gateway_adapter_submodules=") == 1
    assert "+gateway_adapter_submodules" in compact
