#!/usr/bin/env python3
"""
Plugin Package Initializer — Creates a new plugin package

Usage:
    init_plugin.py <plugin-name>
    init_plugin.py <path-to-package-dir>
"""

import os
import re
import sys
from pathlib import Path


def write_stdout(text: str) -> None:
    """CLI product output to fd 1 (avoid print/sys.stdout for G.LOG.02)."""
    os.write(1, text.encode("utf-8"))


MANIFEST_TEMPLATE = """{
  "version": "1.0.0",
  "package_type": "plugin",
  "id": "%(name)s",
  "name": "[TODO: 中文插件名]",
  "description": "[TODO: 一句话描述]",
  "display_name": {
    "en": "[TODO: EN display name]",
    "zh": "[TODO: 中文展示名]"
  },
  "display_description": {
    "en": "[TODO: EN description]",
    "zh": "[TODO: 中文描述，建议 40-50 字]"
  },
  "category": "[TODO: category]",
  "source": "local",
  "avatar": "",
  "default_init_input": {
    "zh": "[TODO: 中文首次对话提示]",
    "en": "[TODO: English first prompt]"
  },
  "tags": [
    {
      "en": "[TODO: Tag1 EN]",
      "zh": "[TODO: 标签1]"
    },
    {
      "en": "[TODO: Tag2 EN]",
      "zh": "[TODO: 标签2]"
    },
    {
      "en": "[TODO: Tag3 EN]",
      "zh": "[TODO: 标签3]"
    }
  ],
  "quick_inputs": [
    {
      "en": "[TODO: Prompt1 EN, same as default_init_input]",
      "zh": "[TODO: 提示词1，同 default_init_input]"
    },
    {
      "en": "[TODO: Prompt2 EN]",
      "zh": "[TODO: 提示词2]"
    },
    {
      "en": "[TODO: Prompt3 EN]",
      "zh": "[TODO: 提示词3]"
    }
  ]
}
"""

README_TEMPLATE = """# %(title)s

[TODO: 一句话描述这个插件提供什么能力扩展]

## 核心能力

- [TODO: 能力1]
- [TODO: 能力2]
- [TODO: 能力3]

## 使用方式

[TODO: 在 JiuwenSwarm 扩展安装并启用本插件，对话输入区勾选插件 chip，然后发送推荐问法。]
"""

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")


def get_jiuwenswarm_data_dir() -> Path:
    raw = os.environ.get("JIUWENSWARM_DATA_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".jiuwenswarm"


def get_agent_workspace_dir() -> Path:
    return get_jiuwenswarm_data_dir() / "agent" / "workspace"


def get_plugin_packages_local_dir() -> Path:
    return get_agent_workspace_dir() / "plugins" / "plugin_packages" / "local"


def get_plugin_packages_built_in_dir() -> Path:
    return get_agent_workspace_dir() / "plugins" / "plugin_packages" / "built_in"


def title_case(name: str) -> str:
    return " ".join(w.capitalize() for w in name.split("-"))


def _assert_package_id_available(name: str) -> None:
    local_dir = get_plugin_packages_local_dir() / name
    built_in_dir = get_plugin_packages_built_in_dir() / name
    if local_dir.exists():
        raise ValueError(
            f"Error: plugin package already exists in local: {local_dir}\n"
            "  Do not overwrite. Rename, or delete the old package first."
        )
    if built_in_dir.exists():
        raise ValueError(
            f"Error: plugin package already exists in built_in: {built_in_dir}\n"
            "  Choose a different plugin-name."
        )


def resolve_init_target(arg: str) -> Path:
    raw = arg.strip()
    if "/" in raw or "\\" in raw:
        resolved = Path(raw).expanduser().resolve()
        expected_parent = get_plugin_packages_local_dir().resolve()
        if resolved.parent != expected_parent:
            raise ValueError(
                "Error: explicit package path must be under local plugin_packages:\n"
                f"  Expected parent: {expected_parent}\n"
                f"  Got:             {resolved}"
            )
        return resolved
    if len(raw) < 2 or not NAME_RE.match(raw):
        raise ValueError(
            f"Error: plugin-name {raw!r} must be kebab-case "
            f"(lowercase letters, digits, hyphens)"
        )
    _assert_package_id_available(raw)
    return get_plugin_packages_local_dir() / raw


def init_package(pkg_dir: Path) -> Path:
    name = pkg_dir.name
    if len(name) < 2 or not NAME_RE.match(name):
        raise ValueError(
            f"Error: path basename '{name}' must be kebab-case "
            f"(lowercase letters, digits, hyphens)"
        )

    if pkg_dir.exists():
        raise ValueError(
            f"Error: directory already exists: {pkg_dir}\n"
            "  Do not overwrite. Rename, or delete the old package first."
        )

    pkg_dir.parent.mkdir(parents=True, exist_ok=True)
    pkg_dir.mkdir()
    write_stdout(f"Initializing plugin package: {name}\n")
    write_stdout(f"  Path: {pkg_dir}\n\n")

    (pkg_dir / "manifest.json").write_text(
        MANIFEST_TEMPLATE % {"name": name}, encoding="utf-8"
    )
    write_stdout("  created manifest.json\n")

    (pkg_dir / "README.md").write_text(
        README_TEMPLATE % {"title": title_case(name)}, encoding="utf-8"
    )
    write_stdout("  created README.md\n")

    write_stdout(f"\nSkeleton ready at {pkg_dir}\n")
    write_stdout("Files contain [TODO] placeholders — fill them in the next step.\n")
    return pkg_dir


def main() -> int:
    if len(sys.argv) < 2:
        write_stdout("Usage: init_plugin.py <plugin-name>\n")
        write_stdout("\n<plugin-name> = kebab-case package id\n")
        write_stdout("Default output (under JIUWENSWARM_DATA_DIR or ~/.jiuwenswarm):\n")
        write_stdout(
            "  <data-dir>/agent/workspace/plugins/plugin_packages/local/<plugin-name>/\n"
        )
        write_stdout("\nExample:\n")
        write_stdout("  python3 init_plugin.py my-plugin\n")
        return 1

    try:
        pkg_dir = resolve_init_target(sys.argv[1])
        init_package(pkg_dir)
    except ValueError as exc:
        write_stdout(str(exc) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
