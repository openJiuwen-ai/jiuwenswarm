# -*- coding: utf-8 -*-
"""
main.py — 规划模块主入口（2026-08-31 plan-supervisor 接入改造）

CLI 只做三件事：
    1. 解析命令行参数（含 agent 协议新 flag：--status-file / --feedback-file）
    2. 设置 SwarmFlow runtime（backend / journal / budget / contextvars）
    3. 调 ``planning_main_session(sess=None, args)`` 业务编排层
       （Phase B 删除 main_sess 主 session 拉起 + aclose）

所有 4 个阶段的编排、4 个 sub-agent 反思循环——
都在 scripts/planning_flow.py 的 ``planning_main_session`` 里。

agent 模式（plan-supervisor 调 subprocess）vs 直接调（兼容旧版）：
    - **agent 模式**（推荐）：plan-supervisor 调 `call_planning_skill` 工具 →
      subprocess 跑本 CLI → 读 ``--status-file`` 拿 status.json →
      4 checkpoint 人审在 agent 端做（不在本 skill 内部）。
    - **直接调**（CI / 调试 / 旧兼容）：不传 ``--status-file``，按退出码 0/1 判断；
      若传 ``--no-human-review`` 走 AUTO-APPROVE 4 checkpoint（仍在 skill 内部，保留旧路径）。

升级路径：当前走 ``call_session_async`` → ``agent_session()`` 调 LLM；
将来切到 team 模式时，把 stage 脚本内部换成 ``team_session`` 算子即可，CLI 不动。

退出码（agent 协议）：
    0  = status=complete（4 stage 跑完；4 checkpoint 在 agent 端过 / 或 skill 端 AUTO-APPROVE）
    1  = status=error / preflight_* / load_inputs_* /
         unsupported_execution_capability / replan_schema_*
    ~~2~~ = ~~aborted~~（已删除——abort 决定权在 agent，skill 不知道有 abort）

Phase 3 改造（vs Phase 2 人审改造）：
    - 删除 ``agent_session(label="planning-main")`` 主 session 拉起（agent 拥有 main session）
    - 删除 ``main_sess.aclose()`` 收尾
    - planning_main_session 接受 ``sess=None``（永不调 sess.send()）
    - 新增 ``--status-file`` 写 status.json 协议（agent 端 call_planning_skill 读）
    - 新增 ``--feedback-file`` REPLAN 反馈入口（替代旧 ``--replan-from``，保留兼容）
    - 新增 ``scripts/_status.py`` helper 原子写 status.json
    - 删除退出码 2=aborted（abort 走 agent 的 modify / abort 路由）
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Any

# 模块级 logger（main() 入口会 setup_planning_logging 配置 handler）
log = logging.getLogger("planning.main")

# 强制 stdout/stderr 用 UTF-8（Windows terminal 默认 GBK 编码，LLM 输出
# 含 ⇔/⚠/✅ 等 Unicode 字符时 print 会抛 UnicodeEncodeError；统一用 utf-8
# + errors='replace' 兜底，planning skill 不再因 terminal 编码差异崩）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# 让 `from scripts._llm_backend import ...` 在 process 顶层能解析。
# 必须在 import scripts.* 之前执行。
_PLANNING_DIR = Path(__file__).resolve().parent.parent
if str(_PLANNING_DIR) not in sys.path:
    sys.path.insert(0, str(_PLANNING_DIR))
_SKILLS_DIR = _PLANNING_DIR.parent
if str(_SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILLS_DIR))


def setup_planning_logging(*, log_file: Path | None = None, level: int = logging.INFO) -> None:
    """配置 planning skill 全局日志。

    层级：root logger 名 "planning"（子模块用 "planning.main" / "planning.flow" / ...）。
    - StreamHandler → stderr（被 paper-gen forward 到主 stderr）
    - FileHandler   → output_dir/stage2_planning.log（落地，事后可 grep / tail）

    与 paper-gen 同款 ANSI 颜色；FileHandler 不上色。
    """
    root = logging.getLogger("planning")
    root.setLevel(level)
    root.propagate = False

    # 防重复 setup
    for h in list(root.handlers):
        root.removeHandler(h)

    # 1) StreamHandler
    stream_h = logging.StreamHandler(sys.stderr)
    stream_h.setLevel(level)
    stream_h.setFormatter(_color_formatter())
    root.addHandler(stream_h)

    # 2) FileHandler（落盘）
    if log_file is not None:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_h = logging.FileHandler(str(log_file), encoding="utf-8")
        file_h.setLevel(level)
        file_h.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        root.addHandler(file_h)


def _color_formatter() -> logging.Formatter:
    """terminal ANSI 颜色 formatter（FileHandler 不上色）。"""
    _LEVEL_COLORS = {
        "DEBUG":    "\033[36m",
        "INFO":     "\033[32m",
        "WARNING":  "\033[33m",
        "ERROR":    "\033[31m",
        "CRITICAL": "\033[35m",
    }
    _RESET = "\033[0m"

    class _ColorFmt(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            color = _LEVEL_COLORS.get(record.levelname, "")
            msg = super().format(record)
            if color and sys.stderr.isatty():
                return msg.replace(record.levelname, f"{color}{record.levelname}{_RESET}", 1)
            return msg

    return _ColorFmt("%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
                     datefmt="%Y-%m-%d %H:%M:%S")


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：参数解析 + 设置 runtime + 调 planning_main_session + 写 status.json。"""
    # 独立运行时加载唯一允许的用户级配置 .env 到 os.environ
    # （框架 CLI 的早期加载只在其自身入口生效）。
    try:
        from jiuwenswarm.common.utils import get_env_file
        from dotenv import load_dotenv
        env_file = get_env_file()
        if env_file.is_file():
            load_dotenv(env_file, override=False)
    except Exception as e:
        log.warning(f"加载 .env 失败（继续运行）: {e}")

    args = _parse_args(argv)
    workflow_args = _build_workflow_args(args)

    # ── 配置 planning 子进程日志：stderr + output_dir/stage2_planning.log 双写 ──
    log_file = Path(args.output_dir) / "stage2_planning.log" if args.output_dir else None
    setup_planning_logging(log_file=log_file, level=logging.INFO)
    from _token_telemetry import TokenUsageLogHandler, resolve_token_usage
    token_handler = TokenUsageLogHandler()
    root_logger = logging.getLogger()
    root_logger.addHandler(token_handler)
    try:
        from jiuwenswarm.symphony.llm import reset_llm_token_usage
        reset_llm_token_usage()
    except Exception as exc:
        log.warning("重置 token 统计失败（继续）: %s", exc)
    log.info(f"main() 启动: input_dir={args.input_dir}, output_dir={args.output_dir}")
    log.info(f"flags: no_human_review={args.no_human_review}, max_method_rounds={args.max_method_rounds}, dry_run={args.dry_run}")

    workflow_args_for_run = workflow_args

    t0 = time.monotonic()
    try:
        log.info("调 _run_planning() ...")
        result = asyncio.run(_run_planning(workflow_args_for_run))
        log.info(f"_run_planning() 返回, status={result.get('status') if isinstance(result, dict) else '?'}")
    except Exception as e:
        log.exception(f"规划流程异常: {e}")
        result = {"status": "error", "errors": [str(e)]}
    finally:
        root_logger.removeHandler(token_handler)
    wall_time = time.monotonic() - t0
    try:
        from jiuwenswarm.symphony.llm import get_llm_token_usage_summary
        tracker_usage = get_llm_token_usage_summary()
    except Exception as e:
        log.warning(f"读取 token 统计失败（不影响产物）: {e}")
        tracker_usage = {}
    llm_token_usage = resolve_token_usage(
        tracker_usage, token_handler.summary(),
        llm_expected=not bool(getattr(args, "dry_run", False)),
    )

    # ── 写 status.json（agent 协议；agent 端 call_planning_skill 读）──
    if args.status_file:
        try:
            from scripts._status import write_status_file
            # planning_main_session 返回的 dict 含 status / artifacts(=paths) / errors
            # / tier / method_rounds_used / check_feasibility / checkpoint_status；
            # wall_time 在 main 侧算
            write_status_file(
                args.status_file,
                status=result.get("status", "error"),
                artifacts=result.get("artifacts") or result.get("paths") or [],
                errors=result.get("errors", []),
                warnings=result.get("warnings", []),
                check_feasibility=result.get("check_feasibility"),
                method_rounds_used=result.get("method_rounds_used", 0),
                wall_time_seconds=wall_time,
                tier=result.get("tier", 0),
                checkpoint_status=result.get("checkpoint_status"),
                llm_token_usage=llm_token_usage,
            )
        except Exception as e:
            log.exception(f"写 status.json 失败（fallback 走退出码）: {e}")

    # ── 退出码映射（agent 端 fallback 用；status.json 优先）──
    status = result.get("status") if isinstance(result, dict) else None
    # ── 终态 progress.json（让 agent 端轮询拿到 complete/error 触发最后一帧 phase chunk）──
    _write_final_progress(args.progress_file, status or "error")
    if status == "complete":
        log.info(f"规划完成: output_dir={result.get('output_dir')}, "
                 f"tier={result.get('tier')}, wall={wall_time:.1f}s")
        return 0
    # 退出码 1 包揽所有非 complete 状态（包括原 aborted 状态——agent 决定 abort，skill 不知道）
    log.error(f"流程未完成: status={status}, errors={result.get('errors', [])[:3]}")
    return 1


