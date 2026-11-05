"""Stamp wheel build metadata without modifying the source checkout."""

from pathlib import Path
import runpy

from setuptools import setup
from setuptools.command.bdist_wheel import bdist_wheel
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist


def _validate_release_dependencies() -> None:
    root = Path(__file__).parent
    helper = runpy.run_path(str(root / "scripts" / "release_dependencies.py"))
    helper["validate_release_dependencies"](root)


class BuildPy(build_py):
    def run(self) -> None:
        super().run()
        helper = runpy.run_path(str(Path(__file__).parent / "scripts" / "build_config.py"))
        helper["write_build_metadata"](
            Path(self.build_lib) / "jiuwenswarm" / "resources" / "build-metadata.json",
            self.distribution.get_version(),
        )


class BdistWheel(bdist_wheel):
    def run(self) -> None:
        _validate_release_dependencies()
        super().run()


class Sdist(sdist):
    def run(self) -> None:
        _validate_release_dependencies()
        super().run()


setup(cmdclass={"build_py": BuildPy, "bdist_wheel": BdistWheel, "sdist": Sdist})
