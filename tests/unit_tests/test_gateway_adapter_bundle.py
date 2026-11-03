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
def test_verifier_resolves_lazy_exports_before_backend_import(monkeypatch, missing):
    monkeypatch.setattr(verifier.sys, "frozen", True, raising=False)
    exports = SimpleNamespace(__all__=["Adapter"], Adapter=object())
    if missing:
        del exports.Adapter
    monkeypatch.setattr(verifier, "gateway_adapter", exports)
    imports = []
    monkeypatch.setattr(verifier, "import_module", imports.append)
    if missing:
        with pytest.raises(AttributeError):
            verifier.verify_gateway_adapter_bundle()
        assert not imports
    else:
        verifier.verify_gateway_adapter_bundle()
        assert imports == ["jiuwenswarm.server.agent_ws_server"]


def test_spec_collects_lazy_gateway_adapters():
    source = (Path(__file__).resolve().parents[2] / "scripts/jiuwenswarm.spec").read_text(encoding="utf-8")
    compact = "".join(source.split())
    assert 'collect_submodules("jiuwenswarm.server.runtime.gateway_adapter")' in compact
    assert compact.count("gateway_adapter_submodules=") == 1
    assert "+gateway_adapter_submodules" in compact