async def _run_planning(workflow_args: dict) -> dict:
    """设置 SwarmFlow runtime + 调 planning_main_session（**不**拉主 session）。

    Phase 3 改造（vs Phase 2）：
        - **删除** ``agent_session(label="planning-main")`` 主 session 拉起
          （主 session 由 plan-supervisor agent 拥有）
        - **删除** ``main_sess.aclose()`` 收尾
        - planning_main_session 接受 ``sess=None``——skill 内部不再持有 main session
        - contextvar 初始化（use_provider / _rt / _path / _seq）**保留**——
          skill 内部 4 sub-agent session 仍然需要

    为什么不直接调 ``run_workflow``？
        run_workflow 加载一个 workflow script（要求 META + ``async def run``），
        业务层要从脚本里取 args，runner 在脚本外层包了 contextvar 设置和
        backend.aclose。Phase 2 砍掉 workflow script 框架后，main.py 直接
        接管这些基础设施层职责。
    """
    # ──────────── 1. 准备 backend ────────────
    try:
        from scripts._llm_backend import JiuwenBackend
        backend = JiuwenBackend()
    except Exception as e:
        log.warning(f"JiuwenBackend 初始化失败（将继续走 MockBackend，会拿不到真 LLM）: {e}")
        from openjiuwen.agent_teams.workflow.engine.backends import MockBackend
        backend = MockBackend()

    # ──────────── 2. 创建 journal（不写盘时为内存 journal）──
    from openjiuwen.agent_teams.workflow.engine.journal import Journal
    journal = await Journal.load(None, wal_path=None)

    # ──────────── 3. 创建 runtime ────────────
    from openjiuwen.agent_teams.workflow.engine.runtime import Runtime
    from openjiuwen.agent_teams.workflow.engine.budget import BudgetLedger
    # progress_sink：监听 PHASE 事件 → 写 progress.json（plan-supervisor 4 stage 任务列表用）
    # 之前是 lambda evt: None——phase() 调用全发到空气，agent 端 7 分钟无输出
    progress_sink = _build_progress_sink(workflow_args.get("progress_file"))
    rt = Runtime(
        backend=backend,
        journal=journal,
        args=workflow_args,
        log_sink=lambda msg: log.info(f"[wf] {msg}"),
        progress_sink=progress_sink,
        budget=BudgetLedger(),
    )
    rt.backend.bind_budget(rt.budget)

    # ──────────── 4. 设置 contextvars（让 agent_session() 找到 runtime）──
    from openjiuwen.agent_teams.workflow.engine.seam import (
        use_provider, reset_provider,
    )
    from openjiuwen.agent_teams.workflow.engine.provider import ENGINE_PROVIDER
    from openjiuwen.agent_teams.workflow.engine.primitives import (
        _rt, _path, _seq, _fresh_holder,
    )

    tok_prov = use_provider(ENGINE_PROVIDER)
    tok_rt = _rt.set(rt)
    tok_p = _path.set(())
    tok_s = _seq.set(_fresh_holder())
    try:
        # ──────────── 5. 调业务编排层（不拉主 session）──
        from scripts.planning_flow import planning_main_session
        result = await planning_main_session(None, workflow_args)
        return result
    finally:
        # 还原 contextvars
        _seq.reset(tok_s)
        _path.reset(tok_p)
        _rt.reset(tok_rt)
        reset_provider(tok_prov)
        # 关闭 backend（清 session 表）
        try:
            await backend.aclose()
        except Exception as exc:  # noqa: BLE001
            log.warning(f"backend.aclose 失败: {exc}")


