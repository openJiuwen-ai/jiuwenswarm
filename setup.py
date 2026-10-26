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


setup(cmdclass={"build_py": BuildPy})
