# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``jiuwenswarm-loop`` CLI 入口。

用法示例::

    jiuwenswarm-loop --cwd /path/to/workspace \
        --trusted-dir /path/to/workspace \
        --verify "bash /path/to/verify.sh" \
        "请阅读 task.md 并修复其中描述的 bug"

遵循 channels/cli/main.py 的 dotenv 两段式加载与退出码惯例。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys


def main() -> None:
    # 第一段：任何 jiuwenswarm import 之前解析早期 dotenv（JIUWENSWARM_DATA_DIR 等）
    try:
        from jiuwenswarm.dotenv_early import parse_dotenv_early

        parse_dotenv_early("jiuwenswarm")
    except KeyboardInterrupt:
        logging.warning("Interrupted during startup. Exiting.")
        sys.exit(130)

    import argparse
    from pathlib import Path

    from jiuwenswarm.channels.loop_cli.app import LoopEngine, LoopOptions

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    # 第二段：主体 import 之后加载 ~/.jiuwenswarm/config/.env（API 凭证等）
    if os.environ.get("JIUWENSWARM_SKIP_DOTENV", "").strip() != "1":
        try:
            from dotenv import load_dotenv
            from jiuwenswarm.common.utils import get_env_file

            load_dotenv(dotenv_path=get_env_file(), override=False)
        except ImportError:
            pass
        except KeyboardInterrupt:
            logging.warning("Interrupted during startup. Exiting.")
            sys.exit(130)

    parser = argparse.ArgumentParser(
        prog="jiuwenswarm-loop",
        description="Loop Engineering 任务编排：rubric 分解 → maker（jiuwenswarm "
                    "harness）→ 机器验证 → 独立 grader 验收 → gap 回炉循环。",
    )
    parser.add_argument("task", help="任务描述；若以 @ 开头则读取该文件内容作为任务")
    parser.add_argument("--cwd", default=os.getcwd(),
                        help="maker 工作目录（默认当前目录）")
    parser.add_argument("--project-dir", default=None,
                        help="项目标识目录（默认取 --cwd）")
    parser.add_argument("--trusted-dir", action="append", default=None,
                        help="信任目录，可重复（默认取 --cwd）")
    parser.add_argument("--verify", dest="verify_cmd", default=None,
                        help="机器验证命令；退出码 0 视为通过（强烈建议提供）")
    parser.add_argument("--diff-repo", default=None,
                        help="git diff 取证目录（默认取 --cwd；非 git 任务用 --evidence-file）")
    parser.add_argument("--evidence-file", action="append", default=None,
                        help="产物文件证据路径，可重复；非 git 任务时把文件内容"
                             "直接注入 grader 证据（如写作任务的输出文档）")
    parser.add_argument("--mode", default="agent.code.normal",
                        help="maker 执行模式（默认 agent.code.normal）")
    parser.add_argument("--max-iterations", type=int, default=3,
                        help="最大迭代轮数（默认 3）")
    parser.add_argument("--state-dir", default=None,
                        help="状态输出目录（默认 <cwd>/loop_state）")
    parser.add_argument("--round-timeout", type=float, default=900.0,
                        help="maker 单轮超时秒数（默认 900）")

    try:
        args = parser.parse_args()
    except KeyboardInterrupt:
        logging.warning("Interrupted during startup. Exiting.")
        sys.exit(130)

    task = args.task
    if task.startswith("@"):
        task_path = Path(task[1:])
        if not task_path.is_file():
            logging.error("jiuwenswarm-loop: 任务文件不存在: %s", task_path)
            sys.exit(2)
        task = task_path.read_text(encoding="utf-8")

    options = LoopOptions(
        task=task,
        cwd=os.path.abspath(args.cwd),
        project_dir=os.path.abspath(args.project_dir) if args.project_dir else None,
        trusted_dirs=[os.path.abspath(d) for d in (args.trusted_dir or [])],
        verify_cmd=args.verify_cmd,
        diff_repo=os.path.abspath(args.diff_repo) if args.diff_repo else None,
        evidence_files=[os.path.abspath(f) for f in (args.evidence_file or [])],
        mode=args.mode,
        max_iterations=max(1, args.max_iterations),
        state_dir=os.path.abspath(args.state_dir) if args.state_dir else None,
        round_timeout=args.round_timeout,
    )

    def log(phase: str, **kw) -> None:
        logging.info("[loop][%s] %s", phase,
                     " ".join(f"{k}={str(v)[:110]}" for k, v in kw.items()))

    async def _run() -> int:
        engine = LoopEngine(options, log=log)
        report = await engine.run()
        logging.info("\n========== Loop Engineering 结果 ==========")
        logging.info("循环终态     : %s", report.final)
        logging.info("机器验证     : %s", "✅ PASS" if report.verify_pass else "❌ FAIL")
        logging.info("迭代轮数     : %s", report.iterations)
        logging.info("maker tokens : %s", report.maker_tokens)
        logging.info("耗时         : %ss", report.wall_seconds)
        logging.info("状态文件     : %s", report.state_path)
        logging.info("rubric        :")
        for r in report.rubric:
            logging.info("  - %s", r)
        logging.info("=============================================")
        if report.final == "satisfied" and report.verify_pass:
            return 0
        if report.final == "max_iterations_reached":
            return 2
        if report.final == "failed":
            return 3
        return 1

    try:
        sys.exit(asyncio.run(_run()))
    except KeyboardInterrupt:
        logging.warning("Interrupted. Exiting.")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        logging.error("jiuwenswarm-loop: 运行失败：%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
