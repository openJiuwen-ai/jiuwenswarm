"""Stamp wheel build metadata without modifying the source checkout."""

from pathlib import Path
import runpy

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def run(self) -> None:
        super().run()
        helper = runpy.run_path(str(Path(__file__).parent / "scripts" / "build_config.py"))
        helper["write_build_metadata"](
            Path(self.build_lib) / "jiuwenswarm" / "resources" / "build-metadata.json",
            self.distribution.get_version(),
        )
        # Controlled wheels validate backend contents at build time. Runtime
        # only reads this manifest and observes the installed dependencies.
        source_root = Path(__file__).resolve().parent
        identity = runpy.run_path(str(source_root / "jiuwenswarm" / "common" / "worker_build_identity.py"))
        identity["write_build_identity"](
            Path(self.build_lib) / "jiuwenswarm" / "_worker_build_identity.json",
            source_root,
        )


setup(cmdclass={"build_py": BuildPy})
