#!/usr/bin/env python3
"""
Agent Template Initializer — Creates a new agent template package

Usage:
    init_template.py <agent-name>
    init_template.py <path-to-package-dir>
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
  "package_type": "agent_template",
  "name": "%(name)s",
  "description": "[TODO: 一句话描述]",
  "persona": {
    "dir": "./persona"
  },
  "display_name": {
    "en": "[TODO: EN display name]",
    "zh": "[TODO: 中文展示名]"
  },
  "display_description": {
    "en": "[TODO: EN description]",
    "zh": "[TODO: 中文描述，建议 40-50 字]"
  },
  "category": "[TODO: category]",
  "avatar": "",
  "source": "local",
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

[TODO: 一句话描述这个 agent 是谁、擅长什么]

## 核心能力

- [TODO: 能力1]
- [TODO: 能力2]
- [TODO: 能力3]

## 使用方式

[TODO: 在 JiuwenSwarm 扩展打开该专家，按引导对话。]
"""

PERSONA_TEMPLATE = """# [TODO: 角色名称] - [TODO: 人设名]

[TODO: 一段角色描述——你是谁、服务谁、核心任务是什么、用什么语气工作]

## 核心能力
1. **[TODO: 能力1]**：[TODO: 描述]
2. **[TODO: 能力2]**：[TODO: 描述]
3. **[TODO: 能力3]**：[TODO: 描述]

## 工作流程
1. **[TODO: 阶段1]**：[TODO: 何时进入、做什么、调用什么工具/skill]
2. **[TODO: 阶段2]**：[TODO: ...]
3. **[TODO: 阶段3]**：[TODO: ...]

## 输出规范
- [TODO: 规范1]
- [TODO: 规范2]

## 注意事项
- [TODO: 约束或边界条件]
"""

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")


def get_jiuwenswarm_data_dir() -> Path:
    """Read data root from env; host must inject JIUWENSWARM_DATA_DIR when non-default."""
    raw = os.environ.get("JIUWENSWARM_DATA_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".jiuwenswarm"


def get_agent_workspace_dir() -> Path:
    return get_jiuwenswarm_data_dir() / "agent" / "workspace"


def get_agent_templates_local_dir() -> Path:
    return get_agent_workspace_dir() / "plugins" / "agent_templates" / "local"


def get_agent_templates_built_in_dir() -> Path:
    return get_agent_workspace_dir() / "plugins" / "agent_templates" / "built_in"


def title_case(name: str) -> str:
    return " ".join(w.capitalize() for w in name.split("-"))


def _assert_package_id_available(name: str) -> None:
    local_dir = get_agent_templates_local_dir() / name
    built_in_dir = get_agent_templates_built_in_dir() / name
    if local_dir.exists():
        raise ValueError(
            f"Error: agent_template already exists in local: {local_dir}\n"
            "  Do not overwrite. Rename, or delete the old package first."
        )
    if built_in_dir.exists():
        raise ValueError(
            f"Error: agent_template already exists in built_in: {built_in_dir}\n"
            "  Choose a different agent-name."
        )


def resolve_init_target(arg: str) -> Path:
    raw = arg.strip()
    if "/" in raw or "\\" in raw:
        resolved = Path(raw).expanduser().resolve()
        expected_parent = get_agent_templates_local_dir().resolve()
        if resolved.parent != expected_parent:
            raise ValueError(
                "Error: explicit package path must be under local agent_templates:\n"
                f"  Expected parent: {expected_parent}\n"
                f"  Got:             {resolved}"
            )
        return resolved
    if len(raw) < 2 or not NAME_RE.match(raw):
        raise ValueError(
            f"Error: agent-name {raw!r} must be kebab-case "
            f"(lowercase letters, digits, hyphens)"
        )
    _assert_package_id_available(raw)
    return get_agent_templates_local_dir() / raw


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
    write_stdout(f"Initializing agent template: {name}\n")
    write_stdout(f"  Path: {pkg_dir}\n\n")

    (pkg_dir / "manifest.json").write_text(
        MANIFEST_TEMPLATE % {"name": name}, encoding="utf-8"
    )
    write_stdout("  created manifest.json\n")

    (pkg_dir / "README.md").write_text(
        README_TEMPLATE % {"title": title_case(name)}, encoding="utf-8"
    )
    write_stdout("  created README.md\n")

    persona_dir = pkg_dir / "persona"
    persona_dir.mkdir()
    (persona_dir / f"{name}.md").write_text(PERSONA_TEMPLATE, encoding="utf-8")
    write_stdout(f"  created persona/{name}.md\n")

    write_stdout(f"\nSkeleton ready at {pkg_dir}\n")
    write_stdout("Files contain [TODO] placeholders — fill them in the next step.\n")
    return pkg_dir


def main() -> int:
    if len(sys.argv) < 2:
        write_stdout("Usage: init_template.py <agent-name>\n")
        write_stdout("\n<agent-name> = kebab-case package id\n")
        write_stdout("Default output (under JIUWENSWARM_DATA_DIR or ~/.jiuwenswarm):\n")
        write_stdout(
            "  <data-dir>/agent/workspace/plugins/agent_templates/local/<agent-name>/\n"
        )
        write_stdout("\nExample:\n")
        write_stdout("  python3 init_template.py my-expert\n")
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
