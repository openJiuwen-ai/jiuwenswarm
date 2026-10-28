"""Error contracts remain lightweight; existing public RSI objects retain identity."""
import importlib
import os
from pathlib import Path
import subprocess
import sys


def test_importing_rsi_errors_does_not_load_optional_execution_services():
    code = """
import sys
from jiuwenswarm.agents.harness.common.rsi.errors import RsiHarnessInstallConflict
prefix = 'jiuwenswarm.agents.harness.common.rsi.'
for module in ('context', 'provider_factory', 'paper_provider', 'mock_harness_provider', 'materializer'):
    assert prefix + module not in sys.modules, module
assert not any(name.startswith('openjiuwen') for name in sys.modules)
"""
    root = Path(__file__).resolve().parents[3]
    subprocess.run([sys.executable, "-c", code], cwd=root,
                   env={**os.environ, "PYTHONPATH": str(root)}, check=True,
                   capture_output=True, text=True, timeout=30)


def test_public_rsi_exports_preserve_original_symbols_and_discoverability():
    package = importlib.import_module("jiuwenswarm.agents.harness.common.rsi")
    assert set(package.__all__) <= set(dir(package))
    for name in package.__all__:
        module, attribute = package._EXPORTS[name]
        assert getattr(package, name) is getattr(importlib.import_module(module), attribute)
