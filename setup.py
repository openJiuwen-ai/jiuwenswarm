"""Keep the main wheel portable while including optional native payloads."""

import os
from pathlib import Path
import runpy
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


ROOT = Path(__file__).resolve().parent
# PEP 517 does not put the project on sys.path. Read only the standalone
# artifact contract; building must not import installed JiuwenSwarm modules.
ARTIFACTS = runpy.run_path(str(ROOT / "jiuwenswarm/cua_engine/artifacts.py"))


class BuildWithNative(build_py):
    def run(self):
        super().run()
        if getattr(self, "editable_mode", False):
            return
        output = Path(self.build_lib) / "jiuwenswarm/cua_engine/runtimes"
        if output.exists():
            shutil.rmtree(output)
        configured = os.environ.get("JIUWENSWARM_CUA_ARTIFACTS")
        # Local source installs may omit optional desktop capabilities. Official
        # release entry points explicitly opt in and require both payloads.
        if not configured:
            return
        artifacts = Path(configured).resolve()
        ARTIFACTS["check_artifacts"](artifacts, ROOT / "jiuwenswarm/cua_engine")
        output.mkdir(parents=True)
        for target in ARTIFACTS["TARGETS"]:
            for suffix in (".json", ".zip"):
                shutil.copy2(artifacts / (target + suffix), output)


setup(cmdclass={"build_py": BuildWithNative})
