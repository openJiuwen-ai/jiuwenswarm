# -*- coding: utf-8 -*-
"""main.py — paper-gen CLI 入口：argparse + 调 main_flow.run() + 退出码映射。

设计：模仿 planning main.py 风格，但只做 CLI 薄壳——argparse + .env 加载 + asyncio.run(main_flow.run)
+ 退出码映射。**不**直接管 SwarmFlow runtime（3 stage subprocess 各自起自己的 backend）。

调用：
    uv run python -m jiuwenswarm.resources.agent.workspace.skills.paper_gen.scripts.main \\
        --input-dir mock/run1 --output-dir out/run1 [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

# 模块级 logger（在 main() 里被 setup_logging 重置 root handler）
log = logging.getLogger("paper_gen.main")

# 让 `from scripts.main_flow import run` + `from scripts._stage_runner import ...` 能解析
_PAPER_GEN_DIR = Path(__file__).resolve().parent.parent
if str(_PAPER_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_PAPER_GEN_DIR))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="paper-gen",
        description="CCF BDCI 端到端 4 stage orchestration: conception → planning → experiment → writing",
    )
    p.add_argument("--input-dir", type=Path,
                   help="用户输入目录（至少含 00_user_request.json）；不与 --stage*-dir 并用")
    p.add_argument("--output-dir", required=True, type=Path,
                   help="端到端产物输出根目录")
    p.add_argument("--force", action="store_true",
                   help="忽略 stage 缓存，全部重跑（默认：跳过 status=complete 的 stage）")
    p.add_argument("--dry-run", action="store_true",
                   help="仅 writing 阶段使用 MockBackend；用于不调用外部模型的链路验证")
    p.add_argument("--max-replan-rounds", type=int, default=2,
                   help="experiment → planning 自动重规划的最大轮数（默认 2；超限终态为 replan_exhausted）")
    p.add_argument("--dataset-cache-dir", type=Path,
                   help="跨 REPLAN 复用的已验证原始数据缓存目录；省略时位于 output_dir 同级 .paper-gen-cache/datasets")
    p.add_argument("--status", action="store_true",
                   help="只查询 output_dir 下各 stage 缓存状态，不跑任何 stage；需 --output-dir")
    p.add_argument("--stage1-dir", type=Path, help="已完成模块一产物目录，仅运行新版写作阶段")
    p.add_argument("--stage2-dir", type=Path, help="已完成模块二产物目录，仅运行新版写作阶段")
    p.add_argument("--stage3-dir", type=Path, help="已完成模块三产物目录，仅运行新版写作阶段")
    args = p.parse_args(argv)
    stage_dirs = (args.stage1_dir, args.stage2_dir, args.stage3_dir)
    if any(stage_dirs) and not all(stage_dirs):
        p.error("--stage1-dir、--stage2-dir 与 --stage3-dir 必须同时提供")
    if args.input_dir and all(stage_dirs):
        p.error("--input-dir 不能与 --stage*-dir 并用")
    if not args.input_dir and not all(stage_dirs):
        p.error("请提供 --input-dir，或同时提供三个 --stage*-dir")
    return args


def _load_dotenv() -> None:
    """加载 JiuwenSwarm 用户级 ``~/.jiuwenswarm/config/.env``。"""
    try:
        from jiuwenswarm.common.utils import get_env_file
        from dotenv import load_dotenv
        env_file = get_env_file()
        if env_file.is_file():
            load_dotenv(env_file, override=False)
    except Exception as e:
        log.warning(f".env 加载失败（继续）: {e}")
    # 本机可能配置了 HTTP(S)_PROXY。OpenAI/httpx 会读取这些变量；若 NO_PROXY
    # 没含 localhost，请求 http://127.0.0.1:8000/v1 会绕去系统代理并返回 502，
    # 本地火山转接器完全收不到请求。
    for key in ("NO_PROXY", "no_proxy"):
        current = os.environ.get(key, "")
        entries = [item.strip() for item in current.split(",") if item.strip()]
        for local in ("127.0.0.1", "localhost"):
            if local not in entries:
                entries.append(local)
        os.environ[key] = ",".join(entries)


def _print_cache_status(output_dir: Path) -> int:
    """查询 output_dir 下各 stage 缓存状态；不跑 stage。退出码 0。"""
    print(f"=== paper-gen cache status: {output_dir} ===")
    if not output_dir.is_dir():
        print("(output_dir 不存在 → 全部 stage 都没跑过)")
        return 0

    stage_dirs = [
        ("conception", "stage1_conception"),
        ("planning",   "stage2_planning"),
        ("experiment", "stage3_experiment"),
        ("writing",    "stage4_writing"),
    ]
    found_any = False
    for name, sub in stage_dirs:
        stage_dir = output_dir / sub
        status_file = stage_dir / "status.json"
        print(f"\n[{name}] {stage_dir}")
        if not stage_dir.is_dir():
            print("  (dir 不存在 → 未跑过)")
            continue
        if not status_file.is_file():
            print("  (no status.json → 跑了但没落 status)")
            continue
        try:
            payload = json.loads(status_file.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  (status.json 解析失败: {e})")
            continue
        status = payload.get("status", "?")
        wall = payload.get("wall_time_seconds", 0)
        token_total = ((payload.get("llm_token_usage") or {}).get("total") or {}).get("total_tokens", 0)
        cached = "CACHED" if status == "complete" else "WILL RE-RUN"
        print(f"  status={status}  wall={wall:.1f}s  tokens={token_total}  cache_state={cached}")
        artifacts = payload.get("artifacts") or []
        if artifacts:
            for a in artifacts[:5]:
                print(f"    - {a}")
            if len(artifacts) > 5:
                print(f"    ... +{len(artifacts) - 5} more")
        found_any = True

    # run_summary 整体
    run_summary = output_dir / "run_summary.json"
    if run_summary.is_file():
        print(f"\n[run_summary] {run_summary}")
        try:
            rs = json.loads(run_summary.read_text(encoding="utf-8"))
            print(f"  end_status={rs.get('status', '?')}, wall={rs.get('wall_time_seconds', 0):.1f}s")
            for stage, info in rs.get("stages", {}).items():
                cached_mark = "CACHED" if info.get("cached") else "RAN"
                print(f"    {stage}: {cached_mark} status={info.get('status', '?')}, wall={info.get('wall_time_seconds', 0):.1f}s")
        except Exception as e:
            print(f"  (parse 失败: {e})")

    token_usage = output_dir / "token_usage.json"
    if token_usage.is_file():
        print(f"\n[token_usage] {token_usage}")
        try:
            usage = json.loads(token_usage.read_text(encoding="utf-8"))
            successful = ((usage.get("successful_path") or {}).get("total") or {})
            unsuccessful = ((usage.get("replan_or_failed") or {}).get("total") or {})
            overall = usage.get("overall_total") or usage.get("total") or {}
            completed = bool(
                (usage.get("successful_path") or {}).get("full_pipeline_completed")
            )
            print(
                "  successful_path="
                f"{int(successful.get('total_tokens') or 0)}"
                + ("" if completed else " (provisional; pipeline incomplete)")
            )
            print(
                "  replan_or_failed="
                f"{int(unsuccessful.get('total_tokens') or 0)}"
            )
            print(f"  overall_total={int(overall.get('total_tokens') or 0)}")
        except Exception as e:
            print(f"  (token_usage.json 解析失败: {e})")

    if not found_any:
        print("\n(没有任何 stage 跑过)")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    # ── 配置 paper-gen 全局日志：stderr + output_dir/paper_gen.log 双写 ──
    # 先用 lenient parser 拿 --output-dir（--status 模式也要走日志）
    from _logging import setup_logging
    _pre = argparse.ArgumentParser(add_help=False)
    # Do not make this early logging-only parser authoritative: argparse must
    # still be able to render ``--help`` without an output path.  The formal
    # parser below enforces the requirement for every execution mode.
    _pre.add_argument("--output-dir", type=Path)
    _pre.add_argument("--status", action="store_true")
    _pre_args, _ = _pre.parse_known_args(argv)
    log_file = _pre_args.output_dir / "paper_gen.log" if _pre_args.output_dir else None
    setup_logging(log_file=log_file, level=logging.INFO)
    log.info(f"paper-gen main() 启动: argv={argv}")
    _load_dotenv()

    # 预解析：--status 不需要 input-dir
    import sys
    if "--status" in (argv or sys.argv[1:]):
        # 简化：从 argv 拿 --output-dir
        p = argparse.ArgumentParser(add_help=False)
        p.add_argument("--output-dir", type=Path, required=True)
        p.add_argument("--status", action="store_true")
        a, _ = p.parse_known_args(argv)
        return _print_cache_status(a.output_dir)

    args = _parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    try:
        from scripts.main_flow import run as flow_run, run_writing_from_stages
        if args.stage1_dir:
            result = asyncio.run(run_writing_from_stages({
                "stage1_dir": str(args.stage1_dir),
                "stage2_dir": str(args.stage2_dir),
                "stage3_dir": str(args.stage3_dir),
                "output_dir": str(args.output_dir),
                "dry_run": args.dry_run,
            }))
        else:
            result = asyncio.run(flow_run({
                "input_dir": str(args.input_dir),
                "output_dir": str(args.output_dir),
                "force": args.force,
                "dry_run": args.dry_run,
                "max_replan_rounds": args.max_replan_rounds,
                "dataset_cache_dir": (str(args.dataset_cache_dir) if args.dataset_cache_dir else None),
            }))
    except Exception:
        import traceback
        log.exception("main_flow.run 异常")
        traceback.print_exc(file=sys.stderr)
        return 1
    wall = time.monotonic() - t0

    status = result.get("status", "error")
    log.info(f"终态: {status}, wall={wall:.1f}s, summary={args.output_dir / 'run_summary.json'}")
    # 只有具备正式发布资格的 complete 才返回成功。partial 是写作诊断终态，
    # 可能表示审查未通过、修订未收敛或 PDF 门禁失败。
    if status == "complete":
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
