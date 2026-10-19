"""Fresh processes detect accidental optional imports hidden by pytest caches."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "statement, blocked",
    [
        (
            "from jiuwenswarm.common.auth import login_credentials",
            ["jiuwenswarm.common.auth.account_kit"],
        ),
        (
            "from jiuwenswarm.agents.harness.common.rails.permissions import tool_permission_context",
            [
                "jiuwenswarm.agents.harness.common.rails.symphony",
                "jiuwenswarm.agents.harness.team.rails.team_member_skill_toolkit_rail",
            ],
        ),
        (
            "from jiuwenswarm.agents.harness.common.tools.image_tools import generate_image; assert generate_image.card.name == 'generate_image'",
            ["fastmcp", "google.genai"],
        ),
    ],
)
def test_unrelated_optional_imports_are_not_required(statement, blocked, tmp_path):
    root = Path(__file__).resolve().parents[3]
    script = f"""
import importlib.abc, sys
sys.path.insert(0, {str(root)!r})
blocked = {blocked!r}
class RejectOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            raise AssertionError('optional module required: ' + fullname)
sys.meta_path.insert(0, RejectOptional())
{statement}
assert not any(name in sys.modules for name in blocked)
print('EXECUTION_IMPORT_BOUNDARY_OK')
"""
    env = dict(
        os.environ,
        JIUWENSWARM_DATA_DIR=str(tmp_path / "data"),
        JIUWENSWARM_HOME=str(tmp_path / "home"),
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "EXECUTION_IMPORT_BOUNDARY_OK" in result.stdout
