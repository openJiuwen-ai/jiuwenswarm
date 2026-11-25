"""WorkSwarm 主题背景（Gateway Extension V2）。

V1 主题接口的主题包（theme.json，theme_api 1）经转换后，由宿主的应用背景、启动开场、受控外观与
设置页能力显示；设计见仓库 docs/DESIGN.md。
"""

from __future__ import annotations

PACKAGE_ID = "workswarm-theme"
ENGINE_VERSION = "2.0.0"
THEME_API_VERSION = 1
DEFAULT_THEME = "workswarm-theme-research-terminal"
#: Built-in themes in the order the settings page lists them.
BUILTIN_THEMES = (
    "workswarm-theme-research-terminal",
    "workswarm-theme-blueprint",
    "workswarm-theme-aurora",
    "workswarm-theme-hive",
)

__all__ = ["BUILTIN_THEMES", "DEFAULT_THEME", "ENGINE_VERSION", "PACKAGE_ID", "THEME_API_VERSION"]
