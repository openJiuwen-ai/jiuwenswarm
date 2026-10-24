"""Exercise this checkout and scope subprocess imports to this test suite."""
import os
from pathlib import Path
import sys

import pytest

ENGINE = Path(__file__).resolve().parents[4] / "jiuwenswarm/cua_engine"
SDK_SOURCE = str(ENGINE / "src")
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "jiuwenswarm/cua_engine/src"))


@pytest.fixture(autouse=True)
def sdk_subprocess_path(monkeypatch):
    paths = (SDK_SOURCE, os.environ.get("PYTHONPATH", ""))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(part for part in paths if part))