def _build_workflow_args(args: argparse.Namespace) -> dict:
    """把 argparse Namespace 转成 planning_main_session 接收的 dict。"""
    out: dict[str, Any] = {
        "input_dir": args.input_dir,
        "output_dir": args.output_dir,
        "max_method_rounds": args.max_method_rounds,
        "no_human_review": args.no_human_review,
        "strict": args.strict,
    }
    if args.replan_from:
        out["replan_from"] = args.replan_from
    if args.feedback_file:
        out["feedback_file"] = args.feedback_file
    if args.status_file:
        out["status_file"] = args.status_file
    if args.progress_file:
        out["progress_file"] = args.progress_file
    if args.seeds is not None:
        out["seeds"] = args.seeds
    if args.max_retries is not None:
        out["max_retries"] = args.max_retries
    if args.timeout_seconds is not None:
        out["timeout_seconds"] = args.timeout_seconds
    if args.dry_run:
        out["dry_run"] = True
    return out


def _build_progress_sink(progress_file: str | None):
    """构造一个 progress_sink：监听 PHASE 事件 → 写 progress.json。

    为什么要 closure 而非直接 RT 实例属性：
        Runtime 创建时 progress_file 还没传到 sink；closure 包住进度文件路径 +
        started_at + 防御性 fallback（PHASE_ORDER 里没有的阶段名落到 index=0）。

    Returns:
        一个 ``rt.progress_sink(WorkflowProgressEvent)`` 兼容的可调用对象。
    """
    if not progress_file:
        # 旧协议 fallback：完全不写 progress.json（直接调 CLI 时保持原行为）
        return lambda evt: None

    from scripts._progress import PHASE_ORDER, write_progress_file  # 延迟 import

    phase_to_index = {p: i for i, p in enumerate(PHASE_ORDER)}
    state: dict[str, Any] = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
                       + f".{int((time.time() % 1) * 1000):03d}Z",
    }

    def sink(evt):
        try:
            kind = getattr(evt, "kind", None)
            kind_str = str(kind) if kind is not None else ""
            # 早期 WorkflowProgressEvent 用 enum；后来可能换字符串——都认
            if "PHASE" not in kind_str:
                return
            phase = getattr(evt, "phase", None) or getattr(evt, "title", None)
            if not phase:
                return
            # PHASE_ORDER 里的阶段正常定位；其他早期阶段（preflight / load_inputs）落到 index=0
            index = phase_to_index.get(phase, 0)
            write_progress_file(
                progress_file,
                current_phase=phase,
                index=index,
                total=len(PHASE_ORDER),
                phase_order=list(PHASE_ORDER),
                status="running",
                started_at=state["started_at"],
            )
        except Exception as exc:  # noqa: BLE001
            # 写盘失败不杀 subprocess；只 stderr 一行
            log.warning(f"progress_sink 写盘失败: {exc}")

    return sink


