#!/usr/bin/env python3
"""批量创建专家 — 每个专家串行走完整标准流程（init → [AI 填充] → validate → register）。

Usage:
    python3 scripts/batch_create.py <batch-config.json> [--session-id <id>]

batch-config.json:
    {
      "path": "<experts-dir>",
      "experts": [
        { "name": "my-expert", "type": "agent" },
        { "name": "my-team", "type": "team", "members": ["researcher", "writer"] }
      ]
    }

注意：Step 2（AI 填充内容）由调用方（AI）在 init 之后、validate 之前完成；
本脚本不生成内容，只驱动 init → validate → register 的串行流程。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Windows 默认 GBK 控制台无法编码 emoji，强制 UTF-8 输出
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass

SCRIPT_DIR = Path(__file__).parent.resolve()
REGISTER_MAX_RETRIES = 2
REGISTER_RETRY_DELAY = 1


def run_step(script: str, args: list[str]) -> bool:
    cmd = [sys.executable, str(SCRIPT_DIR / script)] + args
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.returncode != 0 and proc.stderr.strip():
        print(proc.stderr.strip())
    return proc.returncode == 0


def process_one(expert: dict, output_dir: Path, session_id: str | None) -> bool:
    name = expert.get("name", "")
    etype = expert.get("type", "agent")
    if not name:
        return False

    print(f"\n{'─' * 40}")
    print(f" [{name}] Step 1/3: 初始化")
    init_args = [name, "--type", etype, "--path", str(output_dir)]
    if etype == "team" and expert.get("members"):
        init_args += ["--members", ",".join(expert["members"])]
    if not run_step("init_expert.py", init_args):
        print(f"    [{name}] 初始化失败，跳过")
        return False

    print(f"   ⏸  [{name}] Step 2/3: AI 填充内容（由调用方完成）")
    print(f"      请填写 {output_dir / name} 下所有 [TODO]，再继续。")
    # 批量场景下 AI 应在调用本脚本前已填充内容；此处只做流程占位。
    # 若内容未就绪，下一步 validate 会拦截。

    print(f" [{name}] Step 3/3: 校验")
    if not run_step("validate_expert.py", [str(output_dir / name)]):
        print(f"    [{name}] 校验失败，请修复后重试")
        print(f"      python3 validate_expert.py {output_dir / name}")
        return False

    print(f" [{name}] 确认可发现性")
    reg_args = [str(output_dir / name)]
    if session_id:
        reg_args += ["--session-id", session_id]
    for attempt in range(1, REGISTER_MAX_RETRIES + 1):
        if run_step("register_expert.py", reg_args):
            print(f"    [{name}] 完成")
            return True
        if attempt < REGISTER_MAX_RETRIES:
            print(f"    注册失败，{REGISTER_RETRY_DELAY}s 后重试 ({attempt}/{REGISTER_MAX_RETRIES})...")
            time.sleep(REGISTER_RETRY_DELAY)
    print(f"    [{name}] 注册失败（已重试 {REGISTER_MAX_RETRIES} 次）")
    return False


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    config_path = Path(sys.argv[1]).resolve()
    session_id = None
    i = 2
    while i < len(sys.argv):
        if sys.argv[i] == "--session-id" and i + 1 < len(sys.argv):
            session_id = sys.argv[i + 1]
            i += 2
        else:
            i += 1

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f" 配置文件不存在：{config_path}")
    except json.JSONDecodeError as exc:
        sys.exit(f" 配置文件不是合法 JSON: {exc}")
    except UnicodeDecodeError as exc:
        sys.exit(f" 配置文件编码错误：{exc}")
    default_dir = os.environ.get("JIUWEN_EXPERTS_DIR", "").strip()
    if default_dir:
        default_dir = str(Path(default_dir).expanduser().resolve())
    else:
        default_dir = str(Path.home() / ".jiuwenswarm" / "agent" / "workspace" / "experts")
    output_dir = Path(config.get("path", default_dir)).expanduser().resolve()
    experts = config.get("experts", [])
    if not experts:
        sys.exit(" 配置中无专家列表")

    print(f" 批量创建 {len(experts)} 个专家 → {output_dir}\n")
    passed: list[str] = []
    failed: list[str] = []
    # 串行执行：禁止并行/异步
    for expert in experts:
        if process_one(expert, output_dir, session_id):
            passed.append(expert.get("name", ""))
        else:
            failed.append(expert.get("name", ""))

    print(f"\n{'═' * 40}")
    print(f" 结果: {len(passed)} 成功, {len(failed)} 失败")
    if failed:
        print(f"   失败: {', '.join(failed)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
