#!/usr/bin/env python3
"""Expert Initializer — jiuwenswarm 专家包目录脚手架。

从 templates/ 读取模板，替换结构占位符（__TOKEN__），内容占位符回退为 [TODO]，
产出可被 jiuwenswarm ``expert.load`` 加载的专家包目录骨架。

Usage:
    init_expert.py <expert-name> --type agent|team [--path <experts-dir>] [--members <a,b,c>]

Examples:
    python3 init_expert.py my-expert --type agent
    python3 init_expert.py my-team  --type team --members researcher,writer
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# Windows 默认 GBK 控制台无法编码 emoji，强制 UTF-8 输出
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass

SKILL_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = SKILL_DIR / "templates"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
TOKEN_RE = re.compile(r"__[A-Z0-9_]+__")


def get_experts_dir() -> Path:
    """专家包落盘目录，优先读 JIUWEN_EXPERTS_DIR 环境变量。

    对应 jiuwenswarm ``common.utils.get_agent_experts_dir()``，即
    LocalDirExpertPackageSource 的扫描根（需 JIUWEN_EXPERT_LOCAL_DIRS=1 启用）。
    """
    override = os.environ.get("JIUWEN_EXPERTS_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".jiuwenswarm" / "agent" / "workspace" / "experts"


def title_case(name: str) -> str:
    return " ".join(w.capitalize() for w in name.split("-"))


def load_template(name: str) -> str:
    path = TEMPLATES_DIR / name
    if not path.is_file():
        sys.exit(f" 模板缺失: {path}")
    return path.read_text(encoding="utf-8")


def substitute(text: str, mapping: dict[str, str]) -> str:
    """替换 __TOKEN__：mapping 命中则用值，未命中回退 [TODO]。"""
    def repl(match: re.Match[str]) -> str:
        token = match.group(0)
        return mapping.get(token, "[TODO]")
    return TOKEN_RE.sub(repl, text)


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    rel = path.relative_to(path.parent.parent)
    print(f"   {rel}")


def init_agent(expert_dir: Path, name: str) -> None:
    mapping = {
        "__EXPERT_ID__": name,
        "__TITLE__": title_case(name),
        "__EXPERT_TYPE__": "Agent 型（单个 AI 专家）",
        "__EXPERTS_DIR__": str(get_experts_dir()),
        "__TAG1__": "[TODO]",
        "__TAG2__": "[TODO]",
        "__TAG3__": "[TODO]",
        "__QUICK_PROMPT1__": "[TODO]",
        "__QUICK_PROMPT2__": "[TODO]",
        "__QUICK_PROMPT3__": "[TODO]",
        "__PROFESSION__": "[TODO]",
        "__CATEGORY_ID__": "[TODO]",
    }
    write_file(expert_dir / "manifest.json", substitute(load_template("agent-manifest.json"), mapping))
    write_file(expert_dir / "agents" / f"{name}.md", substitute(load_template("agent-persona.md"), mapping))
    (expert_dir / "avatars").mkdir(parents=True, exist_ok=True)
    (expert_dir / "avatars" / ".gitkeep").touch()
    print("   avatars/ (awaiting image generation)")
    write_file(expert_dir / "README.md", substitute(load_template("README.md"), mapping))


def init_team(expert_dir: Path, name: str, members: list[str]) -> None:
    if not members:
        members = ["member-a"]
    if "leader" in members:
        sys.exit(" 成员名 'leader' 为保留名，请改用其他 id")

    base = {
        "__EXPERT_ID__": name,
        "__MEMBER_A_ID__": members[0],
        "__AGENTS_JSON__": json.dumps(["leader", *members]),
        "__TITLE__": title_case(name),
        "__EXPERT_TYPE__": "Team 型（多角色协作团队）",
        "__EXPERTS_DIR__": str(get_experts_dir()),
        "__TAG1__": "[TODO]",
        "__TAG2__": "[TODO]",
        "__TAG3__": "[TODO]",
        "__QUICK_PROMPT1__": "[TODO]",
        "__QUICK_PROMPT2__": "[TODO]",
        "__QUICK_PROMPT3__": "[TODO]",
        "__PROFESSION__": "[TODO]",
        "__CATEGORY_ID__": "[TODO]",
    }
    # 顶层 group manifest
    write_file(expert_dir / "manifest.json", substitute(load_template("group-manifest.json"), base))
    # leader 子包：manifest.json + AGENT.md + agents/leader-persona.md（leader 用 agents 目录）
    leader_dir = expert_dir / "agents" / "leader"
    write_file(leader_dir / "manifest.json", substitute(load_template("leader-manifest.json"), base))
    write_file(leader_dir / "AGENT.md", substitute(load_template("leader-AGENT.md"), base))
    write_file(leader_dir / "agents" / "leader-persona.md", substitute(load_template("leader-persona.md"), base))
    # 各成员子包：manifest.json + persona/<member>.md（成员用 persona 目录，禁 AGENT.md）
    for mid in members:
        m = {**base, "__MEMBER_ID__": mid}
        member_dir = expert_dir / "agents" / mid
        write_file(member_dir / "manifest.json", substitute(load_template("member-manifest.json"), m))
        write_file(member_dir / "persona" / f"{mid}.md", substitute(load_template("member-persona.md"), m))
    # avatars（leader + 各成员，文件名 = 成员 id）
    avatars_dir = expert_dir / "avatars"
    avatars_dir.mkdir(parents=True, exist_ok=True)
    (avatars_dir / ".gitkeep").touch()
    print("   avatars/ (awaiting image generation, ids: leader, " + ", ".join(members) + ")")
    # README
    write_file(expert_dir / "README.md", substitute(load_template("README.md"), base))


def parse_members(raw: str) -> list[str]:
    members = [m.strip() for m in raw.split(",") if m.strip()]
    seen: set[str] = set()
    for m in members:
        if not NAME_RE.match(m):
            sys.exit(f" 成员名 '{m}' 必须是 kebab-case")
        if m in seen:
            sys.exit(f" 成员名 '{m}' 重复")
        seen.add(m)
    return members


def main() -> None:
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)

    name = sys.argv[1]
    expert_type = None
    output_path = None
    members_raw = None

    i = 2
    while i < len(sys.argv):
        if sys.argv[i] == "--type" and i + 1 < len(sys.argv):
            expert_type = sys.argv[i + 1]; i += 2
        elif sys.argv[i] == "--path" and i + 1 < len(sys.argv):
            output_path = sys.argv[i + 1]; i += 2
        elif sys.argv[i] == "--members" and i + 1 < len(sys.argv):
            members_raw = sys.argv[i + 1]; i += 2
        else:
            i += 1

    if expert_type not in ("agent", "team"):
        sys.exit(" --type 必须是 agent 或 team")

    if not NAME_RE.match(name):
        sys.exit(f" name '{name}' 必须是 kebab-case（小写字母/数字/连字符，≥2 字符）")

    # --path 可选：缺省走 get_experts_dir()（~/.jiuwenswarm/agent/workspace/experts 或 JIUWEN_EXPERTS_DIR）
    if output_path:
        experts_dir = Path(output_path).expanduser().resolve()
        # 显式传 --path 时必须是专家根目录，避免生成到不可见位置
        if experts_dir != get_experts_dir().resolve():
            sys.exit(
                f" --path 必须为专家根目录: {get_experts_dir()}\n"
                f"   当前: {experts_dir}\n"
                f"   非专家根目录下的包无法被 expert.load 发现。可用 JIUWEN_EXPERTS_DIR 覆盖默认路径。"
            )
    else:
        experts_dir = get_experts_dir().resolve()

    expert_dir = experts_dir / name
    if expert_dir.exists():
        sys.exit(f" 目录已存在: {expert_dir}")

    expert_dir.mkdir(parents=True)
    print(f" 初始化 {expert_type} 专家: {name}")
    print(f"   位置: {expert_dir}\n")

    if expert_type == "agent":
        init_agent(expert_dir, name)
    else:
        members = parse_members(members_raw) if members_raw else []
        init_team(expert_dir, name, members)

    print(f"\n Expert '{name}' ({expert_type}) 已初始化于 {expert_dir}")
    print("\n下一步:")
    print("  1. 填写所有 [TODO] 占位符（manifest.json / persona / AGENT.md）")
    print("  2. 生成头像到 avatars/（见 references/avatar-spec.md）")
    print("  3. 运行 validate_expert.py 校验包")
    print("  4. 运行 register_expert.py 确认可发现性")
    print("  5. 确保 JIUWEN_EXPERT_LOCAL_DIRS=1 启用本地源后即可 expert.load")


if __name__ == "__main__":
    main()
