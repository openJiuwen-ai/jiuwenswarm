# -*- coding: utf-8 -*-
"""
main.py — writing-skill 端到端 CLI 薄壳（planning 同构改造，2026-09-07）

CLI 只做四件事：
    1. 解析命令行参数（含 agent 协议新 flag：--status-file / --progress-file）
    2. 设置 SwarmFlow runtime（backend / journal / budget / contextvars）
    3. 调 ``workflow_v2.run(args)`` 证据优先的结构化写作流程
    4. 写 status.json（paper-gen 协议）+ 终态 progress.json

所有编排位于 scripts/workflow_v2.py；不兼容 Part1/Part2 协议。

调用：
    uv run python -m jiuwenswarm.resources.agent.workspace.skills.writing.scripts.main \\
        --module1 <m1.json> --module2 <m2.json> --module3 <m3.json> \\
        --output-dir <dir> [--status-file <s>] [--progress-file <p>] \\
        [--no-human-review] [--max-revision-rounds N] [--title X] [--dry-run]

退出码：
    0  = status=complete（4 stage 跑完 + paper.pdf 落盘）
    1  = status=error / partial / preflight_* / load_inputs_* / aborted
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
from typing import Any

# 模块级 Python logger（main() 入口会 setup_writing_logging 配置 handler）
log = logging.getLogger("writing.main")

# 强制 stdout/stderr 用 UTF-8（Windows terminal 默认 GBK 编码，LLM 输出
# 含 ⇔/⚠/✅ 等 Unicode 字符时 print 会抛 UnicodeEncodeError；统一用 utf-8
# + errors='replace' 兜底，writing skill 不再因 terminal 编码差异崩）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# 让 `from scripts._llm_backend import ...` / `from scripts.workflow_v2 import ...`
# 在 process 顶层能解析。必须在 import scripts.* 之前执行。
_WRITING_DIR = Path(__file__).resolve().parent.parent
if str(_WRITING_DIR) not in sys.path:
    sys.path.insert(0, str(_WRITING_DIR))
_SKILLS_DIR = _WRITING_DIR.parent
if str(_SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILLS_DIR))


# ──────────────────────────────────────────────────────────────
# logging setup（与 planning setup_planning_logging 同构）
# ──────────────────────────────────────────────────────────────


def setup_writing_logging(*, log_file: Path | None = None, level: int = logging.INFO) -> None:
    """配置 writing skill 全局日志。

    层级：root logger 名 "writing"（子模块用 "writing.main" / "writing.flow" / ...）。
    - StreamHandler → stderr（被 paper-gen forward 到主 stderr）
    - FileHandler   → output_dir/writing.log（落地，事后可 grep / tail）

    与 paper-gen 同款 ANSI 颜色；FileHandler 不上色。
    """
    root = logging.getLogger("writing")
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
        "WARNING":   "\033[33m",
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


# ──────────────────────────────────────────────────────────────
# 状态映射：workflow_v2.run() → status.json _VALID_STATUS
# ──────────────────────────────────────────────────────────────

# writing_flow.run() 实际返回的 status 值（见 writing/scripts/writing_flow.py:272-291）
# → status.json schema 的 _VALID_STATUS 集映射表
_WRITING_TO_STATUS_MAP = {
    "success":             "complete",
    "partial":             "partial",
    "failed":              "error",
    "aborted":             "aborted",
    "load_inputs_failed":  "load_inputs_failed",
    "preflight_failed":    "preflight_failed",
    "preflight_error":     "preflight_failed",
    "invalid_execution_evidence": "error",
    # These are meaningful writing states but paper-gen's transport schema has
    # no equivalent values.  Both must remain non-complete downstream.
    "needs_experiment_data": "partial",
    "synthetic_test":       "partial",
}


def _map_writing_status(raw_status: str) -> str:
    """把 workflow_v2.run() 返回的 status 映射到状态文件值。"""
    return _WRITING_TO_STATUS_MAP.get(raw_status, "error")


def _ensure_terminal_verdict(result: dict[str, Any]) -> dict[str, Any]:
    """Make every terminal writing result machine-readable.

    The workflow has legitimate early exits before a review verdict exists
    (input, preflight, agent and controlled-audit failures).  Leaving those
    exits without a verdict made callers display ``unknown`` and obscured the
    recovery route.  Preserve an explicit verdict when present and otherwise
    derive a stable, status-scoped diagnostic rather than treating failure as
    a successful review decision.
    """
    if not isinstance(result, dict) or str(result.get("verdict") or "").strip():
        return result
    status = str(result.get("status") or "writing_error").strip().casefold()
    inferred = {
        "success": "pass",
        "partial": "writing_incomplete",
        "load_inputs_failed": "input_contract_failed",
        "preflight_failed": "preflight_failed",
        "preflight_error": "preflight_failed",
        "invalid_execution_evidence": "invalid_execution_evidence",
        "needs_experiment_data": "upstream_evidence_required",
        "agent_execution_failed": "agent_execution_failed",
        "aborted": "writing_aborted",
        "failed": "writing_failed",
    }.get(status, f"{status}_failed")
    return {**result, "verdict": inferred}


def _status_diagnostics(result: dict[str, Any]) -> tuple[list[str], list[str], str | None]:
    """Preserve a terminal evidence failure in the transport status file.

    ``workflow_v2`` deliberately stops before any LLM call when Module 3's
    execution ledger is invalid.  That is an error for a caller, not a vague
    warning: no draft was reviewed and no paper may be released.
    """
    errors = [str(item) for item in result.get("errors") or []]
    warnings = [str(item) for item in result.get("warnings") or []]
    verdict = result.get("verdict")
    if result.get("status") == "invalid_execution_evidence":
        findings = (result.get("execution_integrity") or {}).get("findings") or []
        finding_codes = [str(item.get("code") or "invalid_execution_evidence") for item in findings if isinstance(item, dict)]
        errors.extend(finding_codes or ["invalid_execution_evidence"])
        verdict = "invalid_execution_evidence"
    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings)), verdict


# ──────────────────────────────────────────────────────────────
# main 入口
# ──────────────────────────────────────────────────────────────


class OutputDirectoryBusy(RuntimeError):
    """Raised when another live writer already owns the same output path."""


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _acquire_output_lock(output_dir: str) -> tuple[Path, str]:
    """Atomically allow exactly one module-four process per output directory."""
    output = Path(output_dir).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output.parent / f".{output.name}.writing.lock"
    token = f"{os.getpid()}-{time.time_ns()}"
    payload = json.dumps({
        "pid": os.getpid(), "token": token, "output_dir": str(output), "created_ns": time.time_ns(),
    }, ensure_ascii=False).encode("utf-8")
    for _ in range(2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                owner = json.loads(lock_path.read_text(encoding="utf-8"))
                owner_pid = int(owner.get("pid") or 0)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                owner_pid = 0
            if _pid_is_alive(owner_pid):
                raise OutputDirectoryBusy(
                    f"another writing process (pid={owner_pid}) already owns {output}"
                )
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return lock_path, token
    raise OutputDirectoryBusy(f"could not acquire writing lock for {output}")


def _release_output_lock(lock_path: Path, token: str) -> None:
    try:
        owner = json.loads(lock_path.read_text(encoding="utf-8"))
        if owner.get("token") == token:
            lock_path.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：参数解析、运行 workflow_v2、写状态文件。"""
    # 独立运行时手动加载用户级 ~/.jiuwenswarm/config/.env 到 os.environ。
    # get_env_file() 在已初始化环境中始终解析到这一个配置源；skill 内不维护
    # 单独的凭据文件。（框架 CLI 的 parse_dotenv_early 只在其入口生效。）
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

    # ── 配置 writing 子进程日志：stderr + output_dir/writing.log 双写 ──
    log_file = Path(args.output_dir) / "writing.log" if args.output_dir else None
    setup_writing_logging(log_file=log_file, level=logging.INFO)
    from _token_telemetry import TokenUsageLogHandler, resolve_token_usage
    token_handler = TokenUsageLogHandler()
    root_logger = logging.getLogger()
    root_logger.addHandler(token_handler)
    try:
        from jiuwenswarm.symphony.llm import reset_llm_token_usage
        reset_llm_token_usage()
    except Exception as exc:
        log.warning("重置 token 统计失败（继续）: %s", exc)
    log.info("─" * 60)
    log.info(f"main() 启动: module1={args.module1}, module2={args.module2}, "
             f"module3={args.module3}")
    log.info(f"  output_dir={args.output_dir}")
    log.info(f"  flags: no_human_review={args.no_human_review}, "
             f"max_revision_rounds={args.max_revision_rounds}, "
             f"dry_run={args.dry_run}, title={args.title!r}")
    log.info(f"  status_file={args.status_file}, progress_file={args.progress_file}")

    try:
        output_lock, output_lock_token = _acquire_output_lock(args.output_dir)
    except OutputDirectoryBusy as exc:
        # Do not overwrite the active process's status/progress files.
        log.error(f"拒绝并发写入: {exc}")
        return 1

    t0 = time.monotonic()
    try:
        log.info("调 _run_writing() ...")
        result = asyncio.run(_run_writing(workflow_args))
        log.info(f"_run_writing() 返回, raw_status={result.get('status') if isinstance(result, dict) else '?'}")
    except Exception as e:
        log.exception(f"写作流程异常: {e}")
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

    # ── 写 status.json（paper-gen 协议；paper-gen stage_runner 读）──
    raw_status = result.get("status") if isinstance(result, dict) else "error"
    mapped_status = _map_writing_status(raw_status)
    status_errors, status_warnings, status_verdict = _status_diagnostics(result)
    if args.status_file:
        try:
            from scripts._status import write_status_file
            # writing_flow.run() 返回的 dict 含 status / output_dir / pdf_path / tex_path
            # / verdict / rounds_used；wall_time 在 main 侧算
            write_status_file(
                args.status_file,
                status=mapped_status,
                artifacts=result.get("artifacts") or [str(p) for p in
                    (Path(args.output_dir) if args.output_dir else Path()).iterdir()
                    if p.is_file() and not p.name.startswith("_")
                ] if args.output_dir else [],
                errors=status_errors,
                warnings=status_warnings,
                wall_time_seconds=wall_time,
                revision_rounds_used=result.get("rounds_used", 0),
                pdf_path=result.get("pdf_path") or "",
                verdict=status_verdict,
                llm_token_usage=llm_token_usage,
            )
        except Exception as e:
            log.exception(f"写 status.json 失败（fallback 走退出码）: {e}")

    # ── 退出码映射（paper-gen stage_runner fallback 用；status.json 优先）──
    # ── 终态 progress.json（让 paper-gen 端轮询拿到真实终态）──
    _write_final_progress(args.progress_file, mapped_status)
    if mapped_status == "complete":
        log.info(f"✓ 写作完成: output_dir={result.get('output_dir')}, "
                 f"pdf={result.get('pdf_path')}, wall={wall_time:.1f}s")
        log.info("─" * 60)
        _release_output_lock(output_lock, output_lock_token)
        return 0
    # 退出码 1 包揽所有非 complete 状态（partial/error/aborted/preflight_*/load_inputs_*）
    log.error(f"✗ 写作未完成: mapped_status={mapped_status} (raw={raw_status}), "
              f"errors={result.get('errors', [])[:3]}, wall={wall_time:.1f}s")
    log.info("─" * 60)
    _release_output_lock(output_lock, output_lock_token)
    return 1


