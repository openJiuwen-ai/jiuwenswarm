# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Guard unit tests against importing JiuwenSwarm from another checkout."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import jiuwenswarm


def test_unit_tests_import_jiuwenswarm_from_current_checkout() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    package_file = Path(jiuwenswarm.__file__).resolve()

    assert package_file.is_relative_to(repo_root / "jiuwenswarm"), (
        f"unit tests imported jiuwenswarm from another checkout: {package_file}"
    )


def test_conftest_reloads_when_a_submodule_comes_from_another_checkout(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    stale_file = tmp_path / "other-checkout" / "jiuwenswarm" / "stale_probe.py"
    script = textwrap.dedent(
        f"""
        import runpy
        import sys
        from pathlib import Path
        from types import ModuleType

        repo_root = Path({str(repo_root)!r})
        sys.path.insert(0, str(repo_root))
        import jiuwenswarm

        stale = ModuleType("jiuwenswarm.stale_checkout_probe")
        stale.__file__ = {str(stale_file)!r}
        sys.modules[stale.__name__] = stale

        runpy.run_path(
            str(repo_root / "tests" / "conftest.py"),
            run_name="checkout_conftest_probe",
        )

        assert stale.__name__ not in sys.modules, sys.modules[stale.__name__].__file__
        package_file = Path(sys.modules["jiuwenswarm"].__file__).resolve()
        assert package_file.is_relative_to(repo_root / "jiuwenswarm"), package_file
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
