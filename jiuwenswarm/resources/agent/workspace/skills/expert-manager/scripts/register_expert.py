#!/usr/bin/env python3
"""Expert Register — jiuwenswarm 专家可发现性确认。

jiuwenswarm 没有 marketplace.json 注册表：LocalDirExpertPackageSource 直接扫描
专家根目录（``~/.jiuwenswarm/agent/workspace/experts``，需 JIUWEN_EXPERT_LOCAL_DIRS=1）。
因此 "注册" 等价于：终检（无 [TODO] 占位符）+ 确认包位于专家根目录 + 提示可发现性。

Usage:
    register_expert.py <expert-dir> [--session-id <id>]

Examples:
    python3 register_expert.py ~/.jiuwenswarm/agent/workspace/experts/my-expert
    python3 register_expert.py ./my-expert --session-id abc-123
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Windows 默认 GBK 控制台无法编码 emoji，强制 UTF-8 输出
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass

script_dir = Path(__file__).parent.resolve()
sys.path.insert(0, str(script_dir))
from validate_expert import validate_expert  # noqa: E402


def get_experts_dir() -> Path:
    override = os.environ.get("JIUWEN_EXPERTS_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".jiuwenswarm" / "agent" / "workspace" / "experts"


def check_completeness(expert_dir: Path) -> list[str]:
    """检查是否还有未填充的 [TODO] 占位符。返回违规文件列表。"""
    issues: list[str] = []
    for path in sorted(expert_dir.rglob("*")):
        if not path.is_file():
            continue
        # 跳过二进制（头像）
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        todo_count = text.count("[TODO")
        if todo_count > 0:
            rel = path.relative_to(expert_dir)
            issues.append(f"{rel}: 仍有 {todo_count} 处 [TODO]")
    return issues


def write_session_marker(expert_dir: Path, session_id: str) -> None:
    marker = expert_dir / ".managed-by-expert-manager"
    try:
        marker.write_text(session_id, encoding="utf-8")
    except OSError as exc:
        print(f" 无法写入 session 标记: {exc}")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    expert_dir = Path(sys.argv[1]).expanduser().resolve()
    session_id = None
    i = 2
    while i < len(sys.argv):
        if sys.argv[i] == "--session-id" and i + 1 < len(sys.argv):
            session_id = sys.argv[i + 1]
            i += 2
        else:
            i += 1

    if not expert_dir.is_dir():
        sys.exit(f" 专家目录不存在: {expert_dir}")

    print(f" 终检专家包: {expert_dir.name}")
    result = validate_expert(expert_dir)
    print(result.summary())
    if not result.is_valid:
        sys.exit("\n 校验未通过，无法确认可发现性。请先修复上述错误。")

    # [TODO] 占位符检查
    issues = check_completeness(expert_dir)
    if issues:
        print("\n 仍有未填充的 [TODO] 占位符：")
        for issue in issues:
            print(f"   - {issue}")
        sys.exit("   请补全内容后再注册。")

    # 确认位于专家根目录
    experts_dir = get_experts_dir().resolve()
    try:
        expert_dir.relative_to(experts_dir)
        in_place = True
    except ValueError:
        in_place = False

    if session_id:
        write_session_marker(expert_dir, session_id)

    print(f"\n 专家 '{expert_dir.name}' 内容完整、校验通过")
    if in_place:
        print(f"   位置: {expert_dir}（位于专家根目录，可被 expert.load 发现）")
        local_enabled = os.environ.get("JIUWEN_EXPERT_LOCAL_DIRS") == "1"
        if local_enabled:
            print("   JIUWEN_EXPERT_LOCAL_DIRS=1 已启用本地源 → 立即可见于 experts.list")
        else:
            print("    需设置 JIUWEN_EXPERT_LOCAL_DIRS=1 启用本地源后才可见于 experts.list")
    else:
        print(f"    当前位置 {expert_dir} 不在专家根目录 {experts_dir} 下")
        print(f"      请移动到 {experts_dir / expert_dir.name} 后再 expert.load")


if __name__ == "__main__":
    main()