def _write_final_progress(progress_file: str | None, status: str) -> None:
    """main() 退出前写终态 progress.json（complete / error）。

    设计：phase() 在 planning_flow 里已经走完，最后一个 phase 已经被 sink 写过；
    这里再覆盖一次（status: complete 或 error），让 agent 端轮询 mtime 时拿到终态
    → 触发最后一帧 phase chunk → 框架推 chat.tool_result 走人审流程。
    """
    if not progress_file:
        return
    try:
        from scripts._progress import PHASE_ORDER, write_progress_file
        if status == "complete":
            last_phase = PHASE_ORDER[-1]  # "写产物"
            last_index = len(PHASE_ORDER) - 1
            final_status = "complete"
        else:
            # 失败时不假装走完；current_phase 标 "error" 让前端能区分
            last_phase = PHASE_ORDER[-1]
            last_index = len(PHASE_ORDER) - 1
            final_status = "error"
        write_progress_file(
            progress_file,
            current_phase=last_phase,
            index=last_index,
            total=len(PHASE_ORDER),
            phase_order=list(PHASE_ORDER),
            status=final_status,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(f"终态 progress.json 写盘失败: {exc}")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """解析命令行参数（含 agent 协议新 flag）。"""
    parser = argparse.ArgumentParser(
        prog="planning-skill",
        description="规划模块（模块二）主入口：方法设计 → 实验规划 → 门禁校验 → 写产物。"
                    "（Phase 3 改造：CLI 薄壳 → planning_main_session(sess=None) + 写 status.json）",
    )
    parser.add_argument("--input-dir", required=True, help="模块一产物目录（含 8 个 JSON 输入文件，旧缓存可缺 references.json）")
    parser.add_argument("--output-dir", required=True, help="模块二产物输出目录（生成 5 .json + 5 .md）")
    parser.add_argument("--max-method-rounds", type=int, default=3, help="方法反思最大轮数（默认 3）")
    parser.add_argument("--strict", action="store_true", help="严格模式：任一错误即返回退出码 1")
    # REPLAN 反馈（agent 协议用 --feedback-file；旧 --replan-from 保留兼容）
    parser.add_argument("--replan-from", default=None,
                        help="[旧] REPLAN 入口：上一轮 PlanningFeedback 文件路径（保留兼容；agent 用 --feedback-file）")
    parser.add_argument("--feedback-file", default=None,
                        help="[新] REPLAN 反馈 JSON 文件路径（agent 协议；替代 --replan-from）")
    # agent 协议：写 status.json
    parser.add_argument("--status-file", default=None,
                        help="[新] status.json 落盘路径（agent 协议；不传则只走退出码 fallback）")
    # agent 协议：写 progress.json（实时进度——plan-supervisor 4 stage 任务列表透出用）
    parser.add_argument("--progress-file", default=None,
                        help="[新] progress.json 落盘路径（agent 协议；phase() 切换时实时更新，"
                             "供 call_planning_skill 轮询后 yield phase chunk）")
    # 旧路径：直接调 CLI 时的人审（agent 模式下不传，agent 自己审）
    parser.add_argument("--no-human-review", action="store_true",
                        help="[旧兼容] 关闭 4 个人审检查点（CI / 批处理用；agent 模式下不传——人审归 agent）")
    # ExecutionConfig 透传
    parser.add_argument("--seeds", type=int, nargs="+", default=None, help="ExecutionConfig.seeds（默认 [42]）")
    parser.add_argument("--max-retries", type=int, default=None, help="ExecutionConfig.max_retries（默认 2）")
    parser.add_argument("--timeout-seconds", type=int, default=None, help="ExecutionConfig.timeout_seconds（默认 null）")
    parser.add_argument("--dry-run", action="store_true", default=None, help="ExecutionConfig.dry_run（默认 false）")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# 保留 _preflight 供 planning_flow.py 复用
# ---------------------------------------------------------------------------

def _preflight() -> dict:
    """启动前环境检查（planning_flow.py 直接 import 此函数）。

    Returns:
        {"critical": [...], "warning": [...], "passed": bool}
    """
    report: dict = {"critical": [], "warning": [], "passed": True}

    try:
        from jiuwenswarm.symphony.llm import LLMConfig
        LLMConfig.from_default_model()
    except Exception as e:
        report["warning"].append(
            f"LLM 客户端初始化失败（不影响流程）: {e}\n  -> 凭证源: ~/.jiuwenswarm/config/.env"
        )

    references_dir = Path(__file__).resolve().parent.parent / "references"
    for name in ("method-designer.md", "method-critic.md", "experiment-planner.md"):
        p = references_dir / name
        if not p.is_file():
            report["critical"].append(f"agent md 缺失: {p}")
            report["passed"] = False

    scripts_dir = Path(__file__).resolve().parent
    for name in (
        "planning_flow.py",
        "load_inputs.py",
        "iterate_method_design.py",
        "plan_experiment_with_data.py",
        "check_feasibility_and_downgrade.py",
        "write_outputs.py",
        "validate_plan.py",
        "estimate_compute.py",
        "_subagent.py",
        "_llm_backend.py",
        "_status.py",  # Phase 3 新增
        "_progress.py",  # Phase 4 新增（实时进度透出）
    ):
        p = scripts_dir / name
        if not p.is_file():
            report["critical"].append(f"依赖脚本缺失: {p}")
            report["passed"] = False

    for mod_name in ("pydantic", "httpx", "yaml"):
        try:
            __import__(mod_name)
        except ImportError:
            report["critical"].append(f"Python 依赖缺失: {mod_name}")
            report["passed"] = False

    try:
        from jiuwenswarm.common.utils import get_env_file
        env_file = get_env_file()
        if not env_file.is_file():
            report["warning"].append(f"用户级配置 .env 缺失: {env_file}")
    except Exception:
        pass

    if report["critical"]:
        for e in report["critical"]:
            log.error(f"[preflight][CRITICAL] {e}")
    if report["warning"]:
        for e in report["warning"]:
            log.warning(f"[preflight][WARN] {e}")
    if report["passed"] and not report["warning"]:
        log.info("[preflight] 全部检查通过")

    return report


if __name__ == "__main__":
    sys.exit(main())
