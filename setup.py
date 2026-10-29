"""Stamp wheel build metadata without modifying the source checkout."""

from pathlib import Path
import os
import runpy

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def run(self) -> None:
        super().run()
        root = Path(__file__).resolve().parent
        helper = runpy.run_path(str(root / "jiuwenswarm" / "common" / "worker_build_identity.py"))
        helper["write_build_metadata"](
            Path(self.build_lib) / "jiuwenswarm" / "resources" / "build-metadata.json",
            self.distribution.get_version(),
        )
        # Load the contract from the selected source, independent of release
        # script location. Write only to build_lib, never the editable checkout.
        supplied = os.environ.get("WORKSWARM_WORKER_IDENTITY")
        helper["write_build_identity"](
            Path(self.build_lib) / "jiuwenswarm" / helper["IDENTITY_FILENAME"], root,
            packaging_root=Path(os.environ.get("WORKSWARM_PACKAGING_ROOT", str(root))),
            identity_input=Path(supplied) if supplied else None,
        )


setup(cmdclass={"build_py": BuildPy})