# ──────────────────────────────────────────────────────────────
# 业务流程：setup runtime + contextvars + 调 writing_flow.run
# ──────────────────────────────────────────────────────────────


async def _run_writing(workflow_args: dict) -> dict:
    """设置 SwarmFlow runtime + 调 workflow_v2.run()（**不**拉主 session）。

    设计：与 planning/scripts/main.py:_run_planning() 同构——
        - 准备 backend（JiuwenBackend / MockBackend fallback）
        - 创建 journal（不写盘时为内存 journal）
        - 创建 runtime（含 progress_sink 把 phase() 事件转写到 progress.json）
        - 设置 contextvars（让 facade.agent() 找到 runtime）
        - 调业务编排层 workflow_v2.run(args)
        - 还原 contextvars + backend.aclose()
    """
    # ──────────── 1. 准备 backend ────────────
    backend_label = "JiuwenBackend"
    try:
        if workflow_args.get("dry_run"):
            raise RuntimeError("dry-run requests MockBackend")
        from scripts._llm_backend import JiuwenBackend
        backend = JiuwenBackend()
    except Exception as e:
        log.warning(f"JiuwenBackend 初始化失败（fallback MockBackend）: {e}")
        from openjiuwen.agent_teams.workflow.engine.backends import MockBackend
        backend = MockBackend()
        backend_label = "MockBackend"

    # ──────────── 2. 创建可恢复 journal ────────────
    # The workflow journal is an exact-content cache, not a semantic cache:
    # structural call path + prompt signature must both match.  Keeping it
    # outside stage4_writing lets a failed output directory be replaced while
    # still reusing completed, byte-identical LLM calls on the next attempt.
    from openjiuwen.agent_teams.workflow.engine.journal import Journal
    output_dir = Path(workflow_args["output_dir"]).resolve()
    cache_dir = output_dir.parent / ".writing-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_flavour = "mock" if backend_label == "MockBackend" else "real"
    # v3 changes evidence-assertion identity and removes the legacy generic
    # writer/reviewer/reviser protocol. Never replay incompatible v2 answers.
    journal_path = cache_dir / f"workflow-v3-{cache_flavour}.jsonl"
    journal_wal_path = Path(str(journal_path) + ".wal")
    journal = await Journal.load(str(journal_path), wal_path=str(journal_wal_path))

    # ──────────── 3. 创建 runtime ────────────
    from openjiuwen.agent_teams.workflow.engine.runtime import Runtime
    from openjiuwen.agent_teams.workflow.engine.budget import BudgetLedger
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

    # ──────────── 4. 设置 contextvars（让 facade.agent() 找到 runtime）──
    from openjiuwen.agent_teams.workflow.engine.seam import use_provider, reset_provider
    from openjiuwen.agent_teams.workflow.engine.provider import ENGINE_PROVIDER
    from openjiuwen.agent_teams.workflow.engine.primitives import (
        _rt, _path, _seq, _fresh_holder,
    )

    log.info(f"  → _run_writing() 设置 runtime: backend={backend_label}, "
             f"journal={journal_path}, progress_sink={'active' if workflow_args.get('progress_file') else 'noop'}")

    tok_prov = use_provider(ENGINE_PROVIDER)
    tok_rt = _rt.set(rt)
    tok_p = _path.set(())
    tok_s = _seq.set(_fresh_holder())
    result: dict | None = None
    try:
        # ──────────── 5. 调业务编排层 ────────────
        from scripts.workflow_v2 import finalize_existing, run as writing_run
        from scripts._subagent import AgentCallError
        log.info(f"  → 调 workflow_v2.run(args) ... evidence-first writing pipeline")
        try:
            runner = finalize_existing if workflow_args.get("resume_finalize") else writing_run
            result = _ensure_terminal_verdict(await runner(workflow_args))
        except AgentCallError as exc:
            output_dir = Path(workflow_args["output_dir"])
            failure_path = output_dir / "reviews" / "agent_execution_failure.json"
            failure_path.parent.mkdir(parents=True, exist_ok=True)
            failure_path.write_text(json.dumps({
                "agent": exc.name,
                "reason": exc.reason,
                "retry_policy": {
                    "timeout_seconds": int(os.getenv("WRITING_AGENT_TIMEOUT_SECONDS", "300")),
                    "max_attempts": max(1, int(os.getenv("WRITING_AGENT_MAX_ATTEMPTS", "2"))),
                },
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            result = _ensure_terminal_verdict({
                "status": "agent_execution_failed",
                "output_dir": str(output_dir),
                "artifacts": [str(failure_path)],
                "warnings": [f"{exc.name} could not complete its bounded LLM call."],
                "failed_agent": exc.name,
            })
        log.info(f"  → workflow_v2.run() 返回: status={result.get('status')}, "
                 f"pdf={result.get('pdf_path')}, rounds={result.get('rounds_used')}")
        return result
    finally:
        # Save even a partial run. Fresh calls are already crash-safe in the
        # WAL; the ordered snapshot makes the next identical retry cheap.
        try:
            if isinstance(result, dict) and result.get("status") == "success":
                await journal.finalize(str(journal_path))
            else:
                await journal.save(str(journal_path))
            log.info(
                f"  → workflow journal 已保存: hits={journal.hits}, "
                f"misses={max(0, len(journal.used) - journal.hits)}, path={journal_path}"
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(f"workflow journal 保存失败（不影响业务产物）: {exc}")
        # 还原 contextvars
        _seq.reset(tok_s)
        _path.reset(tok_p)
        _rt.reset(tok_rt)
        reset_provider(tok_prov)
        # 关闭 backend（清 session 表）
        try:
            await backend.aclose()
            log.info(f"  → _run_writing() 收尾: backend.aclose() 完成")
        except Exception as exc:  # noqa: BLE001
            log.warning(f"backend.aclose 失败: {exc}")


# ──────────────────────────────────────────────────────────────
# progress_sink / 终态 progress.json（与 planning 同构）
# ──────────────────────────────────────────────────────────────


def _build_progress_sink(progress_file: str | None):
    """构造一个 progress_sink：监听 PHASE 事件 → 写 progress.json。

    为什么要 closure 而非直接 RT 实例属性：
        Runtime 创建时 progress_file 还没传到 sink；closure 包住进度文件路径 +
        started_at + 防御性 fallback（PHASE_ORDER 里没有的阶段名落到 index=0）。
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
            if kind_str.lower() != "phase":
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
            log.info(f"  → progress.json: phase {index+1}/{len(PHASE_ORDER)} = {phase!r}")
        except Exception as exc:  # noqa: BLE001
            # 写盘失败不杀 subprocess；只 stderr 一行
            log.warning(f"progress_sink 写盘失败: {exc}")

    return sink


def _write_final_progress(progress_file: str | None, status: str) -> None:
    """main() 退出前写真实终态 progress.json。 

    设计：phase() 在 writing_flow 里已经走完，最后一个 phase 已经被 sink 写过；
    这里再覆盖一次（complete / partial / aborted / error），让 paper-gen 端轮询 mtime 时拿到终态
    → 触发最后一帧 phase chunk → 框架推 chat.tool_result 走人审流程。
    """
    if not progress_file:
        return
    try:
        from scripts._progress import PHASE_ORDER, read_progress_file, write_progress_file
        if status == "complete":
            last_phase = PHASE_ORDER[-1]  # "PDF 编译"
            last_index = len(PHASE_ORDER) - 1
            final_status = "complete"
        else:
            # Partial/aborted/error 都停在实际最后阶段，不能把未发生的
            # Tectonic 编译伪装成失败地点。
            previous = read_progress_file(progress_file) or {}
            last_phase = str(previous.get("current_phase") or "未开始")
            last_index = int(previous.get("index") or 0)
            final_status = status if status in {"partial", "aborted", "error"} else "error"
        write_progress_file(
            progress_file,
            current_phase=last_phase,
            index=last_index,
            total=len(PHASE_ORDER),
            phase_order=list(PHASE_ORDER),
            status=final_status,
            started_at=previous.get("started_at") if status != "complete" and isinstance(previous, dict) else None,
        )
        log.info(f"  → 终态 progress.json: status={final_status}, phase={last_phase!r}")
    except Exception as exc:  # noqa: BLE001
        log.warning(f"终态 progress.json 写盘失败: {exc}")


# ──────────────────────────────────────────────────────────────
# 参数解析 + workflow_args 构造
# ──────────────────────────────────────────────────────────────


def _build_workflow_args(args: argparse.Namespace) -> dict:
    """把 argparse Namespace 转成 writing_flow.run 接收的 dict。

    字段名严格对齐 writing/scripts/writing_flow.py:run() 的 args 契约（line 96-105）。
    """
    out: dict[str, Any] = {
        # 3 个上游 JSON 路径（必填）
        "module1": str(args.module1),
        "module2": str(args.module2),
        "module3": str(args.module3),
        # writing 产物目录
        "output_dir": str(args.output_dir),
        # 反思循环
        "no_human_review": bool(args.no_human_review),
        "max_revision_rounds": int(args.max_revision_rounds),
        "title_override": args.title,
        "conference": str(args.conference),
        "source_manifest": str(args.source_manifest) if args.source_manifest else None,
        # LLM 后端
        "dry_run": bool(args.dry_run),
        "resume_finalize": bool(args.resume_finalize),
        "refresh_front": bool(args.refresh_front),
        # paper-gen 协议（薄壳自己消费，不透传给 writing_flow）
        "status_file": str(args.status_file) if args.status_file else None,
        "progress_file": str(args.progress_file) if args.progress_file else None,
    }
    return out


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """解析命令行参数（含 paper-gen 协议新 flag）。"""
    parser = argparse.ArgumentParser(prog="writing-skill", description="证据优先的结构化 ICLR 写作流程。")
    # 3 个上游 JSON（必填）
    parser.add_argument("--module1", required=True,
                        help="模块一（Conception）JSON 路径")
    parser.add_argument("--module2", required=True,
                        help="模块二（Planning）JSON 路径")
    parser.add_argument("--module3", required=True,
                        help="模块三（Execution）JSON 路径")
    parser.add_argument("--source-manifest", default=None,
                        help="上游只读来源清单；声明 evidence_mode、状态、路径及哈希")
    # writing 产物目录
    parser.add_argument("--output-dir", required=True,
                        help="writing 产物输出目录（生成 paper.pdf/.tex/.json + sections/）")
    # 反思循环
    parser.add_argument("--no-human-review", action="store_true",
                        help="关闭 1 个 human() 检查点（CI / paper-gen 端到端用）")
    parser.add_argument("--max-revision-rounds", type=int, default=3,
                        help="反思循环最大轮数（默认 3；超轮 → revise_exhausted）")
    parser.add_argument("--title", default=None,
                        help="覆盖论文标题（默认从 m1.research_question.topic 读）")
    parser.add_argument("--conference", default="iclr2024",
                        help="已安装会议模板包；默认 iclr2024")
    # LLM 后端
    parser.add_argument("--dry-run", action="store_true",
                        help="走 MockBackend（不调真 LLM，CI / smoke test 用）")
    parser.add_argument("--resume-finalize", action="store_true",
                        help="仅续跑既有冻结产物的受控审计、终稿审查与 PDF；不重写正文或资产")
    parser.add_argument("--refresh-front", action="store_true",
                        help="与 --resume-finalize 配合，重新生成并校验冻结正文对应的标题与摘要")
    # paper-gen 协议
    parser.add_argument("--status-file", default=None,
                        help="status.json 落盘路径（paper-gen 协议；不传则只走退出码 fallback）")
    parser.add_argument("--progress-file", default=None,
                        help="progress.json 落盘路径（paper-gen 协议；phase() 切换时实时更新）")
    return parser.parse_args(argv)


# ──────────────────────────────────────────────────────────────
# preflight（writing_flow.py 内部会调）
# ──────────────────────────────────────────────────────────────


def _preflight() -> dict:
    """启动前环境检查（writing_flow.py 直接 import 此函数）。

    Returns:
        {"critical": [...], "warning": [...], "passed": bool}
    """
    report: dict = {"critical": [], "warning": [], "passed": True}

    try:
        from jiuwenswarm.symphony.llm import LLMConfig
        LLMConfig.from_default_model()
    except Exception as e:
        report["warning"].append(
            f"LLM 客户端初始化失败（不影响流程，会 fallback MockBackend）: {e}\n"
            f"  -> 凭证源: {get_env_file()}"
        )

    references_dir = Path(__file__).resolve().parent.parent / "references"
    for name in (
        "paper_architect.md", "method_writer.md", "results_writer.md", "related_work_writer.md",
        "introduction_writer.md", "limitations_writer.md", "conclusion_writer.md", "integration_editor.md", "claim_verifier.md",
        "literature_novelty_reviewer.md", "argument_reviewer.md", "revision_coordinator.md", "revision_adjudicator.md",
        "final_paper_reviewer.md", "formatter.md", "visual_planner.md", "visual_reviewer.md",
    ):
        p = references_dir / name
        if not p.is_file():
            report["critical"].append(f"agent md 缺失: {p}")
            report["passed"] = False

    scripts_dir = Path(__file__).resolve().parent
    for name in (
        "load_inputs.py",
        "stage7_compile.py",
        "template_registry.py",
        "workflow_v2.py",
        "paper_contract.py",
        "evidence.py",
        "assets.py",
        "visuals.py",
        "structured_render.py",
        "_subagent.py",
        "_llm_backend.py",
        "_status.py",  # 2026-09-07 改造新增
        "_progress.py",  # 2026-09-07 改造新增
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

    # LaTeX 工具链（缺失则降级为明确标注的 ReportLab 审阅预览，不阻塞）
    for tool in ("latexmk", "pdflatex"):
        from shutil import which
        if which(tool) is None:
            report["warning"].append(
                f"LaTeX 工具 {tool!r} 缺失 → stage7 第 {1 if tool=='latexmk' else 2} 档编译跳过，"
                f"自动降级 ReportLab 审阅预览"
            )

    try:
        from jiuwenswarm.common.utils import get_env_file
        env_file = get_env_file()
        if not env_file.is_file():
            report["warning"].append(f"JiuwenSwarm 用户级 .env 缺失: {env_file}")
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
