from __future__ import annotations

import os

from setuptools import setup
from setuptools.dist import Distribution
from wheel.bdist_wheel import bdist_wheel as _bdist_wheel


class BinaryDistribution(Distribution):
    def has_ext_modules(self) -> bool:
        return True


class PlatformBdistWheel(_bdist_wheel):
    def finalize_options(self) -> None:
        super().finalize_options()
        self.root_is_pure = False

    def get_tag(self) -> tuple[str, str, str]:
        _python, _abi, platform_tag = super().get_tag()
        override = os.getenv("JWC_TUI_WHEEL_PLATFORM", "").strip()
        if override:
            platform_tag = override
        return ("py3", "none", platform_tag)


setup(
    distclass=BinaryDistribution,
    cmdclass={"bdist_wheel": PlatformBdistWheel},
)