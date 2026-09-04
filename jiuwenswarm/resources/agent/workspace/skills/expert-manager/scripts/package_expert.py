#!/usr/bin/env python3
"""Expert Packager — 把专家目录打包成 .zip（用于分享或上传到包仓库）。

先校验，通过后打包；跳过 __pycache__ / .DS_Store / .gitkeep 等噪声文件，
保留 manifest.json / persona / agents / skills / avatars 等全部有效产物。
打包守卫：拒绝 output_dir 与专家目录相同（避免把自己打进包）、排除 output_dir
位于专家目录内时的产出文件、超 size 上限的不完整 zip 会被删除。

Usage:
    package_expert.py <path/to/expert-dir> [output-dir]

Example:
    python3 package_expert.py ~/.jiuwenswarm/agent/workspace/experts/my-expert
    python3 package_expert.py ./my-expert ./dist
"""
from __future__ import annotations

import sys
import zipfile
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

JUNK_NAMES = {".gitkeep", ".DS_Store", "Thumbs.db", ".created-by-session", ".managed-by-expert-manager"}
# dist = 打包产出约定目录，总是排除（避免把旧产物打进新包）；其余为噪声目录。
JUNK_DIRS = {"__pycache__", "node_modules", ".git", "dist"}

# 单个专家包大小上限（50MB）。超过则视为异常包，删除不完整 zip 并失败。
MAX_PACKAGE_BYTES = 50 * 1024 * 1024


def _is_within(candidate: Path, base: Path) -> bool:
    """candidate 是否位于 base 目录内（含相等）。"""
    try:
        candidate.relative_to(base)
        return True
    except ValueError:
        return False


def package_expert(expert_path: str | Path, output_dir: str | None = None) -> Path | None:
    expert_dir = Path(expert_path).expanduser().resolve()
    if not expert_dir.is_dir():
        print(f" 专家目录不存在: {expert_dir}")
        return None

    print(" 校验专家包...\n")
    result = validate_expert(expert_dir)
    print(result.summary())
    if not result.is_valid:
        print("\n 校验未通过，已中止打包。请先修复错误。")
        return None

    print()
    out = Path(output_dir).expanduser().resolve() if output_dir else Path.cwd()
    # 守卫：output_dir 与专家目录相同会把自己打进包，拒绝。
    if out == expert_dir:
        print(f" output_dir 不能与专家目录相同: {out}（会把产出的 zip 打进包内）")
        return None
    out.mkdir(parents=True, exist_ok=True)
    zip_path = out / f"{expert_dir.name}.zip"

    # output_dir 位于专家目录内时，需排除 output_dir 子树，避免把旧产物打进新包。
    output_inside_expert = _is_within(out, expert_dir)

    file_count = 0
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for path in sorted(expert_dir.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(expert_dir)
                parts = rel.parts
                if any(p in JUNK_DIRS for p in parts):
                    continue
                if path.name in JUNK_NAMES:
                    continue
                # 排除 output_dir 子树（避免把旧 zip 打进新包）
                if output_inside_expert and _is_within(path, out):
                    continue
                arcname = str(Path(expert_dir.name) / rel)
                zipf.write(path, arcname)
                print(f"   {arcname}")
                file_count += 1
    except Exception as exc:
        print(f" 打包失败: {exc}")
        if zip_path.exists():
            zip_path.unlink()
        return None

    # 大小守卫：超上限视为异常，删除不完整 zip。
    size = zip_path.stat().st_size
    if size > MAX_PACKAGE_BYTES:
        print(f" 包大小 {size / 1024 / 1024:.1f} MB 超过上限 {MAX_PACKAGE_BYTES / 1024 / 1024:.0f} MB，已删除不完整产物。")
        zip_path.unlink()
        return None

    print(f"\n 已打包 {file_count} 个文件到: {zip_path}")
    print(f"   大小: {size / 1024:.1f} KB")
    return zip_path


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python3 package_expert.py <path/to/expert-dir> [output-dir]")
        print("\nExample:")
        print("  python3 package_expert.py ~/.jiuwenswarm/agent/workspace/experts/my-expert")
        sys.exit(1)
    expert_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else None
    print(f" 打包专家: {expert_path}\n")
    result = package_expert(expert_path, output_dir)
    sys.exit(0 if result else 1)


if __name__ == "__main__":
    main()

