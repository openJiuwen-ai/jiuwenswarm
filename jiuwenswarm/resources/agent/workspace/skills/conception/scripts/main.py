# -*- coding: utf-8 -*-
"""conception-skill CLI 薄壳：unwrap --input-dir → 调 workflow.run() → 写 status.json。

设计：
    - 接受 --input-dir --output-dir --status-file --progress-file（与 planning main.py 对齐）
    - 复用 planning/scripts/_status.py 的 write_status_file（sys.path 注入）
    - 走 Jiuwen 原生 research_agent 与文献检索工具
    - 退出码：status in (PASS/completed/complete) → 0；其他 → 1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

# 让 `from scripts.workflow import run` + `from scripts._llm_backend import JiuwenBackend` 能解析
_CONCEPTION_DIR = Path(__file__).resolve().parent.parent
if str(_CONCEPTION_DIR) not in sys.path:
    sys.path.insert(0, str(_CONCEPTION_DIR))
_SKILLS_DIR = _CONCEPTION_DIR.parent
if str(_SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILLS_DIR))

# 复用 planning/scripts/_status.py 的 write_status_file
_PLANNING_SCRIPTS = _CONCEPTION_DIR.parent / "planning" / "scripts"
if str(_PLANNING_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_PLANNING_SCRIPTS))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="conception-skill")
    p.add_argument("--input-dir", required=True, type=Path,
                   help="含 00_user_request.json 的目录")
    p.add_argument("--output-dir", required=True, type=Path,
                   help="产物输出目录（生成 conception_output.json）")
    p.add_argument("--status-file", default=None,
                   help="[agent 协议] status.json 落盘路径")
    p.add_argument("--progress-file", default=None,
                   help="[agent 协议] progress.json 落盘路径（conception 暂不细分阶段，写 1 帧占位）")
    return p.parse_args(argv)


async def _run_with_runtime(payload: dict, output_dir: Path | None = None) -> dict:
    """研究代理自行创建原生模型，无需 SwarmFlow 或 MockBackend。"""
    from scripts.workflow import run as workflow_run
    return await workflow_run(payload)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="[conception] %(message)s")
    # Native research_agent calls may not return usage to Jiuwen's process-wide
    # tracker.  Keep a per-invocation log collector as a non-duplicating
    # fallback; resolve_token_usage still prefers the provider tracker.
    from _token_telemetry import TokenUsageLogHandler, resolve_token_usage
    token_handler = TokenUsageLogHandler()
    root_logger = logging.getLogger()
    root_logger.addHandler(token_handler)
    try:
        from jiuwenswarm.symphony.llm import reset_llm_token_usage
        reset_llm_token_usage()
    except Exception as e:
        print(f"[conception] 重置 token 统计失败（继续）: {e}", file=sys.stderr)
    # .env 加载（与 planning main.py:69-76 同模式）
    try:
        from jiuwenswarm.common.utils import get_env_file
        from dotenv import load_dotenv
        env_file = get_env_file()
        if env_file.is_file():
            load_dotenv(env_file, override=False)
    except Exception as e:
        print(f"[conception] .env 加载失败（继续）: {e}", file=sys.stderr)

    args = _parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # 在首次 LLM/网络请求之前落一帧运行中状态。顶层编排因而可以区分“尚未
    # 启动”与“正在文献检索”，即使外部学术 API 之后超时也会有可见的进度。
    if args.progress_file:
        try:
            from _progress import write_progress_file
            write_progress_file(
                args.progress_file,
                current_phase="文献检索",
                index=0,
                total=2,
                phase_order=["文献检索", "构思生成"],
                status="running",
            )
        except Exception as e:
            print(f"[conception] 初始 progress.json 写盘失败（继续）: {e}", file=sys.stderr)

    # 读 input_dir/00_user_request.json
    req_path = args.input_dir / "00_user_request.json"
    if not req_path.is_file():
        print(f"[conception] input 缺失: {req_path}", file=sys.stderr)
        return 1
    try:
        req = json.loads(req_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[conception] input 无法解析: {type(exc).__name__}", file=sys.stderr)
        return 1
    if not isinstance(req, dict):
        print("[conception] input 顶层必须是 JSON 对象", file=sys.stderr)
        return 1

    # 跑业务
    t0 = time.monotonic()
    try:
        result = asyncio.run(_run_with_runtime(req, args.output_dir))
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        result = {"status": "FAILED", "errors": [str(e)]}
    finally:
        root_logger.removeHandler(token_handler)
    wall = time.monotonic() - t0
    try:
        from jiuwenswarm.symphony.llm import get_llm_token_usage_summary
        tracker_usage = get_llm_token_usage_summary()
    except Exception as e:
        print(f"[conception] 读取 token 统计失败（不影响产物）: {e}", file=sys.stderr)
        tracker_usage = {}
    llm_token_usage = resolve_token_usage(
        tracker_usage, token_handler.summary(), llm_expected=True,
    )

    # 写产物
    out_json = args.output_dir / "conception_output.json"
    out_json.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    status_str = "complete" if result.get("status") in ("PASS", "completed", "complete") else "error"

    # 写 status.json
    if args.status_file:
        from _status import write_status_file
        write_status_file(
            args.status_file,
            status=status_str,
            artifacts=[str(out_json)],
            errors=result.get("errors", []),
            warnings=result.get("warnings", []),
            wall_time_seconds=wall,
            method_rounds_used=0,
            tier=0,
            llm_token_usage=llm_token_usage,
        )

    # 写 progress.json（占位 1 帧，让 agent 端轮询能拿到终态）
    if args.progress_file:
        try:
            from _progress import write_progress_file
            # conception 自身有 2 phase（文献检索 / 构思生成），但父级编排用 PHASE_ORDER 也行
            failed_during_search = result.get("error_type") == "literature_source_unavailable"
            write_progress_file(
                args.progress_file,
                current_phase="文献检索" if failed_during_search else "构思生成",
                index=0 if failed_during_search else 1,
                total=2,
                phase_order=["文献检索", "构思生成"],
                status=status_str,
            )
        except Exception as e:
            print(f"[conception] progress.json 写盘失败: {e}", file=sys.stderr)

    print(
        f"[conception] 构思完成: status={result.get('status')}, "
        f"artifacts={[str(out_json)]}, wall={wall:.1f}s",
        file=sys.stderr,
    )
    return 0 if result.get("status") in ("PASS", "completed", "complete") else 1


if __name__ == "__main__":
    sys.exit(main())
