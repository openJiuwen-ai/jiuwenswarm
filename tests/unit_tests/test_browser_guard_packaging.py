"""The Playwright init-page network guard is a runtime asset.

``jiuwenswarm/server/runtime/agent_adapter/browser_runtime_security.py`` resolves
``GUARD_INIT_PAGE_PATH`` next to itself (``__file__``) and injects it as
``--init-page``. If the script is not declared as package data it is absent from
a wheel, ``_guard_digest()`` returns ``""`` and the guard can never be enforced
(``failure_reason="guard_init_page_missing"``).
"""

import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GUARD_JS = "server/runtime/agent_adapter/browser_runtime_network_guard.js"


def test_browser_guard_script_is_declared_as_package_data():
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    patterns = pyproject["tool"]["setuptools"]["package-data"]["jiuwenswarm"]
    assert "server/runtime/agent_adapter/*.js" in patterns


def test_browser_guard_script_is_present():
    assert (PROJECT_ROOT / "jiuwenswarm" / GUARD_JS).is_file()
