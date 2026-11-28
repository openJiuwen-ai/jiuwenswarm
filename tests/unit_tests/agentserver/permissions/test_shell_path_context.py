"""Compatibility checks for the optional Core shell path interface."""

import builtins
import runpy
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.permissions import shell_path_context as bridge

_SDK_MODULE = "openjiuwen.harness.security.permission_engine.shell_path_context"


@pytest.mark.parametrize("missing_module", [_SDK_MODULE, "missing_internal_dependency"])
def test_old_core_fallback_requires_exact_module_absence(monkeypatch, missing_module):
    real_import = builtins.__import__

    def import_module(name, *args, **kwargs):
        if name == _SDK_MODULE:
            raise ModuleNotFoundError(name=missing_module)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_module)
    if missing_module != _SDK_MODULE:
        with pytest.raises(ModuleNotFoundError) as error:
            runpy.run_path(bridge.__file__)
        assert error.value.name == missing_module
    else:
        adapter = runpy.run_path(bridge.__file__)
        with adapter["shell_path_call"]() as scope:
            assert scope is None
            assert adapter["current_shell_paths"]("bash", {}) is None


def test_new_core_requires_both_callable_entry_points(monkeypatch):
    real_import = builtins.__import__

    def import_module(name, *args, **kwargs):
        if name == _SDK_MODULE:
            return SimpleNamespace(current_shell_path_analysis=None, shell_path_call=nullcontext)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_module)
    with pytest.raises(ImportError, match="two callable entry points"):
        runpy.run_path(bridge.__file__)


def test_present_core_cannot_yield_an_empty_scope(monkeypatch):
    monkeypatch.setattr(bridge, "_core_shell_path_call", nullcontext)
    with pytest.raises(RuntimeError, match="does not provide bind"):
        with bridge.shell_path_call():
            pytest.fail("broken scope was accepted")
